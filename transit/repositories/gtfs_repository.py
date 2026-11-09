from datetime import date

from transit.models import FrequencyRow, ShapePoint, StopTimeRow, TripRow
from transit.services.positioning_service import hhmmss_to_seconds


class GtfsRepository:
    def __init__(self) -> None:
        self.timeline_cache: dict[int, list[StopTimeRow]] = {}
        self.shape_cache: dict[int, list[ShapePoint]] = {}
        self.freq_cache: dict[int, list[FrequencyRow]] = {}

    def get_city_row(self, conn, city_code: str):
        row = conn.execute(
            """
            SELECT city_id, city_name
            FROM cities
            WHERE city_code = %s
            """,
            (city_code,),
        ).fetchone()

        if row is None:
            raise RuntimeError(f"City not found in database: {city_code}")

        return int(row[0]), str(row[1])

    def get_active_service_pks(self, conn, city_id: int, target_date: date) -> set[int]:
        # Auto-detect if the feed's calendar dates are systematically in the future.
        # Cache result per city+date to avoid querying every simulation cycle.
        cache_key = (city_id, target_date.isoformat())
        if not hasattr(self, "_year_shift_cache"):
            self._year_shift_cache: dict = {}

        if cache_key not in self._year_shift_cache:
            try:
                median_row = conn.execute(
                    """
                    SELECT cd.date
                    FROM calendar_dates cd
                    JOIN services s ON s.service_pk = cd.service_pk
                    WHERE s.city_id = %s
                    ORDER BY cd.date
                    OFFSET (
                        SELECT COUNT(*) / 2
                        FROM calendar_dates cd2
                        JOIN services s2 ON s2.service_pk = cd2.service_pk
                        WHERE s2.city_id = %s
                    )
                    LIMIT 1
                    """,
                    (city_id, city_id),
                ).fetchone()
                if median_row:
                    from datetime import datetime as _dt
                    median_date = _dt.strptime(str(median_row[0]), "%Y%m%d").date()
                    self._year_shift_cache[cache_key] = (median_date - target_date).days > 180
                else:
                    self._year_shift_cache[cache_key] = False
            except Exception:
                self._year_shift_cache[cache_key] = False

        if self._year_shift_cache[cache_key]:
            target_date = target_date.replace(year=target_date.year + 1)

        gtfs_date = target_date.strftime("%Y%m%d")
        weekday_name = target_date.strftime("%A").lower()


        valid_columns = {
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        }
        if weekday_name not in valid_columns:
            raise RuntimeError(f"Unexpected weekday column: {weekday_name}")

        base_rows = conn.execute(
            f"""
            SELECT s.service_pk
            FROM services s
            JOIN calendar c ON c.service_pk = s.service_pk
            WHERE s.city_id = %s
              AND c.start_date <= %s
              AND c.end_date >= %s
              AND c.{weekday_name} = 1
            """,
            (city_id, gtfs_date, gtfs_date),
        ).fetchall()

        active = {int(r[0]) for r in base_rows}

        exception_rows = conn.execute(
            """
            SELECT s.service_pk, cd.exception_type
            FROM services s
            JOIN calendar_dates cd ON cd.service_pk = s.service_pk
            WHERE s.city_id = %s
              AND cd.date = %s
            """,
            (city_id, gtfs_date),
        ).fetchall()

        for service_pk, exception_type in exception_rows:
            service_pk = int(service_pk)
            exception_type = int(exception_type)

            if exception_type == 1:
                active.add(service_pk)
            elif exception_type == 2:
                active.discard(service_pk)

        return active

    def get_all_trips(self, conn, city_id: int, active_service_pks: set[int]) -> list[TripRow]:
        if not active_service_pks:
            return []

        rows = conn.execute(
            """
            SELECT
                t.trip_pk,
                t.trip_id,
                r.route_pk,
                r.route_id,
                s.service_pk,
                s.service_id,
                sh.shape_pk,
                sh.shape_id
            FROM trips t
            JOIN routes r ON t.route_pk = r.route_pk
            JOIN services s ON t.service_pk = s.service_pk
            LEFT JOIN shapes sh ON t.shape_pk = sh.shape_pk
            WHERE t.city_id = %s
              AND t.service_pk = ANY(%s)
            ORDER BY t.trip_id
            """,
            (city_id, list(active_service_pks)),
        ).fetchall()

        return [
            TripRow(
                trip_pk=int(r[0]),
                trip_id=str(r[1]),
                route_pk=int(r[2]),
                route_id=str(r[3]),
                service_pk=int(r[4]),
                service_id=str(r[5]),
                shape_pk=int(r[6]) if r[6] is not None else None,
                shape_id=str(r[7]) if r[7] is not None else None,
            )
            for r in rows
        ]


    def get_active_window_trips(
        self,
        conn,
        city_id: int,
        active_service_pks: set[int],
        current_time_str: str,
        lookbehind_seconds: int,
        lookahead_seconds: int,
    ) -> list[TripRow]:
        if not active_service_pks:
            return []

        now_seconds = hhmmss_to_seconds(current_time_str)
        lower_bound = now_seconds - lookbehind_seconds
        upper_bound = now_seconds + lookahead_seconds

        rows = conn.execute(
            """
            SELECT
                t.trip_pk,
                t.trip_id,
                r.route_pk,
                r.route_id,
                s.service_pk,
                s.service_id,
                sh.shape_pk,
                sh.shape_id
            FROM trips t
            JOIN routes r ON t.route_pk = r.route_pk
            JOIN services s ON t.service_pk = s.service_pk
            LEFT JOIN shapes sh ON t.shape_pk = sh.shape_pk
            WHERE t.city_id = %s
              AND t.service_pk = ANY(%s)
              AND t.first_departure_seconds <= %s
              AND t.last_arrival_seconds >= %s
            ORDER BY t.trip_id
            """,
            (
                city_id,
                list(active_service_pks),
                upper_bound,
                lower_bound,
            ),
        ).fetchall()

        return [
            TripRow(
                trip_pk=int(r[0]),
                trip_id=str(r[1]),
                route_pk=int(r[2]),
                route_id=str(r[3]),
                service_pk=int(r[4]),
                service_id=str(r[5]),
                shape_pk=int(r[6]) if r[6] is not None else None,
                shape_id=str(r[7]) if r[7] is not None else None,
            )
            for r in rows
        ]

    def get_trip_timeline(self, conn, trip_pk: int) -> list[StopTimeRow]:
        if trip_pk in self.timeline_cache:
            return self.timeline_cache[trip_pk]

        rows = conn.execute(
            """
            SELECT
                st.stop_sequence,
                s.stop_id,
                s.stop_name,
                s.stop_lat,
                s.stop_lon,
                st.arrival_time,
                st.departure_time,
                st.shape_dist_traveled
            FROM stop_times st
            JOIN stops s ON st.stop_pk = s.stop_pk
            WHERE st.trip_pk = %s
            ORDER BY st.stop_sequence
            """,
            (trip_pk,),
        ).fetchall()

        result = [
            StopTimeRow(
                stop_sequence=int(r[0]),
                stop_id=str(r[1]),
                stop_name=str(r[2]),
                stop_lat=float(r[3]),
                stop_lon=float(r[4]),
                arrival_time=str(r[5]),
                departure_time=str(r[6]),
                shape_dist_traveled=float(r[7]) if r[7] is not None else None,
            )
            for r in rows
        ]
        self.timeline_cache[trip_pk] = result
        return result

    def get_shape_points(self, conn, shape_pk: int) -> list[ShapePoint]:
        if shape_pk in self.shape_cache:
            return self.shape_cache[shape_pk]

        rows = conn.execute(
            """
            SELECT shape_pt_lat, shape_pt_lon, shape_pt_sequence, shape_dist_traveled
            FROM shape_points
            WHERE shape_pk = %s
            ORDER BY shape_pt_sequence
            """,
            (shape_pk,),
        ).fetchall()

        result = [
            ShapePoint(
                lat=float(r[0]),
                lon=float(r[1]),
                seq=int(r[2]),
                shape_dist_traveled=float(r[3]) if r[3] is not None else None,
            )
            for r in rows
        ]
        self.shape_cache[shape_pk] = result
        return result

    def get_frequencies(self, conn, trip_pk: int) -> list[FrequencyRow]:
        if trip_pk in self.freq_cache:
            return self.freq_cache[trip_pk]

        rows = conn.execute(
            """
            SELECT start_time, end_time, headway_secs
            FROM frequencies
            WHERE trip_pk = %s
            ORDER BY start_time
            """,
            (trip_pk,),
        ).fetchall()

        result = [FrequencyRow(str(r[0]), str(r[1]), int(r[2])) for r in rows]
        self.freq_cache[trip_pk] = result
        return result

    def expand_trip_instances(self, conn, trip_row: TripRow, current_time_str: str):
        frequencies = self.get_frequencies(conn, trip_row.trip_pk)

        if not frequencies:
            return [
                {
                    "trip_instance_id": trip_row.trip_id,
                    "trip_pk": trip_row.trip_pk,
                    "trip_id": trip_row.trip_id,
                    "route_pk": trip_row.route_pk,
                    "route_id": trip_row.route_id,
                    "service_pk": trip_row.service_pk,
                    "service_id": trip_row.service_id,
                    "shape_pk": trip_row.shape_pk,
                    "shape_id": trip_row.shape_id,
                    "time_shift_secs": 0,
                }
            ]

        instances = []
        now = hhmmss_to_seconds(current_time_str)

        for f in frequencies:
            start = hhmmss_to_seconds(f.start_time)
            end = hhmmss_to_seconds(f.end_time)
            headway = f.headway_secs

            departure = start
            while departure <= end:
                if departure - 7200 <= now <= departure + 7200:
                    instances.append(
                        {
                            "trip_instance_id": f"{trip_row.trip_id}@{departure}",
                            "trip_pk": trip_row.trip_pk,
                            "trip_id": trip_row.trip_id,
                            "route_pk": trip_row.route_pk,
                            "route_id": trip_row.route_id,
                            "service_pk": trip_row.service_pk,
                            "service_id": trip_row.service_id,
                            "shape_pk": trip_row.shape_pk,
                            "shape_id": trip_row.shape_id,
                            "time_shift_secs": departure - start,
                        }
                    )
                departure += headway

        return instances

    def replace_vehicle_states(self, conn, city_id: int, vehicles: list[dict], updated_at):
        conn.execute("DELETE FROM vehicle_states_current WHERE city_id = %s", (city_id,))

        for vehicle in vehicles:
            conn.execute(
                """
                INSERT INTO vehicle_states_current (
                    city_id, trip_instance_id,
                    trip_pk, route_pk, service_pk, shape_pk,
                    trip_id, route_id, service_id, shape_id,
                    status, source, lat, lon, trip_progress, updated_at
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                    updated_at=EXCLUDED.updated_at
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
                ),
            )

    def warm_caches(self, conn, trip_pks: list[int], shape_pks: list[int]) -> None:
        """Bulk-load timelines and shapes for all given pks in two queries.

        Call once at startup after the active trip list is known. Subsequent
        per-trip calls to get_trip_timeline / get_shape_points will hit the
        in-memory cache and make no DB round-trips.
        """
        # --- Timelines -------------------------------------------------
        uncached_trip_pks = [pk for pk in trip_pks if pk not in self.timeline_cache]
        if uncached_trip_pks:
            rows = conn.execute(
                """
                SELECT
                    st.trip_pk,
                    st.stop_sequence,
                    s.stop_id,
                    s.stop_name,
                    s.stop_lat,
                    s.stop_lon,
                    st.arrival_time,
                    st.departure_time,
                    st.shape_dist_traveled
                FROM stop_times st
                JOIN stops s ON st.stop_pk = s.stop_pk
                WHERE st.trip_pk = ANY(%s)
                ORDER BY st.trip_pk, st.stop_sequence
                """,
                (uncached_trip_pks,),
            ).fetchall()

            from collections import defaultdict
            by_trip: dict[int, list] = defaultdict(list)
            for r in rows:
                by_trip[int(r[0])].append(r)

            for trip_pk, trip_rows in by_trip.items():
                self.timeline_cache[trip_pk] = [
                    StopTimeRow(
                        stop_sequence=int(r[1]),
                        stop_id=str(r[2]),
                        stop_name=str(r[3]),
                        stop_lat=float(r[4]),
                        stop_lon=float(r[5]),
                        arrival_time=str(r[6]),
                        departure_time=str(r[7]),
                        shape_dist_traveled=float(r[8]) if r[8] is not None else None,
                    )
                    for r in trip_rows
                ]
            # Trips with no stop_times get an empty list so cache hits still work
            for pk in uncached_trip_pks:
                if pk not in self.timeline_cache:
                    self.timeline_cache[pk] = []

        # --- Shapes ----------------------------------------------------
        uncached_shape_pks = [pk for pk in shape_pks if pk not in self.shape_cache]
        if uncached_shape_pks:
            rows = conn.execute(
                """
                SELECT shape_pk, shape_pt_lat, shape_pt_lon, shape_pt_sequence, shape_dist_traveled
                FROM shape_points
                WHERE shape_pk = ANY(%s)
                ORDER BY shape_pk, shape_pt_sequence
                """,
                (uncached_shape_pks,),
            ).fetchall()

            from collections import defaultdict
            by_shape: dict[int, list] = defaultdict(list)
            for r in rows:
                by_shape[int(r[0])].append(r)

            for shape_pk, shape_rows in by_shape.items():
                self.shape_cache[shape_pk] = [
                    ShapePoint(
                        lat=float(r[1]),
                        lon=float(r[2]),
                        seq=int(r[3]),
                        shape_dist_traveled=float(r[4]) if r[4] is not None else None,
                    )
                    for r in shape_rows
                ]
            for pk in uncached_shape_pks:
                if pk not in self.shape_cache:
                    self.shape_cache[pk] = []
