import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import psycopg


PROJECT_ROOT = Path(__file__).resolve().parents[2]
UPLOADS_DIR = PROJECT_ROOT / "uploads" / "gtfs"


def infer_source_kind(source_value: str) -> str:
    parsed = urlparse((source_value or "").strip())
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return "url"
    return "file"


def ensure_city_runtime_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS city_runtime_config (
            city_id                     BIGINT PRIMARY KEY REFERENCES cities(city_id) ON DELETE CASCADE,
            timezone_name               TEXT NOT NULL DEFAULT 'UTC',
            service_day_rollover_hour   INTEGER NOT NULL DEFAULT 4,
            is_enabled                  BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gtfs_rt_sources (
            rt_source_pk               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            city_id                    BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
            feed_type                  TEXT NOT NULL CHECK (feed_type IN ('vehicle_positions', 'trip_updates', 'alerts')),
            source_kind                TEXT NOT NULL CHECK (source_kind IN ('url', 'file')),
            source_value               TEXT NOT NULL,
            is_enabled                 BOOLEAN NOT NULL DEFAULT TRUE,
            refresh_interval_seconds   INTEGER,
            headers_json               JSONB,
            notes                      TEXT,

            CONSTRAINT ux_gtfs_rt_sources_city_feed_type UNIQUE (city_id, feed_type)
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gtfs_rt_sources_city_id
        ON gtfs_rt_sources(city_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gtfs_rt_sources_enabled
        ON gtfs_rt_sources(is_enabled)
        """
    )

    conn.execute(
        """
        ALTER TABLE city_runtime_config
        ADD COLUMN IF NOT EXISTS gtfs_import_in_progress BOOLEAN NOT NULL DEFAULT FALSE
        """
    )

    runtime_override_columns = {
        "sim_interval_seconds": "INTEGER",
        "sim_active_trip_lookbehind_seconds": "INTEGER",
        "sim_active_trip_lookahead_seconds": "INTEGER",
        "sim_apply_trip_updates": "BOOLEAN",
        "fusion_interval_seconds": "INTEGER",
        "fusion_rt_max_age_seconds": "INTEGER",
        "fusion_min_match_score": "DOUBLE PRECISION",
        "fusion_max_shape_distance_m": "DOUBLE PRECISION",
        "stop_arrivals_interval_seconds": "INTEGER",
        "stop_arrivals_horizon_seconds": "INTEGER",
        "stop_arrivals_limit_per_stop": "INTEGER",
        "stop_arrivals_shape_speed_mps": "DOUBLE PRECISION",
        "stop_arrivals_shape_max_distance_m": "DOUBLE PRECISION",
        "stop_arrivals_shape_passed_grace_m": "DOUBLE PRECISION",
    }
    for column_name, column_type in runtime_override_columns.items():
        conn.execute(
            f"ALTER TABLE city_runtime_config ADD COLUMN IF NOT EXISTS {column_name} {column_type}"
        )


def ensure_city(conn, city_code: str, city_name: str, timezone_name: str = "UTC") -> int:
    conn.execute(
        """
        INSERT INTO cities (city_code, city_name, timezone)
        VALUES (%s, %s, %s)
        ON CONFLICT (city_code) DO UPDATE
        SET city_name = EXCLUDED.city_name,
            timezone = EXCLUDED.timezone
        """,
        (city_code, city_name, timezone_name),
    )
    row = conn.execute(
        "SELECT city_id FROM cities WHERE city_code = %s",
        (city_code,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Failed to ensure city {city_code}")
    return int(row[0])


def upsert_city_runtime_config(conn, city_id: int, timezone_name: str, service_day_rollover_hour: int) -> None:
    conn.execute(
        """
        INSERT INTO city_runtime_config (
            city_id, timezone_name, service_day_rollover_hour, is_enabled
        )
        VALUES (%s, %s, %s, TRUE)
        ON CONFLICT (city_id) DO UPDATE
        SET timezone_name = EXCLUDED.timezone_name,
            service_day_rollover_hour = EXCLUDED.service_day_rollover_hour,
            is_enabled = TRUE
        """,
        (city_id, timezone_name, service_day_rollover_hour),
    )
    # Keep cities.timezone in sync
    conn.execute(
        "UPDATE cities SET timezone = %s WHERE city_id = %s",
        (timezone_name, city_id),
    )


from app.credentials import encrypt_json

def upsert_rt_sources(conn, city_id: int, rt_sources: dict) -> None:
    provided_feed_types = set()

    for feed_type, payload in rt_sources.items():
        source_value = (payload.get("source_value") or "").strip()
        refresh_interval_seconds = payload.get("refresh_interval_seconds")

        if not source_value:
            continue

        provided_feed_types.add(feed_type)
        source_kind = infer_source_kind(source_value)

        headers = payload.get("headers") or {}
        query_params = payload.get("query_params") or {}
        encrypt_url = bool(payload.get("encrypt_url"))

        # Encrypt the source URL itself if it contains credentials
        stored_source_value = source_value
        if encrypt_url and source_value:
            from app.credentials import encrypt_string
            stored_source_value = encrypt_string(source_value)

        conn.execute(
            """
            INSERT INTO gtfs_rt_sources (
                city_id, feed_type, source_kind, source_value,
                is_enabled, refresh_interval_seconds, headers_json, query_params_json,
                source_encrypted, notes
            )
            VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s, %s, NULL)
            ON CONFLICT (city_id, feed_type) DO UPDATE
            SET source_kind = EXCLUDED.source_kind,
                source_value = EXCLUDED.source_value,
                is_enabled = TRUE,
                refresh_interval_seconds = EXCLUDED.refresh_interval_seconds,
                headers_json = EXCLUDED.headers_json,
                query_params_json = EXCLUDED.query_params_json,
                source_encrypted = EXCLUDED.source_encrypted
            """,
            (
                city_id,
                feed_type,
                source_kind,
                stored_source_value,
                refresh_interval_seconds,
                encrypt_json(headers),
                encrypt_json(query_params),
                encrypt_url,
            ),
        )

    # Disable configured feeds not provided in the latest import form.
    # Only do this when rt_sources was explicitly provided (initial city setup).
    # On a GTFS static update, rt_sources is empty and we preserve existing config.
    if provided_feed_types:
        for feed_type in ("vehicle_positions", "trip_updates", "alerts"):
            if feed_type in provided_feed_types:
                continue
            conn.execute(
                """
                UPDATE gtfs_rt_sources
                SET is_enabled = FALSE
                WHERE city_id = %s AND feed_type = %s
                """,
                (city_id, feed_type),
            )


def save_uploaded_zip(city_code: str, zip_filename: str, zip_bytes: bytes) -> Path:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    safe_name = f"{city_code}_gtfs.zip"
    path = UPLOADS_DIR / safe_name
    path.write_bytes(zip_bytes)

    return path


def run_gtfs_loader(saved_zip_path: Path, city_code: str, city_name: str, log_callback=None) -> subprocess.CompletedProcess:
    candidate_loader_paths = [
        PROJECT_ROOT / "gtfs_loader.py",
        PROJECT_ROOT / "app" / "ingest" / "gtfs_loader.py",
        PROJECT_ROOT / "transit" / "gtfs_loader.py",
    ]

    script_path = next((path for path in candidate_loader_paths if path.exists()), None)

    if script_path is not None:
        cmd = [
            sys.executable,
            str(script_path),
            "--zip",
            str(saved_zip_path),
            "--city-code",
            city_code,
            "--city-name",
            city_name,
            "--replace",
        ]
    else:
        cmd = [
            sys.executable,
            "-m",
            "app.ingest.gtfs_loader",
            "--zip",
            str(saved_zip_path),
            "--city-code",
            city_code,
            "--city-name",
            city_name,
            "--replace",
        ]

    process = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    stdout_lines = []
    for line in process.stdout:
        line = line.rstrip()
        stdout_lines.append(line)
        print(line, flush=True)
        if log_callback:
            log_callback(line)

    process.wait()
    stderr = process.stderr.read()

    return subprocess.CompletedProcess(
        cmd,
        process.returncode,
        stdout="\n".join(stdout_lines),
        stderr=stderr,
    )



def set_gtfs_import_in_progress(conn, city_id: int, value: bool) -> None:
    conn.execute(
        """
        UPDATE city_runtime_config
        SET gtfs_import_in_progress = %s
        WHERE city_id = %s
        """,
        (value, city_id),
    )


def clear_city_runtime_state(conn, city_id: int) -> None:
    """Best-effort cleanup of live/runtime caches after a GTFS replacement.

    The static GTFS loader owns the static tables. This cleanup only removes
    runtime/realtime state that may point at old trip_pk/route_pk/shape_pk rows.
    """
    tables = [
        "vehicle_states_current",
        "vehicle_states_simulated_current",
        "gtfs_rt_trip_updates_current",
        "gtfs_rt_stop_time_updates_current",
        "gtfs_rt_alerts_current",
    ]
    for table in tables:
        try:
            conn.execute(f"DELETE FROM {table} WHERE city_id = %s", (city_id,))
        except Exception:
            # Some dev databases may not have every current/cache table yet.
            conn.rollback()


def wait_for_runtime_manager_maintenance(city_code: str) -> None:
    """Give the runtime manager time to stop city-scoped workers.

    The runtime manager reconciles every RUNTIME_MANAGER_SYNC_INTERVAL_SECONDS
    seconds, so this defaults to that interval plus a small buffer. Override with
    GTFS_IMPORT_MAINTENANCE_WAIT_SECONDS if needed.
    """
    explicit = os.getenv("GTFS_IMPORT_MAINTENANCE_WAIT_SECONDS")
    if explicit:
        wait_seconds = max(0.0, float(explicit))
    else:
        reconcile = float(os.getenv("RUNTIME_MANAGER_SYNC_INTERVAL_SECONDS", os.getenv("RUNTIME_RECONCILE_INTERVAL_SECONDS", "15")))
        wait_seconds = reconcile + 5.0

    if wait_seconds > 0:
        print(f"GTFS maintenance mode enabled for city={city_code}; waiting {wait_seconds:.1f}s for runtime workers to stop...", flush=True)
        time.sleep(wait_seconds)


def import_city_bundle(
    db_config: dict,
    city_code: str,
    city_name: str,
    timezone_name: str,
    service_day_rollover_hour: int,
    zip_filename: str,
    zip_bytes: bytes,
    rt_sources: dict,
    log_callback=None,
) -> dict:
    if not city_code:
        raise RuntimeError("city_code is required")
    if not city_name:
        raise RuntimeError("city_name is required")
    if not zip_bytes:
        raise RuntimeError("GTFS zip upload is empty")

    saved_zip_path = save_uploaded_zip(city_code, zip_filename, zip_bytes)
    city_id: int | None = None

    try:
        with psycopg.connect(**db_config) as conn:
            ensure_city_runtime_tables(conn)
            city_id = ensure_city(conn, city_code, city_name, timezone_name)
            upsert_city_runtime_config(conn, city_id, timezone_name, service_day_rollover_hour)
            upsert_rt_sources(conn, city_id, rt_sources)
            set_gtfs_import_in_progress(conn, city_id, True)
            conn.commit()

        wait_for_runtime_manager_maintenance(city_code)

        result = run_gtfs_loader(saved_zip_path, city_code, city_name, log_callback=log_callback)

        if result.returncode != 0:
            raise RuntimeError(
                "GTFS import failed.\n"
                f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
            )

        if city_id is not None:
            with psycopg.connect(**db_config) as conn:
                clear_city_runtime_state(conn, city_id)
                conn.commit()

        return {
            "city_code": city_code,
            "city_name": city_name,
            "city_id": city_id,
            "saved_zip_path": str(saved_zip_path),
            "loader_exit_code": result.returncode,
            "loader_stdout": result.stdout,
            "loader_stderr": result.stderr,
        }
    finally:
        if city_id is not None:
            try:
                with psycopg.connect(**db_config) as conn:
                    set_gtfs_import_in_progress(conn, city_id, False)
                    conn.commit()
            except Exception:
                # Do not mask the real import error, but leave a clear signal in logs.
                print(f"WARNING: failed to clear GTFS maintenance mode for city={city_code}", flush=True)
