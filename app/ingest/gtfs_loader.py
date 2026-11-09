#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import hashlib
import io
import os
import sys
import zipfile
from pathlib import Path
from typing import Iterable

import psycopg
from dotenv import load_dotenv


def load_db_config() -> dict:
    load_dotenv()
    cfg = {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": os.getenv("DB_NAME", "transit_db"),
        "user": os.getenv("DB_USER", "transit_user"),
        "password": os.getenv("DB_PASSWORD"),
    }
    if not cfg["password"]:
        raise RuntimeError("DB_PASSWORD is not set in .env")
    return cfg


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_csv_from_zip(zf: zipfile.ZipFile, filename: str) -> Iterable[dict]:
    with zf.open(filename) as raw_file:
        text_file = io.TextIOWrapper(raw_file, encoding="utf-8-sig", newline="")
        reader = csv.DictReader(text_file)
        for row in reader:
            yield row


def has_file(zf: zipfile.ZipFile, filename: str) -> bool:
    return filename in set(zf.namelist())


def validate_required_files(zf: zipfile.ZipFile) -> None:
    required = {"stops.txt", "routes.txt", "trips.txt", "stop_times.txt", "shapes.txt"}
    present = set(zf.namelist())
    missing = required - present
    if missing:
        raise FileNotFoundError(f"Missing required GTFS files: {', '.join(sorted(missing))}")


def int_or_none(value):
    if value in (None, ""):
        return None
    return int(value)


def float_or_none(value):
    if value in (None, ""):
        return None
    return float(value)


def time_to_seconds(t: str | None) -> int | None:
    """Convert HH:MM:SS (including >24h) to total seconds. Returns None on bad input."""
    if not t:
        return None
    parts = t.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None


def get_or_create_city(conn, city_code: str, city_name: str) -> int:
    conn.execute(
        """
        INSERT INTO cities (city_code, city_name)
        VALUES (%s, %s)
        ON CONFLICT (city_code) DO UPDATE
        SET city_name = EXCLUDED.city_name
        """,
        (city_code, city_name),
    )
    row = conn.execute(
        "SELECT city_id FROM cities WHERE city_code = %s",
        (city_code,),
    ).fetchone()
    return int(row[0])


def start_import_run(conn, city_id: int, feed_hash: str) -> int:
    row = conn.execute(
        """
        INSERT INTO gtfs_import_runs (city_id, status, feed_hash)
        VALUES (%s, 'running', %s)
        RETURNING import_id
        """,
        (city_id, feed_hash),
    ).fetchone()
    return int(row[0])


def finish_import_run_success(conn, import_id: int) -> None:
    conn.execute(
        """
        UPDATE gtfs_import_runs
        SET status='completed', finished_at=NOW(), error_message=NULL
        WHERE import_id=%s
        """,
        (import_id,),
    )


def finish_import_run_failure(db_config: dict, import_id: int, error_message: str) -> None:
    try:
        with psycopg.connect(**db_config) as conn:
            conn.execute(
                """
                UPDATE gtfs_import_runs
                SET status='failed', finished_at=NOW(), error_message=%s
                WHERE import_id=%s
                """,
                (error_message[:2000], import_id),
            )
            conn.commit()
    except Exception:
        pass


def clear_runtime_state(conn, city_id: int) -> None:
    """Clear only live runtime vehicle state before a GTFS refresh."""
    conn.execute("DELETE FROM vehicle_states_current WHERE city_id = %s", (city_id,))


def _safe_commit(conn) -> None:
    """Commit when the connection is transactional.

    The GTFS loader normally runs with autocommit disabled, but this helper keeps
    pruning compatible with autocommit connections too.
    """
    if not getattr(conn, "autocommit", False):
        conn.commit()


def _copy_text_values(cur, table_name: str, column_name: str, values: Iterable[str]) -> int:
    unique_values = [v for v in dict.fromkeys(values) if v]
    cur.execute(f"CREATE TEMP TABLE {table_name} ({column_name} TEXT PRIMARY KEY) ON COMMIT PRESERVE ROWS")
    if unique_values:
        with cur.copy(f"COPY {table_name} ({column_name}) FROM STDIN") as copy:
            for value in unique_values:
                copy.write_row((value,))
    return len(unique_values)


def _delete_in_batches(conn, label: str, sql: str, params: dict, batch_size: int) -> int:
    """Run a bounded DELETE repeatedly and commit between batches.

    This prevents one very large DELETE from holding locks/WAL/rollback state for
    a long time. Each SQL statement must use a `%(batch_size)s` placeholder and
    delete at most that many rows.
    """
    total = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(sql, {**params, "batch_size": batch_size})
            deleted = cur.rowcount if cur.rowcount is not None else 0
        if deleted <= 0:
            break
        total += deleted
        _safe_commit(conn)
        print(f"Pruned stale {label}: +{deleted} total={total}", flush=True)
    return total


def prune_stale_rows(conn, city_id: int, seen_ids: dict, batch_size: int | None = None) -> None:
    """Delete stale GTFS rows child-to-parent and in small batches.

    Important: never delete directly from parent tables such as services/routes/stops
    and rely on a huge ON DELETE CASCADE. Large feeds such as SFBA can have millions
    of stop_times, so parent deletes can run for a very long time and keep the import
    stuck at "Pruning stale rows...".
    """
    batch_size = batch_size or int(os.getenv("GTFS_PRUNE_BATCH_SIZE", "1000"))
    batch_size = max(1, batch_size)

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS _seen_trips")
        cur.execute("DROP TABLE IF EXISTS _seen_shapes")
        cur.execute("DROP TABLE IF EXISTS _seen_services")
        cur.execute("DROP TABLE IF EXISTS _seen_routes")
        cur.execute("DROP TABLE IF EXISTS _seen_stops")

        if seen_ids.get("trip_ids") is not None:
            count = _copy_text_values(cur, "_seen_trips", "trip_id", seen_ids["trip_ids"])
            print(f"Seen trip ids for pruning: {count}", flush=True)

        if seen_ids.get("shape_ids") is not None:
            count = _copy_text_values(cur, "_seen_shapes", "shape_id", seen_ids["shape_ids"])
            print(f"Seen shape ids for pruning: {count}", flush=True)

        if seen_ids.get("service_ids") is not None:
            count = _copy_text_values(cur, "_seen_services", "service_id", seen_ids["service_ids"])
            print(f"Seen service ids for pruning: {count}", flush=True)

        if seen_ids.get("route_ids") is not None:
            count = _copy_text_values(cur, "_seen_routes", "route_id", seen_ids["route_ids"])
            print(f"Seen route ids for pruning: {count}", flush=True)

        if seen_ids.get("stop_ids") is not None:
            count = _copy_text_values(cur, "_seen_stops", "stop_id", seen_ids["stop_ids"])
            print(f"Seen stop ids for pruning: {count}", flush=True)

    _safe_commit(conn)

    params = {"city_id": city_id}

    if seen_ids.get("trip_ids") is not None:
        _delete_in_batches(conn, "frequencies rows", """
            WITH batch AS (
                SELECT f.frequency_pk
                FROM frequencies f
                JOIN trips t ON t.trip_pk = f.trip_pk
                LEFT JOIN _seen_trips s ON s.trip_id = t.trip_id
                WHERE t.city_id = %(city_id)s
                  AND s.trip_id IS NULL
                LIMIT %(batch_size)s
            )
            DELETE FROM frequencies f
            USING batch
            WHERE f.frequency_pk = batch.frequency_pk
        """, params, batch_size)

        _delete_in_batches(conn, "stop_times rows", """
            WITH batch AS (
                SELECT st.stop_time_pk
                FROM stop_times st
                JOIN trips t ON t.trip_pk = st.trip_pk
                LEFT JOIN _seen_trips s ON s.trip_id = t.trip_id
                WHERE t.city_id = %(city_id)s
                  AND s.trip_id IS NULL
                LIMIT %(batch_size)s
            )
            DELETE FROM stop_times st
            USING batch
            WHERE st.stop_time_pk = batch.stop_time_pk
        """, params, batch_size)

        # Avoid expensive FK-trigger SET NULL work during trip deletes. These tables
        # should not block deleting stale static trips; they are history/cache data.
        _delete_in_batches(conn, "vehicle_states_current trip links", """
            WITH batch AS (
                SELECT v.vehicle_state_pk
                FROM vehicle_states_current v
                JOIN trips t ON t.trip_pk = v.trip_pk
                LEFT JOIN _seen_trips s ON s.trip_id = t.trip_id
                WHERE v.city_id = %(city_id)s
                  AND s.trip_id IS NULL
                LIMIT %(batch_size)s
            )
            UPDATE vehicle_states_current v
            SET trip_pk = NULL
            FROM batch
            WHERE v.vehicle_state_pk = batch.vehicle_state_pk
        """, params, batch_size)

        _delete_in_batches(conn, "vehicle_states_simulated_current trip links", """
            WITH batch AS (
                SELECT v.vehicle_state_pk
                FROM vehicle_states_simulated_current v
                JOIN trips t ON t.trip_pk = v.trip_pk
                LEFT JOIN _seen_trips s ON s.trip_id = t.trip_id
                WHERE v.city_id = %(city_id)s
                  AND s.trip_id IS NULL
                LIMIT %(batch_size)s
            )
            UPDATE vehicle_states_simulated_current v
            SET trip_pk = NULL
            FROM batch
            WHERE v.vehicle_state_pk = batch.vehicle_state_pk
        """, params, batch_size)

        _delete_in_batches(conn, "gtfs_rt_vehicle_positions trip links", """
            WITH batch AS (
                SELECT vp.rt_vehicle_pk
                FROM gtfs_rt_vehicle_positions vp
                JOIN trips t ON t.trip_pk = vp.trip_pk
                LEFT JOIN _seen_trips s ON s.trip_id = t.trip_id
                WHERE vp.city_id = %(city_id)s
                  AND s.trip_id IS NULL
                LIMIT %(batch_size)s
            )
            UPDATE gtfs_rt_vehicle_positions vp
            SET trip_pk = NULL
            FROM batch
            WHERE vp.rt_vehicle_pk = batch.rt_vehicle_pk
        """, params, batch_size)

        _delete_in_batches(conn, "gtfs_rt_trip_updates trip links", """
            WITH batch AS (
                SELECT tu.rt_trip_update_pk
                FROM gtfs_rt_trip_updates tu
                JOIN trips t ON t.trip_pk = tu.trip_pk
                LEFT JOIN _seen_trips s ON s.trip_id = t.trip_id
                WHERE tu.city_id = %(city_id)s
                  AND s.trip_id IS NULL
                LIMIT %(batch_size)s
            )
            UPDATE gtfs_rt_trip_updates tu
            SET trip_pk = NULL
            FROM batch
            WHERE tu.rt_trip_update_pk = batch.rt_trip_update_pk
        """, params, batch_size)

        _delete_in_batches(conn, "gtfs_rt_trip_updates_current trip links", """
            WITH batch AS (
                SELECT tuc.city_id, tuc.trip_key
                FROM gtfs_rt_trip_updates_current tuc
                JOIN trips t ON t.trip_pk = tuc.trip_pk
                LEFT JOIN _seen_trips s ON s.trip_id = t.trip_id
                WHERE tuc.city_id = %(city_id)s
                  AND s.trip_id IS NULL
                LIMIT %(batch_size)s
            )
            UPDATE gtfs_rt_trip_updates_current tuc
            SET trip_pk = NULL
            FROM batch
            WHERE tuc.city_id = batch.city_id
              AND tuc.trip_key = batch.trip_key
        """, params, batch_size)

        _delete_in_batches(conn, "stale trips rows", """
            WITH batch AS (
                SELECT t.trip_pk
                FROM trips t
                LEFT JOIN _seen_trips s ON s.trip_id = t.trip_id
                WHERE t.city_id = %(city_id)s
                  AND s.trip_id IS NULL
                LIMIT %(batch_size)s
            )
            DELETE FROM trips t
            USING batch
            WHERE t.trip_pk = batch.trip_pk
        """, params, batch_size)

    if seen_ids.get("service_ids") is not None:
        _delete_in_batches(conn, "calendar_dates rows", """
            WITH batch AS (
                SELECT cd.calendar_date_pk
                FROM calendar_dates cd
                JOIN services sv ON sv.service_pk = cd.service_pk
                LEFT JOIN _seen_services s ON s.service_id = sv.service_id
                WHERE sv.city_id = %(city_id)s
                  AND s.service_id IS NULL
                LIMIT %(batch_size)s
            )
            DELETE FROM calendar_dates cd
            USING batch
            WHERE cd.calendar_date_pk = batch.calendar_date_pk
        """, params, batch_size)

        _delete_in_batches(conn, "calendar rows", """
            WITH batch AS (
                SELECT c.service_pk
                FROM calendar c
                JOIN services sv ON sv.service_pk = c.service_pk
                LEFT JOIN _seen_services s ON s.service_id = sv.service_id
                WHERE sv.city_id = %(city_id)s
                  AND s.service_id IS NULL
                LIMIT %(batch_size)s
            )
            DELETE FROM calendar c
            USING batch
            WHERE c.service_pk = batch.service_pk
        """, params, batch_size)

        _delete_in_batches(conn, "stale services rows", """
            WITH batch AS (
                SELECT sv.service_pk
                FROM services sv
                LEFT JOIN _seen_services s ON s.service_id = sv.service_id
                WHERE sv.city_id = %(city_id)s
                  AND s.service_id IS NULL
                LIMIT %(batch_size)s
            )
            DELETE FROM services sv
            USING batch
            WHERE sv.service_pk = batch.service_pk
        """, params, batch_size)

    if seen_ids.get("shape_ids") is not None:
        _delete_in_batches(conn, "shape_points rows", """
            WITH batch AS (
                SELECT sp.shape_point_pk
                FROM shape_points sp
                JOIN shapes sh ON sh.shape_pk = sp.shape_pk
                LEFT JOIN _seen_shapes s ON s.shape_id = sh.shape_id
                WHERE sh.city_id = %(city_id)s
                  AND s.shape_id IS NULL
                LIMIT %(batch_size)s
            )
            DELETE FROM shape_points sp
            USING batch
            WHERE sp.shape_point_pk = batch.shape_point_pk
        """, params, batch_size)

        _delete_in_batches(conn, "stale shapes rows", """
            WITH batch AS (
                SELECT sh.shape_pk
                FROM shapes sh
                LEFT JOIN _seen_shapes s ON s.shape_id = sh.shape_id
                WHERE sh.city_id = %(city_id)s
                  AND s.shape_id IS NULL
                LIMIT %(batch_size)s
            )
            DELETE FROM shapes sh
            USING batch
            WHERE sh.shape_pk = batch.shape_pk
        """, params, batch_size)

    if seen_ids.get("route_ids") is not None:
        _delete_in_batches(conn, "stale routes rows", """
            WITH batch AS (
                SELECT r.route_pk
                FROM routes r
                LEFT JOIN _seen_routes s ON s.route_id = r.route_id
                WHERE r.city_id = %(city_id)s
                  AND s.route_id IS NULL
                LIMIT %(batch_size)s
            )
            DELETE FROM routes r
            USING batch
            WHERE r.route_pk = batch.route_pk
        """, params, batch_size)

    if seen_ids.get("stop_ids") is not None:
        _delete_in_batches(conn, "stale stops rows", """
            WITH batch AS (
                SELECT stp.stop_pk
                FROM stops stp
                LEFT JOIN _seen_stops s ON s.stop_id = stp.stop_id
                WHERE stp.city_id = %(city_id)s
                  AND s.stop_id IS NULL
                LIMIT %(batch_size)s
            )
            DELETE FROM stops stp
            USING batch
            WHERE stp.stop_pk = batch.stop_pk
        """, params, batch_size)

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS _seen_trips")
        cur.execute("DROP TABLE IF EXISTS _seen_shapes")
        cur.execute("DROP TABLE IF EXISTS _seen_services")
        cur.execute("DROP TABLE IF EXISTS _seen_routes")
        cur.execute("DROP TABLE IF EXISTS _seen_stops")
    _safe_commit(conn)

def import_stops(conn, city_id: int, rows: Iterable[dict], batch_size: int = 5000) -> int:
    count = 0
    batch = []

    sql = """
        INSERT INTO stops (
            city_id, stop_id, stop_code, stop_name, stop_desc,
            stop_lat, stop_lon, location_type, parent_station_id,
            zone_id, stop_url, stop_timezone, wheelchair_boarding
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (city_id, stop_id) DO UPDATE
        SET stop_code=EXCLUDED.stop_code,
            stop_name=EXCLUDED.stop_name,
            stop_desc=EXCLUDED.stop_desc,
            stop_lat=EXCLUDED.stop_lat,
            stop_lon=EXCLUDED.stop_lon,
            location_type=EXCLUDED.location_type,
            parent_station_id=EXCLUDED.parent_station_id,
            zone_id=EXCLUDED.zone_id,
            stop_url=EXCLUDED.stop_url,
            stop_timezone=EXCLUDED.stop_timezone,
            wheelchair_boarding=EXCLUDED.wheelchair_boarding
    """

    def flush():
        nonlocal batch
        if batch:
            with conn.cursor() as cur:
                cur.executemany(sql, batch)
            batch = []

    for row in rows:
        stop_id = row.get("stop_id")
        stop_name = row.get("stop_name")
        stop_lat = row.get("stop_lat")
        stop_lon = row.get("stop_lon")

        if not stop_id or not stop_name or not stop_lat or not stop_lon:
            continue

        batch.append(
            (
                city_id,
                stop_id,
                row.get("stop_code"),
                stop_name,
                row.get("stop_desc"),
                float(stop_lat),
                float(stop_lon),
                int_or_none(row.get("location_type")),
                row.get("parent_station"),
                row.get("zone_id"),
                row.get("stop_url"),
                row.get("stop_timezone"),
                int_or_none(row.get("wheelchair_boarding")),
            )
        )
        count += 1

        if len(batch) >= batch_size:
            flush()
            print(f"Imported stops rows: {count}", flush=True)

    flush()

    conn.execute(
        """
        UPDATE stops child
        SET parent_stop_pk = parent.stop_pk
        FROM stops parent
        WHERE child.city_id = %s
          AND parent.city_id = child.city_id
          AND child.parent_station_id IS NOT NULL
          AND parent.stop_id = child.parent_station_id
        """,
        (city_id,),
    )

    return count


def import_routes(conn, city_id: int, rows: Iterable[dict], batch_size: int = 5000) -> int:
    count = 0
    batch = []

    sql = """
        INSERT INTO routes (
            city_id, route_id, agency_id, route_short_name, route_long_name,
            route_desc, route_type, route_color, route_text_color, route_url
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (city_id, route_id) DO UPDATE
        SET agency_id=EXCLUDED.agency_id,
            route_short_name=EXCLUDED.route_short_name,
            route_long_name=EXCLUDED.route_long_name,
            route_desc=EXCLUDED.route_desc,
            route_type=EXCLUDED.route_type,
            route_color=EXCLUDED.route_color,
            route_text_color=EXCLUDED.route_text_color,
            route_url=EXCLUDED.route_url
    """

    def flush():
        nonlocal batch
        if batch:
            with conn.cursor() as cur:
                cur.executemany(sql, batch)
            batch = []

    for row in rows:
        route_id = row.get("route_id")
        if not route_id:
            continue

        batch.append(
            (
                city_id,
                route_id,
                row.get("agency_id"),
                row.get("route_short_name"),
                row.get("route_long_name"),
                row.get("route_desc"),
                int_or_none(row.get("route_type")),
                row.get("route_color"),
                row.get("route_text_color"),
                row.get("route_url"),
            )
        )
        count += 1

        if len(batch) >= batch_size:
            flush()
            print(f"Imported routes rows: {count}", flush=True)

    flush()
    return count


def ensure_services(conn, city_id: int, service_ids: set[str], batch_size: int = 5000) -> None:
    if not service_ids:
        return

    sql = """
        INSERT INTO services (city_id, service_id)
        VALUES (%s, %s)
        ON CONFLICT (city_id, service_id) DO NOTHING
    """

    batch = [(city_id, service_id) for service_id in sorted(service_ids)]

    with conn.cursor() as cur:
        for i in range(0, len(batch), batch_size):
            cur.executemany(sql, batch[i:i + batch_size])


def ensure_shapes(conn, city_id: int, shape_ids: set[str], batch_size: int = 5000) -> None:
    if not shape_ids:
        return

    sql = """
        INSERT INTO shapes (city_id, shape_id)
        VALUES (%s, %s)
        ON CONFLICT (city_id, shape_id) DO NOTHING
    """

    batch = [(city_id, shape_id) for shape_id in sorted(shape_ids) if shape_id]

    with conn.cursor() as cur:
        for i in range(0, len(batch), batch_size):
            cur.executemany(sql, batch[i:i + batch_size])


def build_pk_map(conn, query: str, params: tuple) -> dict[str, int]:
    rows = conn.execute(query, params).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def import_calendar(conn, city_id: int, rows: Iterable[dict], service_pk_by_service_id: dict[str, int], batch_size: int = 5000) -> int:
    count = 0
    batch = []

    sql = """
        INSERT INTO calendar (
            service_pk, monday, tuesday, wednesday, thursday,
            friday, saturday, sunday, start_date, end_date
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (service_pk) DO UPDATE
        SET monday=EXCLUDED.monday,
            tuesday=EXCLUDED.tuesday,
            wednesday=EXCLUDED.wednesday,
            thursday=EXCLUDED.thursday,
            friday=EXCLUDED.friday,
            saturday=EXCLUDED.saturday,
            sunday=EXCLUDED.sunday,
            start_date=EXCLUDED.start_date,
            end_date=EXCLUDED.end_date
    """

    def flush():
        nonlocal batch
        if batch:
            with conn.cursor() as cur:
                cur.executemany(sql, batch)
            batch = []

    for row in rows:
        service_id = row.get("service_id")
        if not service_id:
            continue

        service_pk = service_pk_by_service_id.get(service_id)
        if not service_pk:
            continue

        batch.append(
            (
                service_pk,
                int(row["monday"]),
                int(row["tuesday"]),
                int(row["wednesday"]),
                int(row["thursday"]),
                int(row["friday"]),
                int(row["saturday"]),
                int(row["sunday"]),
                row["start_date"],
                row["end_date"],
            )
        )
        count += 1

        if len(batch) >= batch_size:
            flush()
            print(f"Imported calendar rows: {count}", flush=True)

    flush()
    return count


def import_calendar_dates(conn, city_id: int, rows: Iterable[dict], service_pk_by_service_id: dict[str, int], batch_size: int = 5000) -> int:
    count = 0
    batch = []

    sql = """
        INSERT INTO calendar_dates (service_pk, date, exception_type)
        VALUES (%s, %s, %s)
        ON CONFLICT (service_pk, date) DO UPDATE
        SET exception_type = EXCLUDED.exception_type
    """

    def flush():
        nonlocal batch
        if batch:
            with conn.cursor() as cur:
                cur.executemany(sql, batch)
            batch = []

    for row in rows:
        service_id = row.get("service_id")
        date_value = row.get("date")
        exception_type = row.get("exception_type")

        if not service_id or not date_value or not exception_type:
            continue

        service_pk = service_pk_by_service_id.get(service_id)
        if not service_pk:
            continue

        batch.append((service_pk, date_value, int(exception_type)))
        count += 1

        if len(batch) >= batch_size:
            flush()
            print(f"Imported calendar_dates rows: {count}", flush=True)

    flush()
    return count


def import_shape_points(conn, city_id: int, rows: Iterable[dict], shape_pk_by_shape_id: dict[str, int], batch_size: int = 50000) -> int:
    """Use COPY for shape_points — much faster than executemany for large feeds."""
    count = 0
    buffer: list[tuple] = []

    # Delete existing shape_points for this city before COPY (COPY doesn't support ON CONFLICT)
    conn.execute(
        """
        DELETE FROM shape_points
        WHERE shape_pk IN (SELECT shape_pk FROM shapes WHERE city_id = %s)
        """,
        (city_id,),
    )

    def flush():
        nonlocal buffer
        if not buffer:
            return
        with conn.cursor() as cur:
            with cur.copy(
                "COPY shape_points (shape_pk, shape_pt_sequence, shape_pt_lat, shape_pt_lon, shape_dist_traveled) FROM STDIN"
            ) as copy:
                for record in buffer:
                    copy.write_row(record)
        buffer = []

    for row in rows:
        shape_id = row.get("shape_id")
        lat = row.get("shape_pt_lat")
        lon = row.get("shape_pt_lon")
        seq = row.get("shape_pt_sequence")

        if not shape_id or not lat or not lon or not seq:
            continue

        shape_pk = shape_pk_by_shape_id.get(shape_id)
        if not shape_pk:
            continue

        buffer.append(
            (
                shape_pk,
                int(seq),
                float(lat),
                float(lon),
                float_or_none(row.get("shape_dist_traveled")),
            )
        )
        count += 1

        if len(buffer) >= batch_size:
            flush()
            print(f"Imported shape_points rows: {count}", flush=True)

    flush()
    return count


def import_trips(conn, city_id: int, rows: Iterable[dict], route_pk_by_route_id: dict[str, int], service_pk_by_service_id: dict[str, int], shape_pk_by_shape_id: dict[str, int], batch_size: int = 5000) -> int:
    count = 0
    batch = []

    sql = """
        INSERT INTO trips (
            city_id, trip_id, route_pk, service_pk, shape_pk,
            trip_headsign, trip_short_name, direction_id, block_id,
            wheelchair_accessible, bikes_allowed
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (city_id, trip_id) DO UPDATE
        SET route_pk=EXCLUDED.route_pk,
            service_pk=EXCLUDED.service_pk,
            shape_pk=EXCLUDED.shape_pk,
            trip_headsign=EXCLUDED.trip_headsign,
            trip_short_name=EXCLUDED.trip_short_name,
            direction_id=EXCLUDED.direction_id,
            block_id=EXCLUDED.block_id,
            wheelchair_accessible=EXCLUDED.wheelchair_accessible,
            bikes_allowed=EXCLUDED.bikes_allowed
    """

    def flush():
        nonlocal batch
        if batch:
            with conn.cursor() as cur:
                cur.executemany(sql, batch)
            batch = []

    for row in rows:
        trip_id = row.get("trip_id")
        route_id = row.get("route_id")
        service_id = row.get("service_id")

        if not trip_id or not route_id or not service_id:
            continue

        route_pk = route_pk_by_route_id.get(route_id)
        service_pk = service_pk_by_service_id.get(service_id)
        shape_pk = shape_pk_by_shape_id.get(row.get("shape_id")) if row.get("shape_id") else None

        if not route_pk or not service_pk:
            continue

        batch.append(
            (
                city_id,
                trip_id,
                route_pk,
                service_pk,
                shape_pk,
                row.get("trip_headsign"),
                row.get("trip_short_name"),
                int_or_none(row.get("direction_id")),
                row.get("block_id"),
                int_or_none(row.get("wheelchair_accessible")),
                int_or_none(row.get("bikes_allowed")),
            )
        )
        count += 1

        if len(batch) >= batch_size:
            flush()
            print(f"Imported trips rows: {count}", flush=True)

    flush()
    return count


def import_stop_times(conn, city_id: int, rows: Iterable[dict], trip_pk_by_trip_id: dict[str, int], stop_pk_by_stop_id: dict[str, int], batch_size: int = 50000) -> int:
    """Use COPY for stop_times and compute arrival/departure seconds inline.
    This eliminates the post-import UPDATE pass entirely."""
    count = 0
    buffer: list[tuple] = []

    # Delete existing stop_times for this city before COPY (COPY doesn't support ON CONFLICT)
    conn.execute(
        """
        DELETE FROM stop_times
        WHERE trip_pk IN (SELECT trip_pk FROM trips WHERE city_id = %s)
        """,
        (city_id,),
    )

    def flush():
        nonlocal buffer
        if not buffer:
            return
        with conn.cursor() as cur:
            with cur.copy(
                "COPY stop_times ("
                "  trip_pk, stop_pk, arrival_time, departure_time,"
                "  arrival_seconds, departure_seconds,"
                "  stop_sequence, stop_headsign, pickup_type, drop_off_type,"
                "  shape_dist_traveled, timepoint"
                ") FROM STDIN"
            ) as copy:
                for record in buffer:
                    copy.write_row(record)
        buffer = []

    for row in rows:
        trip_id = row.get("trip_id")
        stop_id = row.get("stop_id")
        stop_sequence = row.get("stop_sequence")

        if not trip_id or not stop_id or not stop_sequence:
            continue

        trip_pk = trip_pk_by_trip_id.get(trip_id)
        stop_pk = stop_pk_by_stop_id.get(stop_id)

        if not trip_pk or not stop_pk:
            continue

        arrival_time = row.get("arrival_time") or None
        departure_time = row.get("departure_time") or None

        buffer.append(
            (
                trip_pk,
                stop_pk,
                arrival_time,
                departure_time,
                time_to_seconds(arrival_time),
                time_to_seconds(departure_time),
                int(stop_sequence),
                row.get("stop_headsign") or None,
                int_or_none(row.get("pickup_type")),
                int_or_none(row.get("drop_off_type")),
                float_or_none(row.get("shape_dist_traveled")),
                int_or_none(row.get("timepoint")),
            )
        )
        count += 1

        if len(buffer) >= batch_size:
            flush()
            print(f"Imported stop_times rows: {count}", flush=True)

    flush()
    return count


def update_trip_time_windows(conn, city_id: int) -> None:
    """Update first_departure_seconds / last_arrival_seconds on trips from stop_times."""
    print("Updating trip time windows...", flush=True)
    conn.execute(
        """
        UPDATE trips t
        SET
            first_departure_seconds = x.first_departure_seconds,
            last_arrival_seconds    = x.last_arrival_seconds
        FROM (
            SELECT st.trip_pk,
                   MIN(st.departure_seconds) AS first_departure_seconds,
                   MAX(st.arrival_seconds)   AS last_arrival_seconds
            FROM stop_times st
            WHERE st.trip_pk IN (SELECT trip_pk FROM trips WHERE city_id = %s)
            GROUP BY st.trip_pk
        ) x
        WHERE t.trip_pk = x.trip_pk
        """,
        (city_id,),
    )
    print("Trip time windows updated.", flush=True)


def import_frequencies(conn, city_id: int, rows: Iterable[dict], trip_pk_by_trip_id: dict[str, int], batch_size: int = 5000) -> int:
    count = 0
    batch = []

    sql = """
        INSERT INTO frequencies (
            trip_pk, start_time, end_time, headway_secs, exact_times
        )
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (trip_pk, start_time, end_time, headway_secs) DO UPDATE
        SET exact_times = EXCLUDED.exact_times
    """

    def flush():
        nonlocal batch
        if batch:
            with conn.cursor() as cur:
                cur.executemany(sql, batch)
            batch = []

    for row in rows:
        trip_id = row.get("trip_id")
        start_time = row.get("start_time")
        end_time = row.get("end_time")
        headway_secs = row.get("headway_secs")

        if not trip_id or not start_time or not end_time or not headway_secs:
            continue

        trip_pk = trip_pk_by_trip_id.get(trip_id)
        if not trip_pk:
            continue

        batch.append(
            (
                trip_pk,
                start_time,
                end_time,
                int(headway_secs),
                int_or_none(row.get("exact_times")),
            )
        )
        count += 1

        if len(batch) >= batch_size:
            flush()
            print(f"Imported frequencies rows: {count}", flush=True)

    flush()
    return count


def collect_service_ids(zf: zipfile.ZipFile) -> set[str]:
    service_ids: set[str] = set()

    if has_file(zf, "trips.txt"):
        for row in read_csv_from_zip(zf, "trips.txt"):
            service_id = row.get("service_id")
            if service_id:
                service_ids.add(service_id)

    if has_file(zf, "calendar.txt"):
        for row in read_csv_from_zip(zf, "calendar.txt"):
            service_id = row.get("service_id")
            if service_id:
                service_ids.add(service_id)

    if has_file(zf, "calendar_dates.txt"):
        for row in read_csv_from_zip(zf, "calendar_dates.txt"):
            service_id = row.get("service_id")
            if service_id:
                service_ids.add(service_id)

    return service_ids


def collect_shape_ids(zf: zipfile.ZipFile) -> set[str]:
    shape_ids: set[str] = set()

    if has_file(zf, "trips.txt"):
        for row in read_csv_from_zip(zf, "trips.txt"):
            shape_id = row.get("shape_id")
            if shape_id:
                shape_ids.add(shape_id)

    if has_file(zf, "shapes.txt"):
        for row in read_csv_from_zip(zf, "shapes.txt"):
            shape_id = row.get("shape_id")
            if shape_id:
                shape_ids.add(shape_id)

    return shape_ids


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", required=True)
    parser.add_argument("--city-code", required=True)
    parser.add_argument("--city-name", required=True)
    parser.add_argument("--replace", action="store_true", help="Delete existing data for the city before importing")
    args = parser.parse_args()

    zip_path = Path(args.zip)
    if not zip_path.exists():
        print(f"Zip not found: {zip_path}")
        sys.exit(1)

    db_config = load_db_config()
    feed_hash = sha256_file(zip_path)
    import_id = None

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            validate_required_files(zf)

            with psycopg.connect(**db_config) as conn:
                city_id = get_or_create_city(conn, args.city_code, args.city_name)
                import_id = start_import_run(conn, city_id, feed_hash)

                if args.replace:
                    print("Clearing runtime state...", flush=True)
                    clear_runtime_state(conn, city_id)
                    print("Runtime state cleared.", flush=True)

                print("Importing stops...", flush=True)
                stops_rows = list(read_csv_from_zip(zf, "stops.txt"))
                seen_stop_ids = [r["stop_id"] for r in stops_rows]
                stops_count = import_stops(conn, city_id, iter(stops_rows))
                print(f"Imported stops: {stops_count}", flush=True)

                print("Importing routes...", flush=True)
                routes_rows = list(read_csv_from_zip(zf, "routes.txt"))
                seen_route_ids = [r["route_id"] for r in routes_rows]
                routes_count = import_routes(conn, city_id, iter(routes_rows))
                print(f"Imported routes: {routes_count}", flush=True)

                print("Ensuring services...", flush=True)
                service_ids = collect_service_ids(zf)
                ensure_services(conn, city_id, service_ids)
                print(f"Ensured services: {len(service_ids)}", flush=True)

                print("Ensuring shapes...", flush=True)
                shape_ids = collect_shape_ids(zf)
                ensure_shapes(conn, city_id, shape_ids)
                print(f"Ensured shapes: {len(shape_ids)}", flush=True)

                route_pk_by_route_id = build_pk_map(
                    conn,
                    "SELECT route_id, route_pk FROM routes WHERE city_id = %s",
                    (city_id,),
                )
                service_pk_by_service_id = build_pk_map(
                    conn,
                    "SELECT service_id, service_pk FROM services WHERE city_id = %s",
                    (city_id,),
                )
                shape_pk_by_shape_id = build_pk_map(
                    conn,
                    "SELECT shape_id, shape_pk FROM shapes WHERE city_id = %s",
                    (city_id,),
                )
                stop_pk_by_stop_id = build_pk_map(
                    conn,
                    "SELECT stop_id, stop_pk FROM stops WHERE city_id = %s",
                    (city_id,),
                )

                if has_file(zf, "calendar.txt"):
                    print("Importing calendar...", flush=True)
                    calendar_count = import_calendar(
                        conn,
                        city_id,
                        read_csv_from_zip(zf, "calendar.txt"),
                        service_pk_by_service_id,
                    )
                    print(f"Imported calendar rows: {calendar_count}", flush=True)

                if has_file(zf, "calendar_dates.txt"):
                    print("Importing calendar_dates...", flush=True)
                    calendar_dates_count = import_calendar_dates(
                        conn,
                        city_id,
                        read_csv_from_zip(zf, "calendar_dates.txt"),
                        service_pk_by_service_id,
                    )
                    print(f"Imported calendar_dates rows: {calendar_dates_count}", flush=True)

                print("Importing shape_points...", flush=True)
                shape_points_count = import_shape_points(
                    conn,
                    city_id,
                    read_csv_from_zip(zf, "shapes.txt"),
                    shape_pk_by_shape_id,
                )
                print(f"Imported shape_points: {shape_points_count}", flush=True)

                print("Importing trips...", flush=True)
                trips_rows = list(read_csv_from_zip(zf, "trips.txt"))
                seen_trip_ids = [r["trip_id"] for r in trips_rows if r.get("trip_id")]
                trips_count = import_trips(
                    conn,
                    city_id,
                    iter(trips_rows),
                    route_pk_by_route_id,
                    service_pk_by_service_id,
                    shape_pk_by_shape_id,
                )
                print(f"Imported trips: {trips_count}", flush=True)

                trip_pk_by_trip_id = build_pk_map(
                    conn,
                    "SELECT trip_id, trip_pk FROM trips WHERE city_id = %s",
                    (city_id,),
                )

                print("Importing stop_times...", flush=True)
                stop_times_count = import_stop_times(
                    conn,
                    city_id,
                    read_csv_from_zip(zf, "stop_times.txt"),
                    trip_pk_by_trip_id,
                    stop_pk_by_stop_id,
                )
                print(f"Imported stop_times: {stop_times_count}", flush=True)

                if has_file(zf, "frequencies.txt"):
                    print("Importing frequencies...", flush=True)
                    frequencies_count = import_frequencies(
                        conn,
                        city_id,
                        read_csv_from_zip(zf, "frequencies.txt"),
                        trip_pk_by_trip_id,
                    )
                    print(f"Imported frequencies rows: {frequencies_count}", flush=True)

                update_trip_time_windows(conn, city_id)

                # Save the heavy import before pruning. Pruning then runs in bounded
                # batches and commits between batches, so one stale cleanup cannot hold
                # a single giant transaction for very large feeds such as SFBA.
                conn.commit()

                if os.getenv("GTFS_PRUNE_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}:
                    print("Pruning stale rows...", flush=True)
                    prune_stale_rows(conn, city_id, {
                        "stop_ids": seen_stop_ids,
                        "route_ids": seen_route_ids,
                        "service_ids": list(service_ids),
                        "shape_ids": list(shape_ids),
                        "trip_ids": seen_trip_ids,
                    })
                    print("Stale rows pruned.", flush=True)
                else:
                    print("Skipping stale row pruning because GTFS_PRUNE_ENABLED is disabled.", flush=True)

                finish_import_run_success(conn, import_id)
                conn.commit()
                print("Import completed successfully.", flush=True)

    except Exception as e:
        if import_id is not None:
            finish_import_run_failure(db_config, import_id, str(e))
        print(f"Import failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
