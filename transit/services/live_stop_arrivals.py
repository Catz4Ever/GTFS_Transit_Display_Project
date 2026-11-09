from __future__ import annotations

import math
import os
from datetime import datetime, timezone, timedelta
from typing import Any
from zoneinfo import ZoneInfo


EARTH_RADIUS_M = 6_371_000.0


def parse_gtfs_time_to_seconds(value: str | None) -> int | None:
    if not value:
        return None
    parts = str(value).split(":")
    if len(parts) != 3:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2])
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def format_seconds_as_gtfs_time(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    seconds = max(0, int(seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def seconds_since_service_midnight(now: datetime, timezone_name: str, rollover_hour: int = 4) -> int:
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = ZoneInfo("UTC")

    local_now = now.astimezone(tz) if now.tzinfo is not None else now.replace(tzinfo=tz)
    seconds = local_now.hour * 3600 + local_now.minute * 60 + local_now.second

    # GTFS allows service days to run after midnight as 24:xx, 25:xx, etc.
    # Before the configured rollover, treat current time as next-day GTFS time.
    if local_now.hour < rollover_hour:
        seconds += 24 * 3600

    return seconds

def format_local_predicted_time(now: datetime, timezone_name: str, eta_seconds: int) -> str:
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = ZoneInfo("UTC")

    local_now = now.astimezone(tz) if now.tzinfo is not None else now.replace(tzinfo=tz)
    return (local_now + timedelta(seconds=int(eta_seconds))).strftime("%H:%M:%S")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def latlon_to_xy_m(lat: float, lon: float, ref_lat: float) -> tuple[float, float]:
    x = math.radians(lon) * EARTH_RADIUS_M * math.cos(math.radians(ref_lat))
    y = math.radians(lat) * EARTH_RADIUS_M
    return x, y


def build_shape_distances(points: list[tuple[float, float]]) -> list[float]:
    distances = [0.0]
    for idx in range(1, len(points)):
        distances.append(
            distances[-1]
            + haversine_m(points[idx - 1][0], points[idx - 1][1], points[idx][0], points[idx][1])
        )
    return distances


def project_point_to_shape(
    lat: float | None,
    lon: float | None,
    points: list[tuple[float, float]],
    cumulative_distances: list[float],
) -> dict[str, float] | None:
    if lat is None or lon is None or len(points) < 2:
        return None

    ref_lat = lat
    px, py = latlon_to_xy_m(lat, lon, ref_lat)

    best: dict[str, float] | None = None

    for idx in range(len(points) - 1):
        lat1, lon1 = points[idx]
        lat2, lon2 = points[idx + 1]

        x1, y1 = latlon_to_xy_m(lat1, lon1, ref_lat)
        x2, y2 = latlon_to_xy_m(lat2, lon2, ref_lat)

        dx = x2 - x1
        dy = y2 - y1
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq <= 0:
            continue

        t = ((px - x1) * dx + (py - y1) * dy) / seg_len_sq
        t = max(0.0, min(1.0, t))

        proj_x = x1 + t * dx
        proj_y = y1 + t * dy
        off_shape_m = math.hypot(px - proj_x, py - proj_y)

        seg_len_m = cumulative_distances[idx + 1] - cumulative_distances[idx]
        shape_dist = cumulative_distances[idx] + t * seg_len_m

        if best is None or off_shape_m < best["distance_from_shape_m"]:
            best = {
                "shape_dist_traveled": shape_dist,
                "distance_from_shape_m": off_shape_m,
                "segment_index": float(idx),
            }

    return best


def latest_trip_update_snapshot_pk(conn, city_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT snapshot_pk
        FROM gtfs_rt_feed_snapshots
        WHERE city_id = %s AND feed_type = 'trip_updates'
        ORDER BY snapshot_pk DESC
        LIMIT 1
        """,
        (city_id,),
    ).fetchone()
    return int(row[0]) if row else None


def load_shape_projection_cache(conn, shape_pk: int, cache: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    if shape_pk in cache:
        return cache[shape_pk]

    rows = conn.execute(
        """
        SELECT shape_pt_lat, shape_pt_lon, shape_dist_traveled
        FROM shape_points
        WHERE shape_pk = %s
        ORDER BY shape_pt_sequence
        """,
        (shape_pk,),
    ).fetchall()

    points = [(float(r[0]), float(r[1])) for r in rows if r[0] is not None and r[1] is not None]
    if len(points) < 2:
        cache[shape_pk] = None
        return None

    if rows and all(r[2] is not None for r in rows):
        distances = [float(r[2]) for r in rows]
    else:
        distances = build_shape_distances(points)

    cache[shape_pk] = {
        "points": points,
        "distances": distances,
    }
    return cache[shape_pk]


def get_live_stop_arrivals(
    conn,
    city_id: int,
    city_code: str,
    city_name: str,
    stop_id: str,
    *,
    limit: int = 12,
    horizon_seconds: int = 3600,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return vehicle-based arrivals for a stop.

    Primary sources, in order:
    1. GTFS-RT trip update stop prediction, when available.
    2. Runtime shape-distance ETA using current vehicle GPS and stop projection.
    3. Static scheduled time as a fallback.

    The shape-distance fallback is important for GTFS-RT vehicle_position rows that
    have a matched trip but no current_stop_sequence/next_stop_sequence.
    """
    now = now or datetime.now(timezone.utc).astimezone()

    stop_row = conn.execute(
        """
        SELECT
            s.stop_pk,
            s.stop_id,
            s.stop_code,
            s.stop_name,
            s.stop_lat,
            s.stop_lon,
            COALESCE(crc.service_day_rollover_hour, 4) AS service_day_rollover_hour,
            COALESCE(c.timezone, crc.timezone_name, 'UTC') AS timezone
        FROM stops s
        JOIN cities c ON c.city_id = s.city_id
        LEFT JOIN city_runtime_config crc ON crc.city_id = c.city_id
        WHERE s.city_id = %s AND s.stop_id = %s
        """,
        (city_id, stop_id),
    ).fetchone()

    if stop_row is None:
        return {
            "city": city_code,
            "city_name": city_name,
            "stop_id": stop_id,
            "stop_name": stop_id,
            "updated_at": now.isoformat(),
            "arrival_count": 0,
            "arrivals": [],
            "fallback_schedules": [],
            "warning": "Stop not found",
            "schema_version": 4,
        }

    stop_pk = int(stop_row[0])
    stop_lat = float(stop_row[4]) if stop_row[4] is not None else None
    stop_lon = float(stop_row[5]) if stop_row[5] is not None else None

    rollover_hour = int(stop_row[6] or 4)
    city_timezone = str(stop_row[7]) if len(stop_row) > 7 and stop_row[7] else "UTC"
    now_seconds = seconds_since_service_midnight(now, city_timezone, rollover_hour)
    now_epoch = int(now.timestamp())

    shape_speed_mps = float(os.getenv("STOP_ARRIVALS_SHAPE_SPEED_MPS", "7.0"))
    shape_max_distance_m = float(os.getenv("STOP_ARRIVALS_SHAPE_MAX_DISTANCE_M", "180"))
    shape_passed_grace_m = float(os.getenv("STOP_ARRIVALS_SHAPE_PASSED_GRACE_M", "80"))

    rows = conn.execute(
        """
        WITH active AS (
            SELECT
                v.trip_instance_id,
                v.trip_pk,
                v.trip_id,
                v.route_id,
                v.source,
                v.vehicle_id,
                v.vehicle_label,
                v.raw_rt_trip_id,
                v.match_strategy,
                v.match_confidence,
                v.lat,
                v.lon,
                v.updated_at,
                v.current_stop_sequence,
                v.next_stop_sequence,
                v.previous_stop_sequence
            FROM vehicle_states_current v
            WHERE v.city_id = %s
              AND v.trip_pk IS NOT NULL
              AND v.trip_pk IN (
                SELECT st2.trip_pk
                FROM stop_times st2
                 WHERE st2.stop_pk = %s
            )
        )
        SELECT
            a.trip_instance_id,               -- 0
            a.trip_pk,                        -- 1
            a.trip_id,                        -- 2
            a.route_id,                       -- 3
            COALESCE(r.route_short_name, a.route_id) AS route_name, -- 4
            a.source,                         -- 5
            a.vehicle_id,                     -- 6
            a.vehicle_label,                  -- 7
            a.raw_rt_trip_id,                 -- 8
            a.match_strategy,                 -- 9
            a.match_confidence,               -- 10
            a.lat,                            -- 11
            a.lon,                            -- 12
            a.updated_at,                     -- 13
            a.current_stop_sequence,          -- 14
            a.next_stop_sequence,             -- 15
            a.previous_stop_sequence,         -- 16
            st.stop_sequence,                 -- 17
            st.arrival_time,                  -- 18
            st.departure_time,                -- 19
            st.arrival_seconds,               -- 20
            st.departure_seconds,             -- 21
            st.shape_dist_traveled,           -- 22
            t.shape_pk,                       -- 23
            tu.trip_key AS rt_trip_update_key,-- 24
            stu.arrival_delay_seconds,        -- 25
            stu.arrival_time_epoch,           -- 26
            stu.departure_delay_seconds,      -- 27
            stu.departure_time_epoch,         -- 28
            tu.updated_at AS tu_updated_at         -- 29
        FROM active a
        JOIN stop_times st ON st.trip_pk = a.trip_pk AND st.stop_pk = %s
        JOIN trips t ON t.trip_pk = a.trip_pk
        JOIN routes r ON r.route_pk = t.route_pk
        LEFT JOIN gtfs_rt_trip_updates_current tu
          ON tu.city_id = %s
         AND (
              tu.trip_pk = a.trip_pk
              OR (tu.trip_id IS NOT NULL AND tu.trip_id = a.trip_id)
              OR (tu.vehicle_id IS NOT NULL AND tu.vehicle_id = a.vehicle_id)
         )
        LEFT JOIN gtfs_rt_stop_time_updates_current stu
          ON stu.city_id = tu.city_id
         AND stu.trip_key = tu.trip_key
         AND (
              (stu.stop_sequence IS NOT NULL AND stu.stop_sequence = st.stop_sequence)
              OR (stu.stop_id IS NOT NULL AND stu.stop_id = %s)
         )
        ORDER BY st.arrival_time, a.route_id, a.trip_instance_id
        """,
        (city_id, stop_pk, stop_pk, city_id, stop_id),
    ).fetchall()

    arrivals: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int | None]] = set()
    shape_cache: dict[int, dict[str, Any] | None] = {}

    for r in rows:
        scheduled_time = str(r[18]) if r[18] is not None else None
        scheduled_seconds = int(r[20]) if r[20] is not None else parse_gtfs_time_to_seconds(scheduled_time)
        if scheduled_seconds is None:
            continue

        vehicle_lat = float(r[11]) if r[11] is not None else None
        vehicle_lon = float(r[12]) if r[12] is not None else None
        target_shape_dist = float(r[22]) if r[22] is not None else None
        shape_pk = int(r[23]) if r[23] is not None else None

        arrival_delay_seconds = int(r[25]) if r[25] is not None else None
        _raw_arrival_time_epoch = int(r[26]) if r[26] is not None else None
        _tu_updated_at = r[29] if len(r) > 29 else None
        # Discard RT trip update prediction if the update itself is stale (>3 min old).
        # 80% of SFBA trip updates are stale; using them produces wildly wrong ETAs.
        if _raw_arrival_time_epoch is not None and _tu_updated_at is not None:
            import datetime as _dt
            _age = (datetime.now(timezone.utc) - (_tu_updated_at if _tu_updated_at.tzinfo else _tu_updated_at.replace(tzinfo=timezone.utc))).total_seconds()
            arrival_time_epoch = _raw_arrival_time_epoch if _age <= 180 else None
        else:
            arrival_time_epoch = _raw_arrival_time_epoch

        vehicle_shape_dist = None
        vehicle_distance_from_shape_m = None
        shape_eta_seconds = None

        # Prefer explicit GTFS-RT stop prediction if present.
        if arrival_time_epoch is not None:
            eta_seconds = arrival_time_epoch - now_epoch
            try:
                predicted_tz = ZoneInfo(city_timezone)
            except Exception:
                predicted_tz = ZoneInfo("UTC")
            predicted_time = datetime.fromtimestamp(arrival_time_epoch, tz=predicted_tz).strftime("%H:%M:%S")
            prediction_source = "gtfs_rt_trip_update"

        else:
            # Runtime fallback:
            # If DB distances are NULL, project both the vehicle and the selected stop to the shape.
            if (
                shape_pk is not None
                and vehicle_lat is not None
                and vehicle_lon is not None
                and stop_lat is not None
                and stop_lon is not None
                and shape_speed_mps > 0
            ):
                shape_data = load_shape_projection_cache(conn, shape_pk, shape_cache)
                if shape_data:
                    vehicle_projection = project_point_to_shape(
                        vehicle_lat,
                        vehicle_lon,
                        shape_data["points"],
                        shape_data["distances"],
                    )

                    if target_shape_dist is None:
                        stop_projection = project_point_to_shape(
                            stop_lat,
                            stop_lon,
                            shape_data["points"],
                            shape_data["distances"],
                        )
                        target_shape_dist = (
                            stop_projection["shape_dist_traveled"] if stop_projection is not None else None
                        )

                    if vehicle_projection is not None and target_shape_dist is not None:
                        vehicle_shape_dist = vehicle_projection["shape_dist_traveled"]
                        vehicle_distance_from_shape_m = vehicle_projection["distance_from_shape_m"]
                        remaining_m = target_shape_dist - vehicle_shape_dist

                        if (
                            vehicle_distance_from_shape_m <= shape_max_distance_m
                            and remaining_m >= -shape_passed_grace_m
                        ):
                            shape_eta_seconds = max(0, int(remaining_m / shape_speed_mps))

            if shape_eta_seconds is not None:
                eta_seconds = shape_eta_seconds
                predicted_time = format_local_predicted_time(now, city_timezone, eta_seconds)
                prediction_source = "shape_distance_rt"
            else:
                predicted_seconds = scheduled_seconds + (arrival_delay_seconds or 0)
                eta_seconds = predicted_seconds - now_seconds
                predicted_time = format_seconds_as_gtfs_time(predicted_seconds)
                prediction_source = "scheduled_static"

        # Keep only upcoming arrivals, with a small grace window for vehicles currently at the stop.
        if eta_seconds < -90 or eta_seconds > horizon_seconds:
            continue

        key = (str(r[0]), str(r[3]), int(r[17]) if r[17] is not None else None)
        if key in seen:
            continue
        seen.add(key)

        arrivals.append(
            {
                "route_id": str(r[3]) if r[3] is not None else None,
                "route_name": str(r[4]) if r[4] is not None else str(r[3]),
                "trip_instance_id": str(r[0]),
                "trip_pk": int(r[1]) if r[1] is not None else None,
                "trip_id": str(r[2]) if r[2] is not None else None,
                "vehicle_id": str(r[6]) if r[6] is not None else None,
                "vehicle_label": str(r[7]) if r[7] is not None else None,
                "raw_rt_trip_id": str(r[8]) if r[8] is not None else None,
                "stop_id": stop_id,
                "stop_sequence": int(r[17]) if r[17] is not None else None,
                "scheduled_time": scheduled_time,
                "predicted_time": predicted_time,
                "eta_seconds": max(0, int(eta_seconds)),
                "delay_seconds": arrival_delay_seconds,
                "source": str(r[5]) if r[5] is not None else None,
                "prediction_source": prediction_source,
                "match_strategy": str(r[9]) if r[9] is not None else None,
                "match_confidence": float(r[10]) if r[10] is not None else None,
                "vehicle_lat": vehicle_lat,
                "vehicle_lon": vehicle_lon,
                "vehicle_updated_at": r[13].isoformat() if r[13] is not None else None,
                "current_stop_sequence": int(r[14]) if r[14] is not None else None,
                "next_stop_sequence": int(r[15]) if r[15] is not None else None,
                "previous_stop_sequence": int(r[16]) if r[16] is not None else None,
                "vehicle_shape_dist_traveled": vehicle_shape_dist,
                "target_shape_dist_traveled": target_shape_dist,
                "vehicle_distance_from_shape_m": vehicle_distance_from_shape_m,
            }
        )

    arrivals.sort(key=lambda x: (x["eta_seconds"], x.get("route_name") or "", x.get("vehicle_label") or ""))
    arrivals = arrivals[:limit]

    # Explicit fallback schedule for transparency. The UI can show this only when arrivals are empty.
    fallback_rows = conn.execute(
        """
        SELECT DISTINCT
            r.route_id,
            COALESCE(r.route_short_name, r.route_id) AS route_name,
            st.arrival_time,
            st.arrival_seconds
        FROM stop_times st
        JOIN trips t ON t.trip_pk = st.trip_pk
        JOIN routes r ON r.route_pk = t.route_pk
        WHERE st.stop_pk = %s
            AND st.arrival_seconds IS NOT NULL
            AND st.arrival_seconds >= %s
            AND st.arrival_seconds <= %s
        ORDER BY st.arrival_seconds, route_name
            LIMIT %s
        """,
        (stop_pk, now_seconds, now_seconds + horizon_seconds, limit),
    ).fetchall()

    fallback_schedules = [
        {
            "route_id": str(r[0]) if r[0] is not None else None,
            "route_name": str(r[1]) if r[1] is not None else None,
            "scheduled_time": str(r[2]) if r[2] is not None else None,
            "source": "gtfs_static_schedule",
        }
        for r in fallback_rows
    ]

    return {
        "city": city_code,
        "city_name": city_name,
        "stop_id": str(stop_row[1]),
        "stop_code": str(stop_row[2]) if stop_row[2] is not None else None,
        "stop_name": str(stop_row[3]) if stop_row[3] is not None else str(stop_row[1]),
        "stop_lat": float(stop_row[4]) if stop_row[4] is not None else None,
        "stop_lon": float(stop_row[5]) if stop_row[5] is not None else None,
        "updated_at": now.isoformat(),
        "timezone": city_timezone,
        "arrival_count": len(arrivals),
        "arrivals": arrivals,
        "fallback_schedules": fallback_schedules,
        "schema_version": 4,
    }
