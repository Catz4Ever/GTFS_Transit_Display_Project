#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import os
import signal
import sys
import time
from datetime import datetime

from transit.config import load_settings
from transit.db import connect_db
from transit.repositories.gtfs_repository import GtfsRepository
from transit.services.calendar_service import get_service_date_and_time
from transit.services.positioning_service import PositioningService
from transit.services.realtime_adjustment import apply_trip_update_delays, estimate_timeline_delay_seconds



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

class SimulationService:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.repo = GtfsRepository()
        self.positioning = PositioningService()
        self.running = True
        self.enable_rt_timeline_adjustment = os.getenv("SIM_APPLY_TRIP_UPDATES", "true").lower() in {"1", "true", "yes"}
        self.simulation_trip_lookbehind_seconds = int(os.getenv("SIM_ACTIVE_TRIP_LOOKBEHIND_SECONDS", "900"))
        self.simulation_trip_lookahead_seconds = int(os.getenv("SIM_ACTIVE_TRIP_LOOKAHEAD_SECONDS", "7200"))

    def stop(self, *_args):
        logging.info("Stopping simulation worker for city=%s", self.settings.city_code)
        self.running = False

    def _warm_caches_for_trips(self, conn, trips) -> None:
        """Bulk-load timelines and shapes for the active trip window in two queries."""
        trip_pks = [t.trip_pk for t in trips]
        shape_pks = list({t.shape_pk for t in trips if t.shape_pk is not None})
        self.repo.warm_caches(conn, trip_pks, shape_pks)
        logging.info(
            "Cache warmed city=%s trips=%s shapes=%s timeline_cached=%s shape_cached=%s",
            self.settings.city_code,
            len(trip_pks),
            len(shape_pks),
            len(self.repo.timeline_cache),
            len(self.repo.shape_cache),
        )

    def get_latest_stop_time_updates(self, conn, city_id: int, trip_pk: int | None, trip_id: str | None, start_date: str | None = None, start_time: str | None = None) -> list[dict]:
        """Fetch current GTFS-RT StopTimeUpdates for a trip from the small current cache.

        This avoids scanning partitioned RT history tables on every simulation cycle.
        """
        if trip_pk is None and not trip_id:
            return []

        conditions = ["tu.city_id = %s"]
        params: list = [city_id]
        if trip_pk is not None:
            conditions.append("tu.trip_pk = %s")
            params.append(trip_pk)
        else:
            conditions.append("tu.trip_id = %s")
            params.append(trip_id)

        order_bits = ["tu.updated_at DESC"]
        if start_date:
            order_bits.insert(0, "CASE WHEN tu.start_date = %s THEN 0 ELSE 1 END")
            params.append(start_date)
        if start_time:
            order_bits.insert(0, "CASE WHEN tu.start_time = %s THEN 0 ELSE 1 END")
            params.append(start_time)

        latest = conn.execute(
            f"""
            SELECT tu.trip_key
            FROM gtfs_rt_trip_updates_current tu
            WHERE {' AND '.join(conditions)}
            ORDER BY {', '.join(order_bits)}
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        if latest is None:
            return []

        rows = conn.execute(
            """
            SELECT stop_sequence, stop_id, arrival_delay_seconds, departure_delay_seconds,
                   arrival_time_epoch, departure_time_epoch, schedule_relationship
            FROM gtfs_rt_stop_time_updates_current
            WHERE city_id = %s AND trip_key = %s
            ORDER BY stop_sequence NULLS LAST, stop_id NULLS LAST, stop_update_key
            """,
            (city_id, str(latest[0])),
        ).fetchall()
        return [
            {
                "stop_sequence": int(r[0]) if r[0] is not None else None,
                "stop_id": str(r[1]) if r[1] is not None else None,
                "arrival_delay_seconds": int(r[2]) if r[2] is not None else None,
                "departure_delay_seconds": int(r[3]) if r[3] is not None else None,
                "arrival_time_epoch": int(r[4]) if r[4] is not None else None,
                "departure_time_epoch": int(r[5]) if r[5] is not None else None,
                "schedule_relationship": int(r[6]) if r[6] is not None else None,
            }
            for r in rows
        ]

    def replace_simulated_vehicle_states(self, conn, city_id: int, vehicles: list[dict], updated_at: datetime):
        conn.execute("DELETE FROM vehicle_states_simulated_current WHERE city_id = %s", (city_id,))

        for vehicle in vehicles:
            conn.execute(
                """
                INSERT INTO vehicle_states_simulated_current (
                    city_id, trip_instance_id,
                    trip_pk, route_pk, service_pk, shape_pk,
                    trip_id, route_id, service_id, shape_id,
                    status, source, lat, lon, trip_progress, updated_at,
                    current_stop_sequence, next_stop_sequence, previous_stop_sequence,
                    scheduled_delay_seconds, adjusted_progress
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (city_id, trip_instance_id) DO UPDATE
                SET trip_pk=EXCLUDED.trip_pk,
                    route_pk=EXCLUDED.route_pk,
                    service_pk=EXCLUDED.service_pk,
                    shape_pk=EXCLUDED.shape_pk,
                    trip_id=EXCLUDED.trip_id,
                    route_id=EXCLUDED.route_id,
                    service_id=EXCLUDED.service_id,
                    shape_id=EXCLUDED.shape_id,
                    status=EXCLUDED.status,
                    source=EXCLUDED.source,
                    lat=EXCLUDED.lat,
                    lon=EXCLUDED.lon,
                    trip_progress=EXCLUDED.trip_progress,
                    updated_at=EXCLUDED.updated_at,
                    current_stop_sequence=EXCLUDED.current_stop_sequence,
                    next_stop_sequence=EXCLUDED.next_stop_sequence,
                    previous_stop_sequence=EXCLUDED.previous_stop_sequence,
                    scheduled_delay_seconds=EXCLUDED.scheduled_delay_seconds,
                    adjusted_progress=EXCLUDED.adjusted_progress
                """,
                (
                    city_id,
                    vehicle["trip_instance_id"],
                    vehicle["trip_pk"],
                    vehicle["route_pk"],
                    vehicle["service_pk"],
                    vehicle["shape_pk"],
                    vehicle["trip_id"],
                    vehicle["route_id"],
                    vehicle["service_id"],
                    vehicle["shape_id"],
                    vehicle["status"],
                    vehicle["source"],
                    vehicle["lat"],
                    vehicle["lon"],
                    vehicle["trip_progress"],
                    updated_at,
                    vehicle.get("current_stop_sequence"),
                    vehicle.get("next_stop_sequence"),
                    vehicle.get("previous_stop_sequence"),
                    vehicle.get("scheduled_delay_seconds"),
                    vehicle.get("adjusted_progress"),
                ),
            )

    def run(self):
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        logging.info("Starting simulation worker for city=%s", self.settings.city_code)

        with connect_db(self.settings.db_config) as conn:
            city_id, city_name = self.repo.get_city_row(conn, self.settings.city_code)

            # State carried across cycles to avoid redundant DB fetches
            _last_active_service_pks: frozenset[int] = frozenset()
            _last_trip_pks: frozenset[int] = frozenset()
            _cached_trips: list = []
            _caches_warmed = False

            while self.running:
                if _is_import_in_progress(conn, self.settings.city_code):
                    logging.info("Simulation paused: GTFS import in progress city=%s", self.settings.city_code)
                    time.sleep(5)
                    continue
                started = time.time()
                now = datetime.now()

                target_date, current_time_str = get_service_date_and_time(
                    now=now,
                    forced_date=self.settings.force_date,
                    forced_time=self.settings.force_time,
                    rollover_hour=self.settings.service_day_rollover_hour,
                )
                gtfs_start_date = target_date.strftime("%Y%m%d")

                active_service_pks = self.repo.get_active_service_pks(conn, city_id, target_date)
                active_service_pks_frozen = frozenset(active_service_pks)

                # Only re-fetch the active trip window when the service set changes.
                # For SFBA this query returns 1,700+ trips; skipping it saves ~0.3s/cycle.
                if active_service_pks_frozen != _last_active_service_pks:
                    trips = self.repo.get_active_window_trips(
                        conn,
                        city_id,
                        active_service_pks,
                        current_time_str,
                        self.simulation_trip_lookbehind_seconds,
                        self.simulation_trip_lookahead_seconds,
                    )
                    _cached_trips = trips
                    _last_active_service_pks = active_service_pks_frozen
                    _caches_warmed = False
                else:
                    trips = _cached_trips

                # Warm timeline + shape caches once per trip set (two bulk queries).
                # After warming, get_trip_timeline / get_shape_points are pure memory lookups.
                new_trip_pks = frozenset(t.trip_pk for t in trips)
                if not _caches_warmed or new_trip_pks != _last_trip_pks:
                    self._warm_caches_for_trips(conn, trips)
                    _last_trip_pks = new_trip_pks
                    _caches_warmed = True

                logging.info(
                    "Loaded city=%s city_id=%s city_name=%s active_services=%s active_window_trips=%s",
                    self.settings.city_code,
                    city_id,
                    city_name,
                    len(active_service_pks),
                    len(trips),
                )

                vehicles = []
                skipped_no_timeline = 0
                skipped_no_shape = 0
                skipped_not_active = 0
                skipped_no_position = 0
                adjusted_with_trip_updates = 0

                for trip_row in trips:
                    instances = self.repo.expand_trip_instances(conn, trip_row, current_time_str)

                    for inst in instances:
                        timeline = self.repo.get_trip_timeline(conn, inst["trip_pk"])
                        if not timeline:
                            skipped_no_timeline += 1
                            continue

                        timeline_used = timeline
                        scheduled_delay_seconds = None
                        if self.enable_rt_timeline_adjustment:
                            rt_start_time = None
                            if "@" in inst["trip_instance_id"]:
                                try:
                                    departure = int(str(inst["trip_instance_id"]).rsplit("@", 1)[1])
                                    rt_start_time = f"{departure // 3600:02d}:{(departure % 3600) // 60:02d}:{departure % 60:02d}"
                                except Exception:
                                    rt_start_time = None
                            updates = self.get_latest_stop_time_updates(conn, city_id, inst["trip_pk"], inst["trip_id"], gtfs_start_date, rt_start_time)
                            if updates:
                                timeline_used = apply_trip_update_delays(timeline, updates)
                                scheduled_delay_seconds = estimate_timeline_delay_seconds(updates)
                                adjusted_with_trip_updates += 1

                        if inst["shape_pk"] is None:
                            skipped_no_shape += 1
                            continue

                        shape_points = self.repo.get_shape_points(conn, inst["shape_pk"])
                        if not shape_points:
                            skipped_no_shape += 1
                            continue

                        result = self.positioning.simulate_trip_state(
                            timeline_used,
                            current_time_str,
                            inst["time_shift_secs"],
                        )
                        if result["status"] != "active":
                            skipped_not_active += 1
                            continue

                        pos = self.positioning.compute_vehicle_position(
                            trip_pk=inst["trip_pk"],
                            shape_pk=inst["shape_pk"],
                            timeline=timeline_used,
                            shape_points=shape_points,
                            current_time_str=current_time_str,
                            time_shift_secs=inst["time_shift_secs"],
                        )
                        if pos is None:
                            skipped_no_position += 1
                            continue

                        lat, lon = pos
                        vehicles.append(
                            {
                                "trip_instance_id": inst["trip_instance_id"],
                                "trip_pk": inst["trip_pk"],
                                "trip_id": inst["trip_id"],
                                "route_pk": inst["route_pk"],
                                "route_id": inst["route_id"],
                                "service_pk": inst["service_pk"],
                                "service_id": inst["service_id"],
                                "shape_pk": inst["shape_pk"],
                                "shape_id": inst["shape_id"],
                                "status": "active",
                                "source": "simulated_rt_adjusted" if scheduled_delay_seconds is not None else "simulated",
                                "lat": lat,
                                "lon": lon,
                                "trip_progress": result["trip_progress"],
                                "adjusted_progress": result["trip_progress"] if scheduled_delay_seconds is not None else None,
                                "current_stop_sequence": result.get("current_stop_sequence"),
                                "next_stop_sequence": result.get("next_stop_sequence"),
                                "previous_stop_sequence": result.get("previous_stop_sequence"),
                                "scheduled_delay_seconds": scheduled_delay_seconds,
                            }
                        )

                self.replace_simulated_vehicle_states(conn, city_id, vehicles, now)
                conn.commit()

                logging.info(
                    "Simulation cycle complete city=%s date=%s time=%s active_vehicles=%s duration_s=%.3f "
                    "skipped_no_timeline=%s skipped_no_shape=%s skipped_not_active=%s skipped_no_position=%s rt_adjusted=%s",
                    self.settings.city_code,
                    target_date.isoformat(),
                    current_time_str,
                    len(vehicles),
                    time.time() - started,
                    skipped_no_timeline,
                    skipped_no_shape,
                    skipped_not_active,
                    skipped_no_position,
                    adjusted_with_trip_updates,
                )
                time.sleep(self.settings.interval_seconds)


def main():
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        SimulationService().run()
    except Exception as e:
        logging.exception("Simulation worker failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
