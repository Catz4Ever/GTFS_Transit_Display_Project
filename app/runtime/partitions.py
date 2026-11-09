#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create daily PostgreSQL partitions for high-volume runtime tables.

Important:
- The schema should already define the parent tables as PARTITION BY RANGE.
- DEFAULT partitions keep inserts safe if this worker is down.
- This worker pre-creates daily partitions for a configurable window.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg
from dotenv import load_dotenv
from psycopg import sql


@dataclass(frozen=True)
class PartitionedTable:
    parent: str
    key_column: str


# key_column is informational here; parent table partition bounds already define the key.
TABLES = [
    PartitionedTable("vehicle_position_history", "observed_at"),
    PartitionedTable("gtfs_rt_vehicle_positions", "created_at"),
    PartitionedTable("gtfs_rt_trip_updates", "created_at"),
    PartitionedTable("gtfs_rt_stop_time_updates", "created_at"),
    PartitionedTable("gtfs_rt_alerts", "created_at"),
]


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


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
        raise RuntimeError("DB_PASSWORD is not set")
    return cfg


def day_floor(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc)
    return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)


def partition_name(parent: str, start: datetime) -> str:
    safe_parent = re.sub(r"[^a-zA-Z0-9_]", "_", parent)
    return f"{safe_parent}_p{start:%Y%m%d}"


def ensure_runtime_partitions_catalog(conn) -> None:
    # Safe even if schema.sql already created it.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_partitions (
            partition_name TEXT PRIMARY KEY,
            parent_table TEXT NOT NULL,
            range_start TIMESTAMPTZ NOT NULL,
            range_end TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def table_exists(conn, table_name: str) -> bool:
    return conn.execute(
        "SELECT to_regclass(%s) IS NOT NULL",
        (table_name,),
    ).fetchone()[0]


def parent_is_partitioned(conn, parent: str) -> bool:
    row = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_partitioned_table pt
            JOIN pg_class c ON c.oid = pt.partrelid
            WHERE c.relname = %s
        )
        """,
        (parent,),
    ).fetchone()
    return bool(row and row[0])


def default_partition_has_rows(conn, parent: str, start: datetime, end: datetime) -> bool:
    default_name = f"{parent}_default"
    if not table_exists(conn, default_name):
        return False

    # We only need to know whether any rows exist in the default partition. If the default
    # has rows for the new range, PostgreSQL may reject attaching the new partition because
    # it would violate the default partition constraint. The simplest safe production rule is:
    # create dated partitions before ingesting; for test/dev, truncate/migrate defaults first.
    row = conn.execute(
        sql.SQL("SELECT EXISTS (SELECT 1 FROM {} LIMIT 1)").format(
            sql.Identifier(default_name)
        )
    ).fetchone()
    return bool(row and row[0])


def ensure_partition(conn, parent: str, start: datetime, end: datetime) -> bool:
    if not parent_is_partitioned(conn, parent):
        logging.warning("Skipping parent=%s because it is not partitioned", parent)
        return False

    name = partition_name(parent, start)
    if table_exists(conn, name):
        conn.execute(
            """
            INSERT INTO runtime_partitions(parent_table, partition_name, range_start, range_end)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (partition_name) DO NOTHING
            """,
            (parent, name, start, end),
        )
        return False

    # PostgreSQL partition bounds cannot use query parameters. Use sql.Literal.
    query = sql.SQL(
        """
        CREATE TABLE {partition}
        PARTITION OF {parent}
        FOR VALUES FROM ({start_literal}) TO ({end_literal})
        """
    ).format(
        partition=sql.Identifier(name),
        parent=sql.Identifier(parent),
        start_literal=sql.Literal(start),
        end_literal=sql.Literal(end),
    )

    conn.execute(query)
    conn.execute(
        """
        INSERT INTO runtime_partitions(parent_table, partition_name, range_start, range_end)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (partition_name) DO NOTHING
        """,
        (parent, name, start, end),
    )
    return True


def ensure_partitions(conn, days_back: int, days_ahead: int) -> dict[str, int]:
    ensure_runtime_partitions_catalog(conn)
    base = day_floor(datetime.now(timezone.utc))
    created: dict[str, int] = {}

    for table in TABLES:
        count = 0
        for offset in range(-days_back, days_ahead + 1):
            start = base + timedelta(days=offset)
            end = start + timedelta(days=1)
            try:
                if ensure_partition(conn, table.parent, start, end):
                    count += 1
            except psycopg.Error as exc:
                # Most common after ingesting before partitions exist:
                # default partition already contains rows that belong to this date range.
                logging.exception(
                    "Could not create partition parent=%s start=%s end=%s. "
                    "If default partitions already contain runtime rows, stop runtime and "
                    "TRUNCATE the *_default runtime partitions, then restart.",
                    table.parent,
                    start.isoformat(),
                    end.isoformat(),
                )
                raise exc
        created[table.parent] = count

    return created


def run_once(db_config: dict, days_back: int, days_ahead: int) -> dict[str, int]:
    with psycopg.connect(**db_config) as conn:
        lock_timeout_ms = int(os.getenv("PARTITION_LOCK_TIMEOUT_MS", "5000"))
        conn.execute(f"SET lock_timeout = '{int(lock_timeout_ms)}ms'")
        created = ensure_partitions(conn, days_back, days_ahead)
        conn.commit()
        return created


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not env_bool("PARTITION_MANAGER_ENABLED", True):
        logging.info("Partition manager disabled by PARTITION_MANAGER_ENABLED")
        return

    interval = int(os.getenv("PARTITION_MANAGER_INTERVAL_SECONDS", "3600"))
    days_back = int(os.getenv("PARTITION_DAYS_BACK", "0"))
    days_ahead = int(os.getenv("PARTITION_DAYS_AHEAD", "3"))
    db_config = load_db_config()

    logging.info(
        "Starting partition manager interval=%ss days_back=%s days_ahead=%s",
        interval,
        days_back,
        days_ahead,
    )

    while True:
        try:
            created = run_once(db_config, days_back, days_ahead)
            logging.info("Partition manager complete created=%s", created)
        except Exception as exc:
            logging.exception("Partition manager failed: %s", exc)
        time.sleep(interval)

def main_once():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    load_dotenv()

    db_config = {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": os.getenv("DB_NAME", "transit_db"),
        "user": os.getenv("DB_USER", "transit_user"),
        "password": os.getenv("DB_PASSWORD"),
    }

    days_back = int(os.getenv("PARTITION_STARTUP_DAYS_BACK", os.getenv("PARTITION_DAYS_BACK", "0")))
    days_ahead = int(os.getenv("PARTITION_STARTUP_DAYS_AHEAD", os.getenv("PARTITION_DAYS_AHEAD", "3")))

    with psycopg.connect(**db_config) as conn:
        lock_timeout_ms = int(os.getenv("PARTITION_LOCK_TIMEOUT_MS", "5000"))
        conn.execute(f"SET lock_timeout = '{int(lock_timeout_ms)}ms'")
        created = ensure_partitions(conn, days_back, days_ahead)
        conn.commit()

    logging.info("Partition manager one-shot complete created=%s", created)


if __name__ == "__main__":
    import sys

    if "--once" in sys.argv:
        main_once()
    else:
        main()