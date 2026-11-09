"""
MQTT stop-arrivals publisher — event-driven architecture.

Instead of polling vehicle_states_current from the DB every N seconds,
this service subscribes to the fusion vehicle snapshot topic. When a new
snapshot arrives, it recomputes ETAs only for stops whose serving vehicles
have moved more than _POSITION_CHANGE_THRESHOLD_M metres, and publishes
only those stops.

DB usage per triggered cycle:
  - 2 queries (stop_times + fallback schedules) using the ANY(%s) approach
  - 0 queries if no vehicle positions changed

This eliminates the ~900ms postgres scan and the redundant
vehicle_states_current read that duplicated fusion's own DB write.
"""

import hashlib
import itertools
import json
import logging
import os
import queue
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt

from transit.config import load_settings
from transit.db import connect_db
from transit.services.live_stop_arrivals import (
    build_shape_distances,
    format_local_predicted_time,
    format_seconds_as_gtfs_time,
    parse_gtfs_time_to_seconds,
    project_point_to_shape,
    seconds_since_service_midnight,
)

SCHEMA_VERSION = 4

# Skip recomputing ETAs for vehicles that moved less than this
_POSITION_CHANGE_THRESHOLD_M = 5.0
_MAX_VEHICLE_STALENESS_S = 60


def _position_hash(vehicles: list[dict]) -> str:
    key = "|".join(
        f"{v.get('trip_instance_id')}:{v.get('lat', 0):.5f}:{v.get('lon', 0):.5f}"
        for v in sorted(vehicles, key=lambda v: v.get("trip_instance_id") or "")
    )
    return hashlib.md5(key.encode()).hexdigest()


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    R = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))



def _is_import_in_progress(conn, city_code: str) -> bool:
    """Check if a GTFS import is in progress for this city. If so, workers should pause."""
    try:
        row = conn.execute(
            """
            SELECT COALESCE(crc.gtfs_import_in_progress, FALSE)
            FROM cities c
            JOIN city_runtime_config crc ON crc.city_id = c.city_id
            WHERE c.city_code = %s
            """,
            (city_code,),
        ).fetchone()
        return bool(row[0]) if row else False
    except Exception:
        return False

class StopArrivalsService:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.running = True
        self.client = None
        self.interval_seconds = int(
            os.getenv("STOP_ARRIVALS_INTERVAL_SECONDS", str(self.settings.interval_seconds))
        )
        self.horizon_seconds = int(os.getenv("STOP_ARRIVALS_HORIZON_SECONDS", "1800"))
        self.limit_per_stop = int(os.getenv("STOP_ARRIVALS_LIMIT_PER_STOP", "8"))
        self.shape_speed_mps = float(os.getenv("STOP_ARRIVALS_SHAPE_SPEED_MPS", "7.0"))
        self.shape_max_distance_m = float(os.getenv("STOP_ARRIVALS_SHAPE_MAX_DISTANCE_M", "180"))
        self.shape_passed_grace_m = float(os.getenv("STOP_ARRIVALS_SHAPE_PASSED_GRACE_M", "80"))

        # Dedicated MQTT publish thread — decouples compute from network I/O
        self._publish_queue: queue.Queue = queue.Queue(maxsize=20000)
        self._publisher_thread: threading.Thread | None = None

        # MQTT-driven state
        self._vehicle_lock = threading.Lock()
        self._current_vehicles: list[dict] = []
        self._last_snapshot_hash: str = ""
        self._new_snapshot_event = threading.Event()

        # Per-vehicle position tracking for change detection
        self._prev_positions: dict[str, tuple[float, float]] = {}
        self._last_published_at: dict[str, float] = {}
        # Track which stop_pks were last published per vehicle trip_instance_id
        # Used to clear stale retained messages when a vehicle passes a stop
        self._vehicle_stop_pks: dict[str, set[int]] = {}

        # Static data loaded once at startup
        self._all_stops: dict[int, dict] = {}
        self._shape_cache: dict[int, dict | None] = {}
        self._city_meta: dict = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop(self, *_args):
        logging.info("Stopping stop arrivals worker for city=%s", self.settings.city_code)
        self.running = False
        if self.client:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # MQTT
    # ------------------------------------------------------------------

    def _vehicle_snapshot_topic(self) -> str:
        prefix = self.settings.mqtt_topic_prefix.rstrip("/")
        return f"{prefix}/{self.settings.city_code}/vehicles/snapshot"

    def _stop_arrivals_topic(self, stop_id: str) -> str:
        prefix = self.settings.mqtt_topic_prefix.rstrip("/")
        return f"{prefix}/{self.settings.city_code}/stops/{stop_id}/arrivals"

    def _stop_index_topic(self) -> str:
        prefix = self.settings.mqtt_topic_prefix.rstrip("/")
        return f"{prefix}/{self.settings.city_code}/stops/index"

    def start_mqtt(self):
        if not self.settings.mqtt_enabled:
            logging.info("MQTT disabled for city=%s", self.settings.city_code)
            return

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if self.settings.mqtt_username:
            client.username_pw_set(self.settings.mqtt_username, self.settings.mqtt_password)

        def on_connect(client, userdata, flags, reason_code, properties):
            logging.info(
                "MQTT connected for stop arrivals city=%s reason_code=%s",
                self.settings.city_code, reason_code,
            )
            client.subscribe(self._vehicle_snapshot_topic(), qos=0)
            logging.info("Subscribed to %s", self._vehicle_snapshot_topic())

        def on_disconnect(client, userdata, flags, reason_code, properties):
            logging.warning(
                "MQTT disconnected for stop arrivals city=%s reason_code=%s",
                self.settings.city_code, reason_code,
            )

        def on_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload.decode())
                vehicles = payload.get("vehicles", [])
                h = _position_hash(vehicles)
                with self._vehicle_lock:
                    if h != self._last_snapshot_hash:
                        self._current_vehicles = vehicles
                        self._last_snapshot_hash = h
                        self._new_snapshot_event.set()
            except Exception:
                logging.exception("Failed to parse vehicle snapshot city=%s", self.settings.city_code)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        client.connect(self.settings.mqtt_host, self.settings.mqtt_port, self.settings.mqtt_keepalive)
        client.loop_start()
        self.client = client
        self._publisher_thread = threading.Thread(target=self._publisher_loop, daemon=True, name=f"mqtt-pub-{self.settings.city_code}")
        self._publisher_thread.start()

    def _publish_json(self, topic: str, payload: dict, retain: bool = True) -> None:
        """Enqueue for async publish. Returns immediately; publisher thread does the I/O."""
        if not self.client:
            return
        try:
            self._publish_queue.put_nowait((topic, json.dumps(payload, ensure_ascii=False), retain))
        except queue.Full:
            logging.warning("Publish queue full, dropping message for topic=%s", topic)

    def _publisher_loop(self) -> None:
        """Dedicated thread that drains the publish queue and calls paho."""
        while self.running or not self._publish_queue.empty():
            try:
                topic, payload_str, retain = self._publish_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                if self.client:
                    self.client.publish(topic, payload_str, qos=0, retain=retain)
            except Exception as e:
                logging.warning("Publish error topic=%s: %s", topic, e)
            finally:
                self._publish_queue.task_done()

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _load_static_data(self, conn) -> None:
        city_id = self._city_meta["city_id"]
        rows = conn.execute(
            "SELECT stop_pk, stop_id, stop_code, stop_name, stop_lat, stop_lon FROM stops WHERE city_id = %s",
            (city_id,),
        ).fetchall()
        self._all_stops = {
            int(r[0]): {
                "stop_pk": int(r[0]),
                "stop_id": str(r[1]),
                "stop_code": str(r[2]) if r[2] is not None else None,
                "stop_name": str(r[3]) if r[3] is not None else str(r[1]),
                "stop_lat": float(r[4]) if r[4] is not None else None,
                "stop_lon": float(r[5]) if r[5] is not None else None,
            }
            for r in rows
        }
        logging.info("Loaded %s stops for city=%s", len(self._all_stops), self.settings.city_code)

    def _bulk_load_shapes(self, conn, shape_pks: set[int]) -> None:
        missing = [pk for pk in shape_pks if pk not in self._shape_cache]
        if not missing:
            return
        rows = conn.execute(
            "SELECT shape_pk, shape_pt_lat, shape_pt_lon, shape_dist_traveled "
            "FROM shape_points WHERE shape_pk = ANY(%s) ORDER BY shape_pk, shape_pt_sequence",
            (missing,),
        ).fetchall()
        raw: dict[int, list] = defaultdict(list)
        for r in rows:
            raw[int(r[0])].append(r)
        for shape_pk, pts in raw.items():
            points = [(float(r[1]), float(r[2])) for r in pts if r[1] is not None]
            if len(points) < 2:
                self._shape_cache[shape_pk] = None
                continue
            distances = (
                [float(r[3]) for r in pts]
                if all(r[3] is not None for r in pts)
                else build_shape_distances(points)
            )
            self._shape_cache[shape_pk] = {"points": points, "distances": distances}
        for pk in missing:
            if pk not in self._shape_cache:
                self._shape_cache[pk] = None

    def _load_stop_time_rows(self, conn, city_id: int, trip_pks: list[int], now_seconds: int) -> list[tuple]:
        if not trip_pks:
            return []
        return conn.execute(
            """
            SELECT
                st.stop_pk, st.trip_pk, st.stop_sequence,
                st.arrival_time, st.departure_time,
                st.arrival_seconds, st.departure_seconds,
                st.shape_dist_traveled, t.shape_pk,
                COALESCE(r.route_short_name, r.route_id) AS route_name,
                stu.arrival_delay_seconds, stu.arrival_time_epoch,
                stu.departure_delay_seconds, stu.departure_time_epoch
            FROM stop_times st
            JOIN trips t ON t.trip_pk = st.trip_pk
            JOIN routes r ON r.route_pk = t.route_pk
            JOIN stops s ON s.stop_pk = st.stop_pk
            LEFT JOIN gtfs_rt_trip_updates_current tu
              ON tu.city_id = %s AND tu.trip_pk = st.trip_pk
            LEFT JOIN gtfs_rt_stop_time_updates_current stu
              ON stu.city_id = tu.city_id
             AND stu.trip_key = tu.trip_key
             AND (
                   (stu.stop_sequence IS NOT NULL AND stu.stop_sequence = st.stop_sequence)
                OR (stu.stop_sequence IS NULL AND stu.stop_id IS NOT NULL AND stu.stop_id = s.stop_id)
             )
            WHERE st.trip_pk = ANY(%s)
              AND st.arrival_seconds >= %s::integer - 90
              AND st.arrival_seconds <= %s::integer + %s::integer
            ORDER BY st.stop_pk, st.arrival_seconds, st.trip_pk
            """,
            (city_id, trip_pks, now_seconds, now_seconds, self.horizon_seconds),
        ).fetchall()

    def _load_fallback_schedules(self, conn, stop_pks: list[int], now_seconds: int) -> dict[int, list]:
        if not stop_pks:
            return {}
        rows = conn.execute(
            """
            SELECT DISTINCT ON (st.stop_pk, st.arrival_seconds, r.route_id)
                st.stop_pk,
                r.route_id,
                COALESCE(r.route_short_name, r.route_id),
                st.arrival_time, st.arrival_seconds
            FROM stop_times st
            JOIN trips t ON t.trip_pk = st.trip_pk
            JOIN routes r ON r.route_pk = t.route_pk
            WHERE st.stop_pk = ANY(%s)
              AND st.arrival_seconds IS NOT NULL
              AND st.arrival_seconds >= %s
              AND st.arrival_seconds <= %s
            ORDER BY st.stop_pk, st.arrival_seconds, r.route_id
            """,
            (stop_pks, now_seconds, now_seconds + self.horizon_seconds),
        ).fetchall()
        result: dict[int, list] = defaultdict(list)
        counts: dict[int, int] = defaultdict(int)
        for r in rows:
            spk = int(r[0])
            if counts[spk] >= self.limit_per_stop:
                continue
            result[spk].append({
                "route_id": str(r[1]) if r[1] is not None else None,
                "route_name": str(r[2]) if r[2] is not None else None,
                "scheduled_time": str(r[3]) if r[3] is not None else None,
                "source": "gtfs_static_schedule",
            })
            counts[spk] += 1
        return result

    # ------------------------------------------------------------------
    # Core cycle
    # ------------------------------------------------------------------

    def _find_changed_vehicles(self, vehicles: list[dict]) -> list[dict]:
        """Return vehicles that moved >= threshold OR whose stops are overdue for refresh.

        The movement threshold alone misses slow/stopped buses. The time-based
        fallback ensures stops never go more than _MAX_VEHICLE_STALENESS_S stale.
        """
        changed = []
        current_ids = set()
        now_ts = time.time()
        for v in vehicles:
            tid = v.get("trip_instance_id") or ""
            current_ids.add(tid)
            lat = v.get("lat")
            lon = v.get("lon")
            if lat is None or lon is None:
                continue
            prev = self._prev_positions.get(tid)
            last_pub = self._last_published_at.get(tid, 0.0)
            moved = prev is None or _haversine_m(prev[0], prev[1], lat, lon) >= _POSITION_CHANGE_THRESHOLD_M
            stale = (now_ts - last_pub) >= _MAX_VEHICLE_STALENESS_S
            if moved or stale:
                changed.append(v)
                self._prev_positions[tid] = (lat, lon)
                self._last_published_at[tid] = now_ts
        # Evict entries for vehicles no longer present
        for tid in list(self._prev_positions):
            if tid not in current_ids:
                del self._prev_positions[tid]
                self._last_published_at.pop(tid, None)
        return changed

    def _compute_and_publish(self, conn, vehicles: list[dict], now: datetime) -> int:
        city_id = self._city_meta["city_id"]
        city_name = self._city_meta["city_name"]
        city_timezone = self._city_meta["timezone"]
        rollover_hour = self._city_meta["rollover_hour"]
        city_code = self.settings.city_code

        now_seconds = seconds_since_service_midnight(now, city_timezone, rollover_hour)
        now_epoch = int(now.timestamp())

        changed = self._find_changed_vehicles(vehicles)
        if not changed:
            return 0

        vehicles_by_trip_pk: dict[int, list[dict]] = defaultdict(list)
        for v in changed:
            tp = v.get("trip_pk")
            if tp is not None:
                vehicles_by_trip_pk[int(tp)].append(v)

        trip_pks = list(vehicles_by_trip_pk.keys())
        st_rows = self._load_stop_time_rows(conn, city_id, trip_pks, now_seconds)
        if not st_rows:
            return 0

        needed_shapes = {int(r[8]) for r in st_rows if r[8] is not None}
        self._bulk_load_shapes(conn, needed_shapes)

        rows_by_stop: dict[int, list] = defaultdict(list)
        for r in st_rows:
            rows_by_stop[int(r[0])].append(r)

        fallback_by_stop = self._load_fallback_schedules(conn, list(rows_by_stop.keys()), now_seconds)

        published = 0
        limit = self.limit_per_stop

        # Track which stop_pks are being published this cycle per vehicle
        current_vehicle_stop_pks: dict[str, set[int]] = defaultdict(set)

        # --- Derive implied speed per vehicle from nearest RT-anchored stop ---
        # For each vehicle: find the nearest upcoming stop that has arrival_time_epoch
        # and shape_dist_traveled, project vehicle onto shape, compute speed from
        # remaining_dist / time_to_that_stop. Falls back to self.shape_speed_mps.
        _MIN_SPEED_MPS = 1.0   # floor — stopped/near-stopped vehicles
        _MAX_SPEED_MPS = 25.0  # ceiling — bad data guard
        _MIN_RT_SECONDS = 5    # ignore RT anchors less than 5s away (already passing)

        implied_speed_by_trip_pk: dict[int, float] = {}

        for trip_pk, trip_st_rows in itertools.groupby(
            sorted(st_rows, key=lambda r: (int(r[1]), int(r[5]) if r[5] is not None else 999999)),
            key=lambda r: int(r[1]),
        ):
            vehicles_for_trip = vehicles_by_trip_pk.get(trip_pk, [])
            if not vehicles_for_trip:
                continue

            # Collect RT-anchored stops for this trip (have epoch + shape_dist)
            rt_anchors = []
            for r in trip_st_rows:
                epoch = int(r[11]) if r[11] is not None else None
                shape_dist = float(r[7]) if r[7] is not None else None
                shape_pk = int(r[8]) if r[8] is not None else None
                if epoch is None or shape_dist is None or shape_pk is None:
                    continue
                time_to_stop = epoch - now_epoch
                if time_to_stop < _MIN_RT_SECONDS:
                    continue
                rt_anchors.append((time_to_stop, shape_dist, shape_pk))

            if not rt_anchors:
                continue

            # Pick the nearest upcoming RT anchor
            rt_anchors.sort(key=lambda x: x[0])
            time_to_anchor, anchor_shape_dist, anchor_shape_pk = rt_anchors[0]

            shape_data = self._shape_cache.get(anchor_shape_pk)
            if not shape_data:
                continue

            for v in vehicles_for_trip:
                vlat = v.get("lat")
                vlon = v.get("lon")
                if vlat is None or vlon is None:
                    continue
                vp = project_point_to_shape(vlat, vlon, shape_data["points"], shape_data["distances"])
                if vp is None:
                    continue
                vehicle_dist = vp["shape_dist_traveled"]
                remaining_m = anchor_shape_dist - vehicle_dist
                if remaining_m <= 0 or time_to_anchor <= 0:
                    continue
                implied = remaining_m / time_to_anchor
                implied = max(_MIN_SPEED_MPS, min(_MAX_SPEED_MPS, implied))
                tid = v.get("trip_instance_id") or str(trip_pk)
                implied_speed_by_trip_pk[trip_pk] = implied
                logging.debug(
                    "Implied speed city=%s vehicle=%s trip_pk=%s speed_mps=%.2f remaining_m=%.0f time_s=%.0f",
                    city_code, v.get("vehicle_id"), trip_pk, implied, remaining_m, time_to_anchor,
                )

        for stop_pk, stop_rows in rows_by_stop.items():
            stop_info = self._all_stops.get(stop_pk)
            if stop_info is None:
                continue

            stop_id = stop_info["stop_id"]
            stop_lat = stop_info["stop_lat"]
            stop_lon = stop_info["stop_lon"]
            arrivals: list[dict] = []
            seen: set[tuple] = set()

            for r in stop_rows:
                trip_pk = int(r[1])
                for v in vehicles_by_trip_pk.get(trip_pk, []):
                    scheduled_time = str(r[3]) if r[3] is not None else None
                    scheduled_seconds = int(r[5]) if r[5] is not None else parse_gtfs_time_to_seconds(scheduled_time)
                    if scheduled_seconds is None:
                        continue

                    vehicle_lat = v.get("lat")
                    vehicle_lon = v.get("lon")
                    target_shape_dist = float(r[7]) if r[7] is not None else None
                    shape_pk = int(r[8]) if r[8] is not None else None
                    arrival_delay_seconds = int(r[10]) if r[10] is not None else None
                    _raw_arrival_time_epoch = int(r[11]) if r[11] is not None else None
                    _tu_updated_at = r[14] if len(r) > 14 else None
                    # Discard stale RT trip update predictions (>3 min old)
                    if _raw_arrival_time_epoch is not None and _tu_updated_at is not None:
                        _age = (now - (_tu_updated_at if _tu_updated_at.tzinfo else _tu_updated_at.replace(tzinfo=timezone.utc))).total_seconds()
                        arrival_time_epoch = _raw_arrival_time_epoch if _age <= 180 else None
                    else:
                        arrival_time_epoch = _raw_arrival_time_epoch

                    vehicle_shape_dist = None
                    vehicle_distance_from_shape_m = None
                    shape_eta_seconds = None

                    if arrival_time_epoch is not None:
                        eta_seconds = arrival_time_epoch - now_epoch
                        try:
                            predicted_tz = ZoneInfo(city_timezone)
                        except Exception:
                            predicted_tz = ZoneInfo("UTC")
                        predicted_time = datetime.fromtimestamp(arrival_time_epoch, tz=predicted_tz).strftime("%H:%M:%S")
                        prediction_source = "gtfs_rt_trip_update"
                    else:
                        if (
                            shape_pk is not None
                            and vehicle_lat is not None
                            and vehicle_lon is not None
                            and stop_lat is not None
                            and stop_lon is not None
                            and self.shape_speed_mps > 0
                        ):
                            shape_data = self._shape_cache.get(shape_pk)
                            if shape_data:
                                vp = project_point_to_shape(vehicle_lat, vehicle_lon, shape_data["points"], shape_data["distances"])
                                if target_shape_dist is None:
                                    sp = project_point_to_shape(stop_lat, stop_lon, shape_data["points"], shape_data["distances"])
                                    target_shape_dist = sp["shape_dist_traveled"] if sp else None
                                if vp is not None and target_shape_dist is not None:
                                    vehicle_shape_dist = vp["shape_dist_traveled"]
                                    vehicle_distance_from_shape_m = vp["distance_from_shape_m"]
                                    remaining_m = target_shape_dist - vehicle_shape_dist
                                    effective_speed = implied_speed_by_trip_pk.get(trip_pk, self.shape_speed_mps)
                                    if vehicle_distance_from_shape_m <= self.shape_max_distance_m and remaining_m >= -self.shape_passed_grace_m:
                                        shape_eta_seconds = max(0, int(remaining_m / effective_speed))

                        if shape_eta_seconds is not None:
                            eta_seconds = shape_eta_seconds
                            predicted_time = format_local_predicted_time(now, city_timezone, eta_seconds)
                            prediction_source = "shape_distance_rt"
                        else:
                            predicted_seconds = scheduled_seconds + (arrival_delay_seconds or 0)
                            eta_seconds = predicted_seconds - now_seconds
                            predicted_time = format_seconds_as_gtfs_time(predicted_seconds)
                            prediction_source = "scheduled_static"

                    if eta_seconds < -90 or eta_seconds > self.horizon_seconds:
                        continue

                    key = (v.get("trip_instance_id"), v.get("route_id"), int(r[2]) if r[2] is not None else None)
                    if key in seen:
                        continue
                    seen.add(key)

                    arrivals.append({
                        "route_id": v.get("route_id"),
                        "route_name": str(r[9]) if r[9] is not None else v.get("route_id"),
                        "trip_instance_id": v.get("trip_instance_id"),
                        "trip_pk": trip_pk,
                        "trip_id": v.get("trip_id"),
                        "vehicle_id": v.get("vehicle_id"),
                        "vehicle_label": v.get("vehicle_label"),
                        "raw_rt_trip_id": v.get("raw_rt_trip_id"),
                        "stop_id": stop_id,
                        "stop_sequence": int(r[2]) if r[2] is not None else None,
                        "scheduled_time": scheduled_time,
                        "predicted_time": predicted_time,
                        "eta_seconds": max(0, int(eta_seconds)),
                        "delay_seconds": arrival_delay_seconds,
                        "source": v.get("source"),
                        "prediction_source": prediction_source,
                        "match_strategy": v.get("match_strategy"),
                        "match_confidence": v.get("match_confidence"),
                        "vehicle_lat": vehicle_lat,
                        "vehicle_lon": vehicle_lon,
                        "vehicle_updated_at": None,
                        "current_stop_sequence": v.get("current_stop_sequence"),
                        "next_stop_sequence": v.get("next_stop_sequence"),
                        "previous_stop_sequence": v.get("previous_stop_sequence"),
                        "vehicle_shape_dist_traveled": vehicle_shape_dist,
                        "target_shape_dist_traveled": target_shape_dist,
                        "vehicle_distance_from_shape_m": vehicle_distance_from_shape_m,
                    })

            arrivals.sort(key=lambda x: (x["eta_seconds"], x.get("route_name") or "", x.get("vehicle_label") or ""))
            arrivals = arrivals[:limit]

            # Track which vehicles are contributing to this stop this cycle
            for arrival in arrivals:
                tid = arrival.get("trip_instance_id")
                if tid:
                    current_vehicle_stop_pks[tid].add(stop_pk)

            # Skip publishing stops with no arrivals — the stale-stop clearing
            # mechanism handles removing outdated retained messages when vehicles pass.
            # Publishing empty payloads for every served stop doubles the MQTT write load.
            if not arrivals:
                continue

            self._publish_json(self._stop_arrivals_topic(stop_id), {
                "city": city_code,
                "city_name": city_name,
                "stop_id": stop_id,
                "stop_code": stop_info["stop_code"],
                "stop_name": stop_info["stop_name"],
                "stop_lat": stop_lat,
                "stop_lon": stop_lon,
                "updated_at": now.isoformat(),
                "timezone": city_timezone,
                "arrival_count": len(arrivals),
                "arrivals": arrivals,
                "fallback_schedules": fallback_by_stop.get(stop_pk, []),
                "schema_version": SCHEMA_VERSION,
            })
            published += 1

        # Update per-vehicle stop tracking (used for future clearing logic)
        for v in changed:
            tid = v.get("trip_instance_id") or ""
            curr_stop_pks = current_vehicle_stop_pks.get(tid, set())
            if curr_stop_pks:
                self._vehicle_stop_pks[tid] = curr_stop_pks
            elif tid in self._vehicle_stop_pks:
                del self._vehicle_stop_pks[tid]

        # Evict tracking for vehicles no longer present in the snapshot
        current_tids = {v.get("trip_instance_id") for v in vehicles}
        for tid in list(self._vehicle_stop_pks):
            if tid not in current_tids:
                del self._vehicle_stop_pks[tid]

        return published

    def _publish_index(self) -> None:
        stop_ids = sorted(s["stop_id"] for s in self._all_stops.values())
        self._publish_json(self._stop_index_topic(), {
            "city": self.settings.city_code,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "stop_count": len(stop_ids),
            "stops": stop_ids,
        })

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        self.start_mqtt()
        logging.info("Starting stop arrivals worker for city=%s", self.settings.city_code)

        with connect_db(self.settings.db_config) as conn:
            row = conn.execute(
                """
                SELECT c.city_id, c.city_name,
                       COALESCE(crc.service_day_rollover_hour, 4),
                       COALESCE(c.timezone, crc.timezone_name, 'UTC')
                FROM cities c
                LEFT JOIN city_runtime_config crc ON crc.city_id = c.city_id
                WHERE c.city_code = %s
                """,
                (self.settings.city_code,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"City not found: {self.settings.city_code}")
            self._city_meta = {
                "city_id": int(row[0]),
                "city_name": str(row[1]),
                "rollover_hour": int(row[2]),
                "timezone": str(row[3]),
            }

            self._load_static_data(conn)
            self._publish_index()

            logging.info("Waiting for first vehicle snapshot city=%s", self.settings.city_code)
            self._new_snapshot_event.wait(timeout=30)

            _cycle_count = 0

            while self.running:
                # Block until new snapshot or timeout
                self._new_snapshot_event.wait(timeout=self.interval_seconds)
                self._new_snapshot_event.clear()

                if not self.running:
                    break

                if _is_import_in_progress(conn, self.settings.city_code):
                    logging.info("Stop arrivals paused: GTFS import in progress city=%s", self.settings.city_code)
                    continue

                with self._vehicle_lock:
                    vehicles = list(self._current_vehicles)

                if not vehicles:
                    continue

                started = time.time()
                now = datetime.now(timezone.utc).astimezone()

                try:
                    published = self._compute_and_publish(conn, vehicles, now)
                    conn.commit()
                except Exception:
                    logging.exception("Stop arrivals cycle error city=%s", self.settings.city_code)
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    continue

                _cycle_count += 1
                if published > 0:
                    logging.info(
                        "Stop arrivals cycle complete city=%s stops_updated=%s total_stops=%s duration_s=%.3f",
                        self.settings.city_code,
                        published,
                        len(self._all_stops),
                        time.time() - started,
                    )

                # Refresh stop index periodically
                if _cycle_count % 60 == 0:
                    self._publish_index()
