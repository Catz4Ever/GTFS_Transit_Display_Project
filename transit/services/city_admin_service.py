import psycopg


RUNTIME_OVERRIDE_COLUMNS = {
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


def ensure_runtime_override_columns(conn):
    for column_name, column_type in RUNTIME_OVERRIDE_COLUMNS.items():
        conn.execute(
            f"ALTER TABLE city_runtime_config ADD COLUMN IF NOT EXISTS {column_name} {column_type}"
        )


def _optional_int(value, default=None):
    if value is None or value == "":
        return default
    return int(value)


def _optional_float(value, default=None):
    if value is None or value == "":
        return default
    return float(value)


def _optional_bool(value):
    if value is None or value == "":
        return None
    return str(value).lower() in {"1", "true", "yes", "on"}


def _looks_like_fernet_token(value: str) -> bool:
    return bool(value) and value.startswith("gAAAA")


def get_city_admin_overview(conn):
    rows = conn.execute(
        '''
        SELECT
            c.city_code,
            c.city_name,
            COALESCE(crc.timezone_name, 'UTC') AS timezone_name,
            COALESCE(crc.service_day_rollover_hour, 4) AS service_day_rollover_hour,
            COALESCE(crc.is_enabled, TRUE) AS is_enabled,
            COALESCE(crc.sim_enabled, TRUE) AS sim_enabled,
            crc.sim_force_date,
            crc.sim_force_time,
            s.feed_type,
            s.source_kind,
            s.source_value,
            s.is_enabled AS source_enabled,
            s.refresh_interval_seconds,
            COALESCE(s.source_encrypted, FALSE) AS source_encrypted
        FROM cities c
        LEFT JOIN city_runtime_config crc ON crc.city_id = c.city_id
        LEFT JOIN gtfs_rt_sources s ON s.city_id = c.city_id
        ORDER BY c.city_code, s.feed_type
        '''
    ).fetchall()
    return rows


def get_city_detail(conn, city_code: str):
    ensure_runtime_override_columns(conn)

    city_row = conn.execute(
        '''
        SELECT
            c.city_id,
            c.city_code,
            c.city_name,
            COALESCE(crc.timezone_name, 'UTC') AS timezone_name,
            COALESCE(crc.service_day_rollover_hour, 4) AS service_day_rollover_hour,
            COALESCE(crc.is_enabled, TRUE) AS is_enabled,
            COALESCE(crc.sim_enabled, TRUE) AS sim_enabled,
            crc.sim_force_date,
            crc.sim_force_time,
            crc.gtfs_url,
            crc.gtfs_refresh_interval_hours,
            crc.gtfs_last_refreshed_at,
            crc.gtfs_last_refresh_status,
            COALESCE(crc.gtfs_url_encrypted, FALSE) AS gtfs_url_encrypted,
            crc.sim_interval_seconds,
            crc.sim_active_trip_lookbehind_seconds,
            crc.sim_active_trip_lookahead_seconds,
            crc.sim_apply_trip_updates,
            crc.fusion_interval_seconds,
            crc.fusion_rt_max_age_seconds,
            crc.fusion_min_match_score,
            crc.fusion_max_shape_distance_m,
            crc.stop_arrivals_interval_seconds,
            crc.stop_arrivals_horizon_seconds,
            crc.stop_arrivals_limit_per_stop,
            crc.stop_arrivals_shape_speed_mps,
            crc.stop_arrivals_shape_max_distance_m,
            crc.stop_arrivals_shape_passed_grace_m
        FROM cities c
        LEFT JOIN city_runtime_config crc ON crc.city_id = c.city_id
        WHERE c.city_code = %s
        ''',
        (city_code,),
    ).fetchone()

    if city_row is None:
        return None

    source_rows = conn.execute(
        '''
        SELECT feed_type, source_kind, source_value, is_enabled, refresh_interval_seconds, COALESCE(source_encrypted, FALSE)
        FROM gtfs_rt_sources
        WHERE city_id = %s
        ORDER BY feed_type
        ''',
        (int(city_row[0]),),
    ).fetchall()

    sources = {
        str(r[0]): {
            "source_kind": str(r[1]),
            # Do not send encrypted ciphertext back to the edit form.
            # Leaving the field blank preserves the existing encrypted source on save.
            "source_value": "" if bool(r[5]) else str(r[2]),
            "stored_source_value": str(r[2]),
            "is_enabled": bool(r[3]),
            "refresh_interval_seconds": int(r[4]) if r[4] is not None else None,
            "source_encrypted": bool(r[5]),
        }
        for r in source_rows
    }

    return {
        "city_id": int(city_row[0]),
        "city_code": str(city_row[1]),
        "city_name": str(city_row[2]),
        "timezone_name": str(city_row[3]),
        "service_day_rollover_hour": int(city_row[4]),
        "is_enabled": bool(city_row[5]),
        "sim_enabled": bool(city_row[6]),
        "sim_force_date": str(city_row[7]) if city_row[7] is not None else "",
        "sim_force_time": str(city_row[8]) if city_row[8] is not None else "",
        # Do not expose encrypted GTFS static URL ciphertext in the edit form.
        # Leaving the field blank preserves it on save.
        "gtfs_url": "" if bool(city_row[13]) else (str(city_row[9]) if city_row[9] is not None else ""),
        "stored_gtfs_url": str(city_row[9]) if city_row[9] is not None else "",
        "gtfs_refresh_interval_hours": int(city_row[10]) if city_row[10] is not None else 24,
        "gtfs_last_refreshed_at": str(city_row[11]) if city_row[11] is not None else None,
        "gtfs_last_refresh_status": str(city_row[12]) if city_row[12] is not None else None,
        "gtfs_url_encrypted": bool(city_row[13]),
        "sim_interval_seconds": city_row[14],
        "sim_active_trip_lookbehind_seconds": city_row[15],
        "sim_active_trip_lookahead_seconds": city_row[16],
        "sim_apply_trip_updates": city_row[17],
        "fusion_interval_seconds": city_row[18],
        "fusion_rt_max_age_seconds": city_row[19],
        "fusion_min_match_score": city_row[20],
        "fusion_max_shape_distance_m": city_row[21],
        "stop_arrivals_interval_seconds": city_row[22],
        "stop_arrivals_horizon_seconds": city_row[23],
        "stop_arrivals_limit_per_stop": city_row[24],
        "stop_arrivals_shape_speed_mps": city_row[25],
        "stop_arrivals_shape_max_distance_m": city_row[26],
        "stop_arrivals_shape_passed_grace_m": city_row[27],
        "sources": sources,
    }


def ensure_city_runtime_row(conn, city_id: int):
    conn.execute(
        '''
        INSERT INTO city_runtime_config (
            city_id, timezone_name, service_day_rollover_hour, is_enabled, sim_enabled
        )
        VALUES (%s, 'UTC', 4, TRUE, TRUE)
        ON CONFLICT (city_id) DO NOTHING
        ''',
        (city_id,),
    )


def update_city_runtime_settings(
    conn,
    city_code: str,
    city_name: str,
    timezone_name: str,
    service_day_rollover_hour: int,
    is_enabled: bool,
    sim_enabled: bool,
    sim_force_date: str,
    sim_force_time: str,
    source_updates: dict,
    gtfs_url: str = "",
    gtfs_refresh_interval_hours: int = 24,
    gtfs_url_encrypted: bool = False,
    runtime_overrides: dict | None = None,
):
    ensure_runtime_override_columns(conn)

    city_row = conn.execute(
        "SELECT city_id FROM cities WHERE city_code = %s",
        (city_code,),
    ).fetchone()

    if city_row is None:
        raise RuntimeError(f"City not found: {city_code}")

    city_id = int(city_row[0])

    conn.execute(
        '''
        UPDATE cities
        SET city_name = %s
        WHERE city_id = %s
        ''',
        (city_name, city_id),
    )

    ensure_city_runtime_row(conn, city_id)

    existing_runtime = conn.execute(
        """
        SELECT gtfs_url, COALESCE(gtfs_url_encrypted, FALSE)
        FROM city_runtime_config
        WHERE city_id = %s
        """,
        (city_id,),
    ).fetchone()
    existing_gtfs_url = str(existing_runtime[0]) if existing_runtime and existing_runtime[0] is not None else ""
    existing_gtfs_url_encrypted = bool(existing_runtime[1]) if existing_runtime else False

    from app.credentials import encrypt_string
    clean_gtfs_url = gtfs_url.strip() if gtfs_url else ""
    if not clean_gtfs_url and existing_gtfs_url_encrypted:
        stored_gtfs_url = existing_gtfs_url
        gtfs_url_encrypted = True
    elif gtfs_url_encrypted and clean_gtfs_url:
        stored_gtfs_url = encrypt_string(clean_gtfs_url)
    else:
        stored_gtfs_url = clean_gtfs_url

    conn.execute(
        '''
        UPDATE city_runtime_config
        SET timezone_name = %s,
            service_day_rollover_hour = %s,
            is_enabled = %s,
            sim_enabled = %s,
            sim_force_date = %s,
            sim_force_time = %s,
            gtfs_url = NULLIF(%s, ''),
            gtfs_url_encrypted = %s,
            gtfs_refresh_interval_hours = %s,
            sim_interval_seconds = %s,
            sim_active_trip_lookbehind_seconds = %s,
            sim_active_trip_lookahead_seconds = %s,
            sim_apply_trip_updates = %s,
            fusion_interval_seconds = %s,
            fusion_rt_max_age_seconds = %s,
            fusion_min_match_score = %s,
            fusion_max_shape_distance_m = %s,
            stop_arrivals_interval_seconds = %s,
            stop_arrivals_horizon_seconds = %s,
            stop_arrivals_limit_per_stop = %s,
            stop_arrivals_shape_speed_mps = %s,
            stop_arrivals_shape_max_distance_m = %s,
            stop_arrivals_shape_passed_grace_m = %s
        WHERE city_id = %s
        ''',
        (
            timezone_name,
            service_day_rollover_hour,
            is_enabled,
            sim_enabled,
            sim_force_date.strip() or None,
            sim_force_time.strip() or None,
            stored_gtfs_url,
            gtfs_url_encrypted,
            gtfs_refresh_interval_hours or 24,
            _optional_int((runtime_overrides or {}).get("sim_interval_seconds")),
            _optional_int((runtime_overrides or {}).get("sim_active_trip_lookbehind_seconds")),
            _optional_int((runtime_overrides or {}).get("sim_active_trip_lookahead_seconds")),
            _optional_bool((runtime_overrides or {}).get("sim_apply_trip_updates")),
            _optional_int((runtime_overrides or {}).get("fusion_interval_seconds")),
            _optional_int((runtime_overrides or {}).get("fusion_rt_max_age_seconds")),
            _optional_float((runtime_overrides or {}).get("fusion_min_match_score")),
            _optional_float((runtime_overrides or {}).get("fusion_max_shape_distance_m")),
            _optional_int((runtime_overrides or {}).get("stop_arrivals_interval_seconds")),
            _optional_int((runtime_overrides or {}).get("stop_arrivals_horizon_seconds")),
            _optional_int((runtime_overrides or {}).get("stop_arrivals_limit_per_stop")),
            _optional_float((runtime_overrides or {}).get("stop_arrivals_shape_speed_mps")),
            _optional_float((runtime_overrides or {}).get("stop_arrivals_shape_max_distance_m")),
            _optional_float((runtime_overrides or {}).get("stop_arrivals_shape_passed_grace_m")),
            city_id,
        ),
    )

    from app.credentials import encrypt_string
    from transit.services.city_import_service import infer_source_kind

    existing_source_rows = conn.execute(
        """
        SELECT feed_type, source_kind, source_value, COALESCE(source_encrypted, FALSE)
        FROM gtfs_rt_sources
        WHERE city_id = %s
        """,
        (city_id,),
    ).fetchall()
    existing_sources = {
        str(r[0]): {
            "source_kind": str(r[1]),
            "source_value": str(r[2]),
            "source_encrypted": bool(r[3]),
        }
        for r in existing_source_rows
    }

    for feed_type, payload in source_updates.items():
        raw_source_value = (payload.get("source_value") or "").strip()
        encrypt_url = bool(payload.get("encrypt_url"))
        existing = existing_sources.get(feed_type)

        # If the existing value is encrypted and the edit form field is left blank,
        # preserve source_value/source_kind/source_encrypted. Otherwise, saving the
        # page would inspect Fernet ciphertext like "gAAAA..." and incorrectly
        # classify the source as a file.
        if existing and existing.get("source_encrypted") and not raw_source_value:
            stored_source_value = existing["source_value"]
            source_kind = existing["source_kind"]
            source_encrypted = True
        elif (
            existing
            and existing.get("source_encrypted")
            and _looks_like_fernet_token(raw_source_value)
            and raw_source_value == existing.get("source_value")
        ):
            stored_source_value = existing["source_value"]
            source_kind = existing["source_kind"]
            source_encrypted = True
        else:
            source_kind = infer_source_kind(raw_source_value) if raw_source_value else (existing["source_kind"] if existing else "url")
            source_encrypted = bool(encrypt_url and raw_source_value)
            stored_source_value = encrypt_string(raw_source_value) if source_encrypted else raw_source_value

        conn.execute(
            '''
            UPDATE gtfs_rt_sources
            SET source_value = %s,
                source_kind = %s,
                is_enabled = %s,
                refresh_interval_seconds = %s,
                source_encrypted = %s
            WHERE city_id = %s
              AND feed_type = %s
            ''',
            (
                stored_source_value,
                source_kind,
                payload.get("is_enabled", False),
                payload.get("refresh_interval_seconds"),
                source_encrypted,
                city_id,
                feed_type,
            ),
        )
