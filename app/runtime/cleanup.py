#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cleanup old runtime data.

Partitioning mode does two things:
1. Drops old full dated partitions when an entire partition is past retention.
2. Deletes old rows inside the active/current partition using hourly retention.

Why both are needed:
- Daily partitions are efficient for dropping old days.
- But if retention is 1-6 hours, the current day partition can still grow a lot.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import psycopg
from dotenv import load_dotenv
from psycopg import sql


PARTITION_RETENTIONS = {
    "gtfs_rt_stop_time_updates": ("created_at", "RT_STOP_TIME_RETENTION_HOURS", 6),
    "gtfs_rt_trip_updates": ("created_at", "RT_TRIP_RETENTION_HOURS", 6),
    "gtfs_rt_vehicle_positions": ("created_at", "RT_VEHICLE_RETENTION_HOURS", 6),
    "gtfs_rt_alerts": ("created_at", "RT_ALERT_RETENTION_HOURS", 24),
    "vehicle_position_history": ("observed_at", "POSITION_HISTORY_RETENTION_HOURS", 6),
}

DEFAULT_PARTITIONS = {
    "gtfs_rt_stop_time_updates": "gtfs_rt_stop_time_updates_default",
    "gtfs_rt_trip_updates": "gtfs_rt_trip_updates_default",
    "gtfs_rt_vehicle_positions": "gtfs_rt_vehicle_positions_default",
    "gtfs_rt_alerts": "gtfs_rt_alerts_default",
    "vehicle_position_history": "vehicle_position_history_default",
}


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


def retention_hours(env_name: str, default: int) -> int:
    return int(os.getenv(env_name, str(default)))


def cutoff_for(env_name: str, default: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=retention_hours(env_name, default))


def safe_identifier(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


def drop_old_partitions(conn) -> dict[str, int]:
    deleted: dict[str, int] = {}
    for parent, (_column, env_name, default_hours) in PARTITION_RETENTIONS.items():
        cutoff = cutoff_for(env_name, default_hours)
        rows = conn.execute(
            """
            SELECT partition_name
            FROM runtime_partitions
            WHERE parent_table = %s AND range_end < %s
            ORDER BY range_start
            """,
            (parent, cutoff),
        ).fetchall()
        count = 0
        for (partition_name,) in rows:
            partition_name = str(partition_name)
            if not safe_identifier(partition_name):
                logging.warning("Skipping unsafe partition name=%s", partition_name)
                continue
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(partition_name)))
            conn.execute("DELETE FROM runtime_partitions WHERE partition_name = %s", (partition_name,))
            count += 1
        deleted[parent] = count
    return deleted


def cleanup_active_partition_rows_with_counts(conn) -> dict[str, int]:
    """Delete rows older than retention from active partitions/current day.

    This is intentionally separate from dropping full partitions. With daily
    partitions and a 1-hour retention, the active daily partition would otherwise
    grow all day.

    Important order:
    - gtfs_rt_stop_time_updates first, because it depends on gtfs_rt_trip_updates.
    - gtfs_rt_trip_updates after children are deleted.
    """
    deleted: dict[str, int] = {}

    with conn.cursor() as cur:
        stop_cutoff = cutoff_for("RT_STOP_TIME_RETENTION_HOURS", 6)
        trip_cutoff = cutoff_for("RT_TRIP_RETENTION_HOURS", 6)
        vehicle_cutoff = cutoff_for("RT_VEHICLE_RETENTION_HOURS", 6)
        alert_cutoff = cutoff_for("RT_ALERT_RETENTION_HOURS", 24)
        history_cutoff = cutoff_for("POSITION_HISTORY_RETENTION_HOURS", 6)

        # Child table first. Use trip_update cutoff so child rows are deleted
        # before deleting old parent trip update rows.
        cur.execute(
            """
            DELETE FROM gtfs_rt_stop_time_updates stu
            USING gtfs_rt_trip_updates tu
            WHERE stu.rt_trip_update_pk = tu.rt_trip_update_pk
              AND tu.created_at < %s
            """,
            (trip_cutoff,),
        )
        deleted["gtfs_rt_stop_time_updates"] = cur.rowcount

        cur.execute(
            "DELETE FROM gtfs_rt_trip_updates WHERE created_at < %s",
            (trip_cutoff,),
        )
        deleted["gtfs_rt_trip_updates"] = cur.rowcount

        cur.execute(
            "DELETE FROM gtfs_rt_vehicle_positions WHERE created_at < %s",
            (vehicle_cutoff,),
        )
        deleted["gtfs_rt_vehicle_positions"] = cur.rowcount

        cur.execute(
            "DELETE FROM gtfs_rt_alerts WHERE created_at < %s",
            (alert_cutoff,),
        )
        deleted["gtfs_rt_alerts"] = cur.rowcount

        cur.execute(
            "DELETE FROM vehicle_position_history WHERE observed_at < %s",
            (history_cutoff,),
        )
        deleted["vehicle_position_history"] = cur.rowcount

    return deleted


def cleanup_default_partitions_with_counts(conn) -> dict[str, int]:
    deleted: dict[str, int] = {}
    with conn.cursor() as cur:
        # Child default table first, otherwise deleting trip updates may hit FK constraints.
        trip_cutoff = cutoff_for("RT_TRIP_RETENTION_HOURS", 6)
        cur.execute(
            """
            DELETE FROM gtfs_rt_stop_time_updates_default stu
            USING gtfs_rt_trip_updates_default tu
            WHERE stu.rt_trip_update_pk = tu.rt_trip_update_pk
              AND tu.created_at < %s
            """,
            (trip_cutoff,),
        )
        deleted["gtfs_rt_stop_time_updates_default"] = cur.rowcount

        # The rest can be deleted directly.
        for parent, (column, env_name, default_hours) in PARTITION_RETENTIONS.items():
            if parent == "gtfs_rt_stop_time_updates":
                continue
            default_table = DEFAULT_PARTITIONS[parent]
            cutoff = cutoff_for(env_name, default_hours)
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE {} < %s").format(
                    sql.Identifier(default_table),
                    sql.Identifier(column),
                ),
                (cutoff,),
            )
            deleted[default_table] = cur.rowcount
    return deleted


def cleanup_snapshots(conn) -> int:
    # Keep snapshots slightly longer than their dependent RT rows. ON DELETE CASCADE
    # remains as a safety net for any old dependent rows.
    hours = int(os.getenv("RT_SNAPSHOT_RETENTION_HOURS", "24"))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM gtfs_rt_feed_snapshots WHERE fetched_at < %s", (cutoff,))
        return cur.rowcount


def legacy_delete_cleanup(conn) -> dict[str, int]:
    deleted: dict[str, int] = {}
    with conn.cursor() as cur:
        trip_cutoff = cutoff_for("RT_TRIP_RETENTION_HOURS", 6)
        cur.execute(
            """
            DELETE FROM gtfs_rt_stop_time_updates stu
            USING gtfs_rt_trip_updates tu
            WHERE stu.rt_trip_update_pk = tu.rt_trip_update_pk
              AND tu.created_at < %s
            """,
            (trip_cutoff,),
        )
        deleted["gtfs_rt_stop_time_updates"] = cur.rowcount

        for table, (column, env_name, default_hours) in PARTITION_RETENTIONS.items():
            if table == "gtfs_rt_stop_time_updates":
                continue
            cutoff = cutoff_for(env_name, default_hours)
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE {} < %s").format(
                    sql.Identifier(table),
                    sql.Identifier(column),
                ),
                (cutoff,),
            )
            deleted[table] = cur.rowcount

        cur.execute(
            "DELETE FROM gtfs_rt_feed_snapshots WHERE fetched_at < %s",
            (cutoff_for("RT_SNAPSHOT_RETENTION_HOURS", 24),),
        )
        deleted["gtfs_rt_feed_snapshots"] = cur.rowcount
    return deleted


def vacuum_tables(db_config: dict) -> None:
    tables = list(PARTITION_RETENTIONS.keys()) + ["gtfs_rt_feed_snapshots"]
    with psycopg.connect(**db_config, autocommit=True) as conn:
        with conn.cursor() as cur:
            for table in tables:
                cur.execute(sql.SQL("VACUUM ANALYZE {}").format(sql.Identifier(table)))


def cleanup(db_config: dict) -> None:
    partitioning_enabled = env_bool("PARTITIONING_ENABLED", True)
    vacuum_enabled = env_bool("CLEANUP_VACUUM_ENABLED", False)
    active_delete_enabled = env_bool("CLEANUP_ACTIVE_PARTITION_DELETE_ENABLED", True)
    started = datetime.now(timezone.utc)

    with psycopg.connect(**db_config) as conn:
        if partitioning_enabled:
            dropped_partitions = drop_old_partitions(conn)
            active_deleted = (
                cleanup_active_partition_rows_with_counts(conn)
                if active_delete_enabled
                else {}
            )
            default_deleted = cleanup_default_partitions_with_counts(conn)
            snapshots_deleted = cleanup_snapshots(conn)
            conn.commit()
            logging.info(
                "[cleanup] partition cleanup completed started_at=%s "
                "dropped_partitions=%s active_deleted=%s default_deleted=%s snapshots_deleted=%s",
                started.isoformat(),
                dropped_partitions,
                active_deleted,
                default_deleted,
                snapshots_deleted,
            )
        else:
            deleted = legacy_delete_cleanup(conn)
            conn.commit()
            logging.info("[cleanup] legacy delete cleanup completed started_at=%s deleted=%s", started.isoformat(), deleted)

    if vacuum_enabled:
        vacuum_tables(db_config)
        logging.info("[cleanup] vacuum analyze completed")


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not env_bool("CLEANUP_ENABLED", True):
        logging.info("Cleanup service disabled by CLEANUP_ENABLED")
        return

    interval = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "1800"))
    db_config = load_db_config()
    logging.info(
        "Starting cleanup service interval=%ss partitioning=%s active_partition_delete=%s vacuum_enabled=%s",
        interval,
        env_bool("PARTITIONING_ENABLED", True),
        env_bool("CLEANUP_ACTIVE_PARTITION_DELETE_ENABLED", True),
        env_bool("CLEANUP_VACUUM_ENABLED", False),
    )
    while True:
        try:
            cleanup(db_config)
        except Exception as exc:
            logging.exception("[cleanup] failed: %s", exc)
        time.sleep(interval)


if __name__ == "__main__":
    main()
