#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

import psycopg
from urllib.parse import urlencode, urlparse, urlunparse, parse_qs
from app.credentials import decrypt_json
from dotenv import load_dotenv
from google.protobuf.json_format import MessageToDict
from google.transit import gtfs_realtime_pb2


VALID_FEED_TYPES = {"vehicle_positions", "trip_updates", "alerts"}

SENSITIVE_QUERY_KEYS = {
    "api_key", "apikey", "key", "token", "access_token",
    "auth", "authorization", "secret", "password",
}


def redact_url(value: str) -> str:
    """Return a URL/path label safe for logs; never include API keys or query values."""
    if not value:
        return ""
    text = str(value)
    parsed = urlparse(text)
    if parsed.scheme in ("http", "https"):
        host = parsed.netloc or "<missing-host>"
        query = "***" if parsed.query else ""
        return urlunparse((parsed.scheme, host, parsed.path, "", query, ""))

    text = re.sub(
        r"(?i)(api[_-]?key|apikey|access[_-]?token|token|secret|password|key)=([^&\s]+)",
        r"\1=***",
        text,
    )
    return text[:157] + "..." if len(text) > 160 else text


def sanitize_error_message(message: str) -> str:
    """Redact secrets from exception text before it is printed to container logs."""
    text = str(message)
    text = re.sub(
        r"(?i)(api[_-]?key|apikey|access[_-]?token|token|secret|password|key)=([^&\s]+)",
        r"\1=***",
        text,
    )
    text = re.sub(r"(?i)(Authorization:\s*)(Bearer\s+)?[^\s,;]+", r"\1***", text)
    text = re.sub(r"https?://[^\s]+", lambda m: redact_url(m.group(0)), text)
    return text


def normalize_source_kind(source_kind: str, source_value: str) -> str:
    """
    Prefer the decrypted/plain source value over the stored source_kind.
    This keeps encrypted URL sources working even if older admin saves accidentally
    stored source_kind='file' after inspecting Fernet ciphertext instead of the URL.
    """
    parsed = urlparse(source_value or "")
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return "url"
    return source_kind


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


def get_city_row(conn, city_code: str):
    row = conn.execute(
        "SELECT city_id, city_name FROM cities WHERE city_code = %s",
        (city_code,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"City not found: {city_code}")
    return int(row[0]), str(row[1])


def get_source_config(conn, city_id: int, feed_type: str):
    row = conn.execute(
        """
        SELECT rt_source_pk, source_kind, source_value, is_enabled, headers_json, query_params_json, refresh_interval_seconds, COALESCE(source_encrypted, FALSE)
        FROM gtfs_rt_sources
        WHERE city_id = %s AND feed_type = %s
        """,
        (city_id, feed_type),
    ).fetchone()

    if row is None:
        raise RuntimeError(f"No GTFS-RT source configured for city_id={city_id}, feed_type={feed_type}")

    if not bool(row[3]):
        raise RuntimeError(f"GTFS-RT source is disabled for city_id={city_id}, feed_type={feed_type}")

    return {
        "rt_source_pk": int(row[0]),
        "source_kind": str(row[1]),
        "source_value": str(row[2]),
        "is_enabled": bool(row[3]),
        "headers_json": row[4],
        "query_params_json": row[5],
        "refresh_interval_seconds": int(row[6]) if row[6] is not None else None,
        "source_encrypted": bool(row[7]),
    }


def load_feed_bytes(source_kind: str, source_value: str, headers_json=None, query_params_json=None) -> bytes:
    source_kind = normalize_source_kind(source_kind, source_value)

    if source_kind == "file":
        path = Path(source_value)
        if not path.exists():
            raise RuntimeError(f"GTFS-RT file not found: {redact_url(str(path))}")
        return path.read_bytes()

    if source_kind == "url":
        parsed = urlparse(source_value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise RuntimeError(f"Invalid GTFS-RT URL: {redact_url(source_value)}")

        query_params = decrypt_json(query_params_json)
        if query_params:
            existing = parse_qs(parsed.query, keep_blank_values=True)
            existing.update({k: [v] for k, v in query_params.items()})
            new_query = urlencode({k: v[0] for k, v in existing.items()})
            source_value = urlunparse(parsed._replace(query=new_query))

        headers = decrypt_json(headers_json)
        req = urllib.request.Request(source_value, headers={"User-Agent": "transit-gtfs-rt-ingestor/1.0"})
        for k, v in headers.items():
            req.add_header(str(k), str(v))

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception as e:
            raise RuntimeError(f"GTFS-RT URL fetch failed source={redact_url(source_value)} error={sanitize_error_message(str(e))}") from e

    raise RuntimeError(f"Unsupported GTFS-RT source_kind: {source_kind}")


def parse_feed(feed_bytes: bytes) -> gtfs_realtime_pb2.FeedMessage:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(feed_bytes)
    return feed


def create_snapshot(conn, city_id: int, feed_type: str, feed: gtfs_realtime_pb2.FeedMessage) -> int:
    header = feed.header
    feed_timestamp = int(header.timestamp) if header.HasField("timestamp") else None
    version = header.gtfs_realtime_version if header.HasField("gtfs_realtime_version") else None
    entity_count = len(feed.entity)

    row = conn.execute(
        """
        INSERT INTO gtfs_rt_feed_snapshots (
            city_id, feed_type, feed_timestamp, gtfs_realtime_version, entity_count
        )
        VALUES (%s, %s, %s, %s, %s)
        RETURNING snapshot_pk
        """,
        (city_id, feed_type, feed_timestamp, version, entity_count),
    ).fetchone()
    return int(row[0])


def resolve_trip_pk(conn, city_id: int, trip_id: str | None):
    if not trip_id:
        return None
    row = conn.execute(
        "SELECT trip_pk FROM trips WHERE city_id = %s AND trip_id = %s",
        (city_id, trip_id),
    ).fetchone()
    return int(row[0]) if row else None


def resolve_route_pk(conn, city_id: int, route_id: str | None):
    if not route_id:
        return None
    row = conn.execute(
        "SELECT route_pk FROM routes WHERE city_id = %s AND route_id = %s",
        (city_id, route_id),
    ).fetchone()
    return int(row[0]) if row else None


def entity_json(entity) -> str:
    return json.dumps(MessageToDict(entity, preserving_proto_field_name=True), ensure_ascii=False)


def ingest_vehicle_positions(conn, city_id: int, snapshot_pk: int, feed: gtfs_realtime_pb2.FeedMessage) -> int:
    inserted = 0

    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue

        vehicle = entity.vehicle
        if not vehicle.HasField("position"):
            continue

        trip = vehicle.trip
        pos = vehicle.position

        trip_id = trip.trip_id if trip.HasField("trip_id") else None
        route_id = trip.route_id if trip.HasField("route_id") else None

        trip_pk = resolve_trip_pk(conn, city_id, trip_id)
        route_pk = resolve_route_pk(conn, city_id, route_id)

        vehicle_id = vehicle.vehicle.id if vehicle.HasField("vehicle") and vehicle.vehicle.HasField("id") else None
        vehicle_label = vehicle.vehicle.label if vehicle.HasField("vehicle") and vehicle.vehicle.HasField("label") else None

        stop_id = vehicle.stop_id if vehicle.HasField("stop_id") else None
        start_date = trip.start_date if trip.HasField("start_date") else None
        start_time = trip.start_time if trip.HasField("start_time") else None
        schedule_relationship = int(trip.schedule_relationship) if trip.HasField("schedule_relationship") else None

        bearing = float(pos.bearing) if pos.HasField("bearing") else None
        speed = float(pos.speed) if pos.HasField("speed") else None
        latitude = float(pos.latitude)
        longitude = float(pos.longitude)

        current_stop_sequence = int(vehicle.current_stop_sequence) if vehicle.HasField("current_stop_sequence") else None
        current_status = int(vehicle.current_status) if vehicle.HasField("current_status") else None
        congestion_level = int(vehicle.congestion_level) if vehicle.HasField("congestion_level") else None
        occupancy_status = int(vehicle.occupancy_status) if vehicle.HasField("occupancy_status") else None
        position_timestamp = int(vehicle.timestamp) if vehicle.HasField("timestamp") else None

        conn.execute(
            """
            INSERT INTO gtfs_rt_vehicle_positions (
                city_id, snapshot_pk,
                entity_id, vehicle_id, vehicle_label,
                trip_id, route_id, stop_id, start_date, start_time, schedule_relationship,
                trip_pk, route_pk,
                latitude, longitude, bearing, speed,
                current_stop_sequence, current_status, congestion_level, occupancy_status,
                position_timestamp, raw_json
            )
            VALUES (
                %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s::jsonb
            )
            """,
            (
                city_id,
                snapshot_pk,
                entity.id if entity.id else None,
                vehicle_id,
                vehicle_label,
                trip_id,
                route_id,
                stop_id,
                start_date,
                start_time,
                schedule_relationship,
                trip_pk,
                route_pk,
                latitude,
                longitude,
                bearing,
                speed,
                current_stop_sequence,
                current_status,
                congestion_level,
                occupancy_status,
                position_timestamp,
                entity_json(entity),
            ),
        )
        inserted += 1

    return inserted


def ingest_trip_updates(conn, city_id: int, snapshot_pk: int, feed: gtfs_realtime_pb2.FeedMessage) -> int:
    inserted = 0

    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue

        tu = entity.trip_update
        trip = tu.trip

        trip_id = trip.trip_id if trip.HasField("trip_id") else None
        route_id = trip.route_id if trip.HasField("route_id") else None

        trip_pk = resolve_trip_pk(conn, city_id, trip_id)
        route_pk = resolve_route_pk(conn, city_id, route_id)

        vehicle_id = tu.vehicle.id if tu.HasField("vehicle") and tu.vehicle.HasField("id") else None
        vehicle_label = tu.vehicle.label if tu.HasField("vehicle") and tu.vehicle.HasField("label") else None

        start_date = trip.start_date if trip.HasField("start_date") else None
        start_time = trip.start_time if trip.HasField("start_time") else None
        schedule_relationship = int(trip.schedule_relationship) if trip.HasField("schedule_relationship") else None
        trip_delay_seconds = int(tu.delay) if tu.HasField("delay") else None
        trip_timestamp = int(tu.timestamp) if tu.HasField("timestamp") else None

        row = conn.execute(
            """
            INSERT INTO gtfs_rt_trip_updates (
                city_id, snapshot_pk,
                entity_id, trip_id, route_id, start_date, start_time, schedule_relationship,
                vehicle_id, vehicle_label, trip_delay_seconds, trip_timestamp,
                trip_pk, route_pk, raw_json
            )
            VALUES (
                %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s::jsonb
            )
            RETURNING rt_trip_update_pk
            """,
            (
                city_id,
                snapshot_pk,
                entity.id if entity.id else None,
                trip_id,
                route_id,
                start_date,
                start_time,
                schedule_relationship,
                vehicle_id,
                vehicle_label,
                trip_delay_seconds,
                trip_timestamp,
                trip_pk,
                route_pk,
                entity_json(entity),
            ),
        ).fetchone()

        rt_trip_update_pk = int(row[0])

        for stu in tu.stop_time_update:
            stop_sequence = int(stu.stop_sequence) if stu.HasField("stop_sequence") else None
            stop_id = stu.stop_id if stu.HasField("stop_id") else None

            arrival_delay_seconds = None
            arrival_time_epoch = None
            if stu.HasField("arrival"):
                arrival_delay_seconds = int(stu.arrival.delay) if stu.arrival.HasField("delay") else None
                arrival_time_epoch = int(stu.arrival.time) if stu.arrival.HasField("time") else None

            departure_delay_seconds = None
            departure_time_epoch = None
            if stu.HasField("departure"):
                departure_delay_seconds = int(stu.departure.delay) if stu.departure.HasField("delay") else None
                departure_time_epoch = int(stu.departure.time) if stu.departure.HasField("time") else None

            stop_schedule_relationship = int(stu.schedule_relationship) if stu.HasField("schedule_relationship") else None

            conn.execute(
                """
                INSERT INTO gtfs_rt_stop_time_updates (
                    city_id, snapshot_pk, rt_trip_update_pk, stop_sequence, stop_id,
                    arrival_delay_seconds, arrival_time_epoch,
                    departure_delay_seconds, departure_time_epoch,
                    schedule_relationship
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    city_id,
                    snapshot_pk,
                    rt_trip_update_pk,
                    stop_sequence,
                    stop_id,
                    arrival_delay_seconds,
                    arrival_time_epoch,
                    departure_delay_seconds,
                    departure_time_epoch,
                    stop_schedule_relationship,
                ),
            )

        # Upsert into current tables so live_stop_arrivals and the trip stops
        # API can read RT ETAs without scanning the partitioned history tables.
        trip_key = trip_id or entity.id or ""
        feed_timestamp = feed.header.timestamp if feed.header.HasField("timestamp") else None

        conn.execute(
            """
            INSERT INTO gtfs_rt_trip_updates_current (
                city_id, trip_key, snapshot_pk, entity_id,
                trip_id, route_id, start_date, start_time, schedule_relationship,
                vehicle_id, vehicle_label, trip_delay_seconds, trip_timestamp,
                feed_timestamp, trip_pk, route_pk, fetched_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (city_id, trip_key) DO UPDATE
            SET snapshot_pk = EXCLUDED.snapshot_pk,
                entity_id = EXCLUDED.entity_id,
                trip_id = EXCLUDED.trip_id,
                route_id = EXCLUDED.route_id,
                start_date = EXCLUDED.start_date,
                start_time = EXCLUDED.start_time,
                schedule_relationship = EXCLUDED.schedule_relationship,
                vehicle_id = EXCLUDED.vehicle_id,
                vehicle_label = EXCLUDED.vehicle_label,
                trip_delay_seconds = EXCLUDED.trip_delay_seconds,
                trip_timestamp = EXCLUDED.trip_timestamp,
                feed_timestamp = EXCLUDED.feed_timestamp,
                trip_pk = EXCLUDED.trip_pk,
                route_pk = EXCLUDED.route_pk,
                updated_at = NOW()
            """,
            (
                city_id, trip_key, snapshot_pk, entity.id or None,
                trip_id, route_id, start_date, start_time, schedule_relationship,
                vehicle_id, vehicle_label, trip_delay_seconds, trip_timestamp,
                feed_timestamp, trip_pk, route_pk,
            ),
        )

        for stu in tu.stop_time_update:
            stop_sequence_c = int(stu.stop_sequence) if stu.HasField("stop_sequence") else None
            stop_id_c = stu.stop_id if stu.HasField("stop_id") else None
            arr_delay_c = int(stu.arrival.delay) if stu.HasField("arrival") and stu.arrival.HasField("delay") else None
            arr_epoch_c = int(stu.arrival.time) if stu.HasField("arrival") and stu.arrival.HasField("time") else None
            dep_delay_c = int(stu.departure.delay) if stu.HasField("departure") and stu.departure.HasField("delay") else None
            dep_epoch_c = int(stu.departure.time) if stu.HasField("departure") and stu.departure.HasField("time") else None
            sched_rel_c = int(stu.schedule_relationship) if stu.HasField("schedule_relationship") else None
            stop_update_key = f"{stop_sequence_c}:{stop_id_c}"

            conn.execute(
                """
                INSERT INTO gtfs_rt_stop_time_updates_current (
                    city_id, trip_key, stop_update_key, stop_sequence, stop_id,
                    arrival_delay_seconds, arrival_time_epoch,
                    departure_delay_seconds, departure_time_epoch,
                    schedule_relationship, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (city_id, trip_key, stop_update_key) DO UPDATE
                SET stop_sequence = EXCLUDED.stop_sequence,
                    stop_id = EXCLUDED.stop_id,
                    arrival_delay_seconds = EXCLUDED.arrival_delay_seconds,
                    arrival_time_epoch = EXCLUDED.arrival_time_epoch,
                    departure_delay_seconds = EXCLUDED.departure_delay_seconds,
                    departure_time_epoch = EXCLUDED.departure_time_epoch,
                    schedule_relationship = EXCLUDED.schedule_relationship,
                    updated_at = NOW()
                """,
                (
                    city_id, trip_key, stop_update_key, stop_sequence_c, stop_id_c,
                    arr_delay_c, arr_epoch_c, dep_delay_c, dep_epoch_c, sched_rel_c,
                ),
            )

        inserted += 1

    return inserted


def translations_to_dict(translated_string) -> dict | None:
    if not translated_string or not getattr(translated_string, "translation", None):
        return None
    data = {}
    for t in translated_string.translation:
        lang = t.language if t.HasField("language") else ""
        text = t.text if t.HasField("text") else ""
        data[lang] = text
    return data or None


def ingest_alerts(conn, city_id: int, snapshot_pk: int, feed: gtfs_realtime_pb2.FeedMessage) -> int:
    inserted = 0

    # Clear stale current alerts for this city before repopulating from new snapshot.
    conn.execute("DELETE FROM gtfs_rt_alerts_current WHERE city_id = %s", (city_id,))

    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue

        alert = entity.alert

        active_periods = []
        for p in alert.active_period:
            active_periods.append({
                "start": int(p.start) if p.HasField("start") else None,
                "end": int(p.end) if p.HasField("end") else None,
            })

        informed_entities = []
        for ie in alert.informed_entity:
            informed_entities.append({
                "agency_id": ie.agency_id if ie.HasField("agency_id") else None,
                "route_id": ie.route_id if ie.HasField("route_id") else None,
                "route_type": int(ie.route_type) if ie.HasField("route_type") else None,
                "trip": MessageToDict(ie.trip, preserving_proto_field_name=True) if ie.HasField("trip") else None,
                "stop_id": ie.stop_id if ie.HasField("stop_id") else None,
                "direction_id": int(ie.direction_id) if ie.HasField("direction_id") else None,
            })

        cause = int(alert.cause) if alert.HasField("cause") else None
        effect = int(alert.effect) if alert.HasField("effect") else None

        conn.execute(
            """
            INSERT INTO gtfs_rt_alerts (
                city_id, snapshot_pk, entity_id, cause, effect,
                url_translations, header_translations, description_translations,
                active_periods, informed_entities, raw_json
            )
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb)
            """,
            (
                city_id,
                snapshot_pk,
                entity.id if entity.id else None,
                cause,
                effect,
                json.dumps(translations_to_dict(alert.url)),
                json.dumps(translations_to_dict(alert.header_text)),
                json.dumps(translations_to_dict(alert.description_text)),
                json.dumps(active_periods),
                json.dumps(informed_entities),
                entity_json(entity),
            ),
        )

        conn.execute(
            """
            INSERT INTO gtfs_rt_alerts_current (
                city_id, entity_id, cause, effect,
                url_translations, header_translations, description_translations,
                active_periods, informed_entities, updated_at
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, NOW())
            ON CONFLICT (city_id, entity_id) DO UPDATE
            SET cause = EXCLUDED.cause,
                effect = EXCLUDED.effect,
                url_translations = EXCLUDED.url_translations,
                header_translations = EXCLUDED.header_translations,
                description_translations = EXCLUDED.description_translations,
                active_periods = EXCLUDED.active_periods,
                informed_entities = EXCLUDED.informed_entities,
                updated_at = NOW()
            """,
            (
                city_id,
                entity.id if entity.id else None,
                cause,
                effect,
                json.dumps(translations_to_dict(alert.url)),
                json.dumps(translations_to_dict(alert.header_text)),
                json.dumps(translations_to_dict(alert.description_text)),
                json.dumps(active_periods),
                json.dumps(informed_entities),
            ),
        )
        inserted += 1

    return inserted


def run_once(db_config: dict, city_code: str, feed_type: str) -> int:
    with psycopg.connect(**db_config) as conn:
        city_id, city_name = get_city_row(conn, city_code)
        source = get_source_config(conn, city_id, feed_type)

        source_value = source["source_value"]
        if source.get("source_encrypted"):
            from app.credentials import decrypt_string
            source_value = decrypt_string(source_value)

        effective_source_kind = normalize_source_kind(source["source_kind"], source_value)

        feed_bytes = load_feed_bytes(
            source_kind=effective_source_kind,
            source_value=source_value,
            headers_json=source["headers_json"],
            query_params_json=source["query_params_json"],
        )
        feed = parse_feed(feed_bytes)
        snapshot_pk = create_snapshot(conn, city_id, feed_type, feed)

        if feed_type == "vehicle_positions":
            inserted = ingest_vehicle_positions(conn, city_id, snapshot_pk, feed)
        elif feed_type == "trip_updates":
            inserted = ingest_trip_updates(conn, city_id, snapshot_pk, feed)
        elif feed_type == "alerts":
            inserted = ingest_alerts(conn, city_id, snapshot_pk, feed)
        else:
            raise RuntimeError(f"Unsupported feed_type: {feed_type}")

        conn.commit()

        print(
            f"Ingested GTFS-RT city={city_code} city_name={city_name} "
            f"feed_type={feed_type} snapshot_pk={snapshot_pk} inserted={inserted}"
        )
        return source["refresh_interval_seconds"] or 30


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city-code", required=True)
    parser.add_argument("--feed-type", required=True, choices=sorted(VALID_FEED_TYPES))
    parser.add_argument("--watch", action="store_true", help="Continuously ingest on the configured refresh interval")
    args = parser.parse_args()

    db_config = load_db_config()

    try:
        if not args.watch:
            run_once(db_config, args.city_code, args.feed_type)
            return

        while True:
            refresh_interval_seconds = run_once(db_config, args.city_code, args.feed_type)
            time.sleep(max(1, refresh_interval_seconds))

    except KeyboardInterrupt:
        print("Stopped GTFS-RT ingestor.")
    except Exception as e:
        print(
            f"GTFS-RT ingest failed city={args.city_code} feed_type={args.feed_type} "
            f"error={sanitize_error_message(str(e))}"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
