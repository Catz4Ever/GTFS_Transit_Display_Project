#!/usr/bin/env python3
"""
GTFS Auto-Refresh Service

Runs as a managed process. Periodically checks if any city has a gtfs_url
configured, downloads to a temp file, validates, and imports only if valid.
Existing data is never touched if the download or validation fails.
"""

import io
import logging
import os
import re
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlparse, urlunparse
import zipfile
from pathlib import Path

import psycopg
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Required GTFS files — import is rejected if any are missing or empty
REQUIRED_GTFS_FILES = {
    "agency.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
    "stops.txt",
}

# Minimum row counts to consider a feed valid (header + at least this many rows)
MIN_ROW_COUNTS = {
    "routes.txt": 1,
    "trips.txt": 10,
    "stop_times.txt": 100,
    "stops.txt": 10,
}

# Default check interval if not set per-city
DEFAULT_REFRESH_INTERVAL_HOURS = int(os.getenv("GTFS_REFRESH_INTERVAL_HOURS", "24"))
# How often this service wakes up to check if any city is due
CHECK_INTERVAL_SECONDS = int(os.getenv("GTFS_REFRESH_CHECK_SECONDS", "300"))


def redact_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(str(value))
    if parsed.scheme in ("http", "https"):
        return urlunparse((parsed.scheme, parsed.netloc or "<missing-host>", parsed.path, "", "***" if parsed.query else "", ""))
    text = re.sub(
        r"(?i)(api[_-]?key|apikey|access[_-]?token|token|secret|password|key)=([^&\s]+)",
        r"\1=***",
        str(value),
    )
    return text[:157] + "..." if len(text) > 160 else text


def sanitize_error_message(message: str) -> str:
    text = str(message)
    text = re.sub(
        r"(?i)(api[_-]?key|apikey|access[_-]?token|token|secret|password|key)=([^&\s]+)",
        r"\1=***",
        text,
    )
    text = re.sub(r"https?://[^\s]+", lambda m: redact_url(m.group(0)), text)
    return text


def get_db_config() -> dict:
    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": os.getenv("DB_NAME", "transit_db"),
        "user": os.getenv("DB_USER", "transit_user"),
        "password": os.getenv("DB_PASSWORD"),
    }


def get_cities_due_for_refresh(conn) -> list[dict]:
    """Return cities that have a gtfs_url and are due for a refresh."""
    rows = conn.execute("""
        SELECT
            c.city_code,
            c.city_name,
            crc.gtfs_url,
            COALESCE(crc.gtfs_url_encrypted, FALSE) AS gtfs_url_encrypted,
            crc.gtfs_refresh_interval_hours,
            crc.gtfs_last_refreshed_at,
            crc.gtfs_last_refresh_status
        FROM cities c
        JOIN city_runtime_config crc ON crc.city_id = c.city_id
        WHERE crc.gtfs_url IS NOT NULL
          AND crc.gtfs_url != ''
          AND COALESCE(crc.is_enabled, TRUE) = TRUE
    """).fetchall()

    due = []
    for row in rows:
        city_code, city_name, gtfs_url, gtfs_url_encrypted, interval_hours, last_refreshed, last_status = row
        if gtfs_url_encrypted:
            from app.credentials import decrypt_string
            gtfs_url = decrypt_string(gtfs_url)
        interval_hours = interval_hours or DEFAULT_REFRESH_INTERVAL_HOURS

        # Check if due: never refreshed, or interval has elapsed
        is_due = conn.execute("""
            SELECT (
                %s::timestamptz IS NULL
                OR NOW() - %s::timestamptz > make_interval(hours => %s)
            )
        """, (last_refreshed, last_refreshed, interval_hours)).fetchone()[0]

        if is_due:
            due.append({
                "city_code": city_code,
                "city_name": city_name,
                "gtfs_url": gtfs_url,
                "interval_hours": interval_hours,
                "last_refreshed": last_refreshed,
                "last_status": last_status,
            })

    return due


def download_gtfs(url: str, city_code: str) -> tuple[bytes, str | None]:
    """Download GTFS zip from URL."""
    logging.info("GTFS refresh: downloading city=%s source=%s", city_code, redact_url(url))

    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return b"", f"Invalid GTFS URL: {redact_url(url)}"
        req = urllib.request.Request(url, headers={"User-Agent": "transit-gtfs-refresh/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        logging.info("GTFS refresh: downloaded city=%s bytes=%s", city_code, len(data))
        return data, None
    except Exception as e:
        return b"", f"Download failed: {sanitize_error_message(str(e))}"


def validate_gtfs(data: bytes, city_code: str) -> str | None:
    """Validate GTFS zip contents. Returns error message or None if valid."""
    if len(data) < 100:
        return f"Downloaded file too small ({len(data)} bytes)"

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())

            # Some feeds wrap files in a subdirectory
            prefix = ""
            if not any(n in names for n in REQUIRED_GTFS_FILES):
                # Try stripping one directory level
                dirs = {n.split("/")[0] for n in names if "/" in n}
                if len(dirs) == 1:
                    prefix = list(dirs)[0] + "/"

            for required in REQUIRED_GTFS_FILES:
                full = prefix + required
                if full not in names:
                    return f"Missing required file: {required}"

            for filename, min_rows in MIN_ROW_COUNTS.items():
                full = prefix + filename
                try:
                    content = zf.read(full).decode("utf-8-sig", errors="replace")
                    row_count = content.count("\n") - 1  # subtract header
                    if row_count < min_rows:
                        return f"{filename} has only {row_count} rows (minimum {min_rows})"
                except Exception as e:
                    return f"Could not read {filename}: {e}"

    except zipfile.BadZipFile:
        return "Downloaded file is not a valid ZIP"
    except Exception as e:
        return f"Validation error: {e}"

    return None


def record_refresh_attempt(conn, city_code: str, status: str) -> None:
    conn.execute("""
        UPDATE city_runtime_config
        SET gtfs_last_refreshed_at = NOW(),
            gtfs_last_refresh_status = %s
        WHERE city_id = (SELECT city_id FROM cities WHERE city_code = %s)
    """, (status, city_code))
    conn.commit()


def run_import(city: dict, db_config: dict) -> tuple[bool, str]:
    """Run import via import_city_bundle with proper maintenance mode handling."""
    from transit.services.city_import_service import (
        import_city_bundle,
        set_gtfs_import_in_progress,
        wait_for_runtime_manager_maintenance,
    )
    import psycopg as _psycopg

    city_code = city["city_code"]
    city_name = city["city_name"]
    data = city["_gtfs_bytes"]

    # Look up city_id and runtime config for the bundle call
    with _psycopg.connect(**db_config) as conn:
        row = conn.execute("""
            SELECT c.city_id, crc.timezone_name, crc.service_day_rollover_hour
            FROM cities c
            LEFT JOIN city_runtime_config crc ON crc.city_id = c.city_id
            WHERE c.city_code = %s
        """, (city_code,)).fetchone()

    if not row:
        return False, f"city not found: {city_code}"

    city_id, timezone_name, rollover_hour = int(row[0]), row[1] or "UTC", int(row[2] or 4)

    # Set maintenance mode and wait for workers to stop
    with _psycopg.connect(**db_config) as conn:
        set_gtfs_import_in_progress(conn, city_id, True)
        conn.commit()

    logging.info("GTFS refresh: maintenance mode enabled city=%s", city_code)
    wait_for_runtime_manager_maintenance(city_code)

    try:
        import_city_bundle(
            db_config=db_config,
            city_code=city_code,
            city_name=city_name,
            timezone_name=timezone_name,
            service_day_rollover_hour=rollover_hour,
            zip_filename=f"{city_code}_gtfs_refresh.zip",
            zip_bytes=data,
            rt_sources={},  # don't overwrite existing RT sources on auto-refresh
        )
        return True, "success"
    except Exception as e:
        return False, f"import_error: {str(e)[:500]}"
    finally:
        with _psycopg.connect(**db_config) as conn:
            set_gtfs_import_in_progress(conn, city_id, False)
            conn.commit()
        logging.info("GTFS refresh: maintenance mode disabled city=%s", city_code)


def refresh_city(city: dict, db_config: dict) -> None:
    city_code = city["city_code"]
    url = city["gtfs_url"]

    # Download
    data, err = download_gtfs(url, city_code)
    if err:
        logging.error("GTFS refresh: download failed city=%s error=%s", city_code, sanitize_error_message(err))
        with psycopg.connect(**db_config) as conn:
            record_refresh_attempt(conn, city_code, f"download_failed: {sanitize_error_message(err)[:200]}")
        return

    # Validate — never import bad data
    err = validate_gtfs(data, city_code)
    if err:
        logging.error("GTFS refresh: validation failed city=%s error=%s", city_code, err)
        with psycopg.connect(**db_config) as conn:
            record_refresh_attempt(conn, city_code, f"validation_failed: {err[:200]}")
        return

    logging.info("GTFS refresh: validation passed city=%s, running import", city_code)

    city["_gtfs_bytes"] = data
    success, msg = run_import(city, db_config)
    if success:
        logging.info("GTFS refresh: import complete city=%s", city_code)
    else:
        logging.error("GTFS refresh: import failed city=%s msg=%s", city_code, msg)

    with psycopg.connect(**db_config) as conn:
        record_refresh_attempt(conn, city_code, msg)


def run() -> None:
    load_dotenv()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    db_config = get_db_config()
    if not db_config["password"]:
        logging.error("DB_PASSWORD not set, exiting")
        sys.exit(1)

    logging.info("GTFS refresh service started check_interval=%ss", CHECK_INTERVAL_SECONDS)

    while True:
        try:
            with psycopg.connect(**db_config) as conn:
                due = get_cities_due_for_refresh(conn)

            if due:
                logging.info("GTFS refresh: %s city/cities due for refresh", len(due))
                for city in due:
                    refresh_city(city, db_config)
            else:
                logging.debug("GTFS refresh: no cities due")

        except Exception as e:
            logging.exception("GTFS refresh: unexpected error: %s", e)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
