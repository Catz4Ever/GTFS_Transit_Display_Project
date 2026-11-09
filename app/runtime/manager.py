#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg
from dotenv import load_dotenv


@dataclass(frozen=True)
class CityRuntimeConfig:
    city_code: str
    timezone_name: str
    service_day_rollover_hour: int
    is_enabled: bool
    sim_enabled: bool
    sim_force_date: str | None
    sim_force_time: str | None
    gtfs_import_in_progress: bool
    sim_interval_seconds: int | None
    sim_active_trip_lookbehind_seconds: int | None
    sim_active_trip_lookahead_seconds: int | None
    sim_apply_trip_updates: bool | None
    fusion_interval_seconds: int | None
    fusion_rt_max_age_seconds: int | None
    fusion_min_match_score: float | None
    fusion_max_shape_distance_m: float | None
    stop_arrivals_interval_seconds: int | None
    stop_arrivals_horizon_seconds: int | None
    stop_arrivals_limit_per_stop: int | None
    stop_arrivals_shape_speed_mps: float | None
    stop_arrivals_shape_max_distance_m: float | None
    stop_arrivals_shape_passed_grace_m: float | None


@dataclass(frozen=True)
class RtSourceConfig:
    city_code: str
    feed_type: str
    is_enabled: bool


class RuntimeManager:
    def __init__(self) -> None:
        load_dotenv()

        self.db_config = {
            "host": os.getenv("DB_HOST", "localhost"),
            "port": int(os.getenv("DB_PORT", "5432")),
            "dbname": os.getenv("DB_NAME", "transit_db"),
            "user": os.getenv("DB_USER", "transit_user"),
            "password": os.getenv("DB_PASSWORD"),
        }
        if not self.db_config["password"]:
            raise RuntimeError("DB_PASSWORD is not set in .env")

        self.project_root = str(Path(__file__).resolve().parents[2])
        self.sync_interval_seconds = int(os.getenv("RUNTIME_MANAGER_SYNC_INTERVAL_SECONDS", os.getenv("RUNTIME_RECONCILE_INTERVAL_SECONDS", "15")))
        self.stop_arrivals_enabled = os.getenv("STOP_ARRIVALS_ENABLED", "true").lower() == "true"
        self.running = True
        self.processes: dict[str, subprocess.Popen] = {}

    def stop(self, *_args):
        logging.info("Stopping runtime manager")
        self.running = False
        self.stop_all_processes()

    def connect_db(self):
        return psycopg.connect(**self.db_config)

    def ensure_runtime_schema(self) -> None:
        """Ensure runtime-manager-owned maintenance columns exist.

        This is intentionally lightweight and safe to run repeatedly. It lets
        older dev databases keep working after introducing GTFS maintenance mode.
        """
        with self.connect_db() as conn:
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
            conn.commit()

    def get_enabled_cities(self) -> list[CityRuntimeConfig]:
        with self.connect_db() as conn:
            rows = conn.execute(
                '''
                SELECT
                    c.city_code,
                    COALESCE(crc.timezone_name, 'UTC') AS timezone_name,
                    COALESCE(crc.service_day_rollover_hour, 4) AS service_day_rollover_hour,
                    COALESCE(crc.is_enabled, TRUE) AS is_enabled,
                    COALESCE(crc.sim_enabled, TRUE) AS sim_enabled,
                    crc.sim_force_date,
                    crc.sim_force_time,
                    COALESCE(crc.gtfs_import_in_progress, FALSE) AS gtfs_import_in_progress,
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
                ORDER BY c.city_code
                '''
            ).fetchall()

        return [
            CityRuntimeConfig(
                city_code=str(r[0]),
                timezone_name=str(r[1]),
                service_day_rollover_hour=int(r[2]),
                is_enabled=bool(r[3]),
                sim_enabled=bool(r[4]),
                sim_force_date=str(r[5]) if r[5] is not None else None,
                sim_force_time=str(r[6]) if r[6] is not None else None,
                gtfs_import_in_progress=bool(r[7]),
                sim_interval_seconds=int(r[8]) if r[8] is not None else None,
                sim_active_trip_lookbehind_seconds=int(r[9]) if r[9] is not None else None,
                sim_active_trip_lookahead_seconds=int(r[10]) if r[10] is not None else None,
                sim_apply_trip_updates=bool(r[11]) if r[11] is not None else None,
                fusion_interval_seconds=int(r[12]) if r[12] is not None else None,
                fusion_rt_max_age_seconds=int(r[13]) if r[13] is not None else None,
                fusion_min_match_score=float(r[14]) if r[14] is not None else None,
                fusion_max_shape_distance_m=float(r[15]) if r[15] is not None else None,
                stop_arrivals_interval_seconds=int(r[16]) if r[16] is not None else None,
                stop_arrivals_horizon_seconds=int(r[17]) if r[17] is not None else None,
                stop_arrivals_limit_per_stop=int(r[18]) if r[18] is not None else None,
                stop_arrivals_shape_speed_mps=float(r[19]) if r[19] is not None else None,
                stop_arrivals_shape_max_distance_m=float(r[20]) if r[20] is not None else None,
                stop_arrivals_shape_passed_grace_m=float(r[21]) if r[21] is not None else None,
            )
            for r in rows
            if bool(r[3])
        ]

    def get_enabled_rt_sources(self) -> list[RtSourceConfig]:
        with self.connect_db() as conn:
            rows = conn.execute(
                '''
                SELECT
                    c.city_code,
                    s.feed_type,
                    s.is_enabled
                FROM gtfs_rt_sources s
                JOIN cities c ON c.city_id = s.city_id
                LEFT JOIN city_runtime_config crc ON crc.city_id = s.city_id
                WHERE s.is_enabled = TRUE
                  AND COALESCE(crc.is_enabled, TRUE) = TRUE
                ORDER BY c.city_code, s.feed_type
                '''
            ).fetchall()

        return [
            RtSourceConfig(
                city_code=str(r[0]),
                feed_type=str(r[1]),
                is_enabled=bool(r[2]),
            )
            for r in rows
            if bool(r[2])
        ]


    def module_cmd(self, module: str, *args: str) -> list[str]:
        return [sys.executable, "-m", module, *args]

    def partition_manager_enabled(self) -> bool:
        return os.getenv("PARTITION_MANAGER_ENABLED", "true").lower() in ("1", "true", "yes", "on")

    def partition_manager_background_enabled(self) -> bool:
        return os.getenv("PARTITION_MANAGER_BACKGROUND_ENABLED", "false").lower() in ("1", "true", "yes", "on")

    @staticmethod
    def apply_city_runtime_overrides(env: dict, city: CityRuntimeConfig) -> None:
        overrides = {
            "SIM_INTERVAL_SECONDS": city.sim_interval_seconds,
            "SIM_ACTIVE_TRIP_LOOKBEHIND_SECONDS": city.sim_active_trip_lookbehind_seconds,
            "SIM_ACTIVE_TRIP_LOOKAHEAD_SECONDS": city.sim_active_trip_lookahead_seconds,
            "SIM_APPLY_TRIP_UPDATES": city.sim_apply_trip_updates,
            "FUSION_INTERVAL_SECONDS": city.fusion_interval_seconds,
            "FUSION_RT_MAX_AGE_SECONDS": city.fusion_rt_max_age_seconds,
            "FUSION_MIN_MATCH_SCORE": city.fusion_min_match_score,
            "FUSION_MAX_SHAPE_DISTANCE_M": city.fusion_max_shape_distance_m,
            "STOP_ARRIVALS_INTERVAL_SECONDS": city.stop_arrivals_interval_seconds,
            "STOP_ARRIVALS_HORIZON_SECONDS": city.stop_arrivals_horizon_seconds,
            "STOP_ARRIVALS_LIMIT_PER_STOP": city.stop_arrivals_limit_per_stop,
            "STOP_ARRIVALS_SHAPE_SPEED_MPS": city.stop_arrivals_shape_speed_mps,
            "STOP_ARRIVALS_SHAPE_MAX_DISTANCE_M": city.stop_arrivals_shape_max_distance_m,
            "STOP_ARRIVALS_SHAPE_PASSED_GRACE_M": city.stop_arrivals_shape_passed_grace_m,
        }
        for env_name, value in overrides.items():
            if value is None:
                continue
            if isinstance(value, bool):
                env[env_name] = "true" if value else "false"
            else:
                env[env_name] = str(value)

    def build_desired_processes(self) -> dict[str, tuple[list[str], dict]]:
        desired: dict[str, tuple[list[str], dict]] = {}

        if self.partition_manager_enabled() and self.partition_manager_background_enabled():
            desired["partition_manager"] = (
                self.module_cmd("app.runtime.partitions"),
                os.environ.copy(),
            )

        if os.getenv("CLEANUP_ENABLED", "true").lower() in ("1", "true", "yes", "on"):
            desired["cleanup"] = (
                self.module_cmd("app.runtime.cleanup"),
                os.environ.copy(),
            )

        if os.getenv("GTFS_REFRESH_ENABLED", "true").lower() in ("1", "true", "yes", "on"):
            desired["gtfs_refresh"] = (
                self.module_cmd("app.runtime.gtfs_refresh"),
                os.environ.copy(),
            )

        cities = self.get_enabled_cities()
        maintenance_city_codes = {
            city.city_code for city in cities if city.gtfs_import_in_progress
        }

        for city in cities:
            if city.gtfs_import_in_progress:
                logging.info(
                    "City in GTFS maintenance mode; city workers will be stopped city=%s",
                    city.city_code,
                )
                continue

            base_env = os.environ.copy()
            base_env["SIM_CITY_CODE"] = city.city_code
            base_env["SIM_SERVICE_DAY_ROLLOVER_HOUR"] = str(city.service_day_rollover_hour)
            base_env["TZ"] = city.timezone_name

            if city.sim_force_date:
                base_env["SIM_FORCE_DATE"] = city.sim_force_date
            else:
                base_env.pop("SIM_FORCE_DATE", None)

            if city.sim_force_time:
                base_env["SIM_FORCE_TIME"] = city.sim_force_time
            else:
                base_env.pop("SIM_FORCE_TIME", None)

            self.apply_city_runtime_overrides(base_env, city)

            if city.sim_enabled:
                desired[f"simulation:{city.city_code}"] = (
                    self.module_cmd("app.simulation.service"),
                    base_env,
                )

            desired[f"fusion:{city.city_code}"] = (
                self.module_cmd("app.fusion.service"),
                base_env,
            )

            if self.stop_arrivals_enabled:
                desired[f"stop_arrivals:{city.city_code}"] = (
                    self.module_cmd("app.arrivals.service"),
                    base_env,
                )

        for source in self.get_enabled_rt_sources():
            if source.city_code in maintenance_city_codes:
                logging.info(
                    "City in GTFS maintenance mode; RT ingestor will be stopped city=%s feed_type=%s",
                    source.city_code,
                    source.feed_type,
                )
                continue

            env = os.environ.copy()
            desired[f"ingestor:{source.city_code}:{source.feed_type}"] = (
                self.module_cmd(
                    "app.gtfs_rt.ingestor",
                    "--city-code",
                    source.city_code,
                    "--feed-type",
                    source.feed_type,
                    "--watch",
                ),
                env,
            )

        return desired

    def start_process(self, key: str, cmd: list[str], env: dict):
        logging.info("Starting managed process key=%s cmd=%s", key, " ".join(cmd))
        proc = subprocess.Popen(
            cmd,
            cwd=self.project_root,
            env=env,
        )
        self.processes[key] = proc

    def stop_process(self, key: str):
        proc = self.processes.get(key)
        if not proc:
            return

        logging.info("Stopping managed process key=%s pid=%s", key, proc.pid)
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            self.processes.pop(key, None)

    def stop_all_processes(self):
        for key in list(self.processes.keys()):
            self.stop_process(key)

    def reconcile(self):
        desired = self.build_desired_processes()
        desired_keys = set(desired.keys())
        existing_keys = set(self.processes.keys())

        for key in sorted(existing_keys - desired_keys):
            self.stop_process(key)

        for key in sorted(existing_keys & desired_keys):
            proc = self.processes[key]
            if proc.poll() is not None:
                logging.warning("Managed process exited key=%s returncode=%s; restarting", key, proc.returncode)
                self.stop_process(key)
                cmd, env = desired[key]
                self.start_process(key, cmd, env)

        for key in sorted(desired_keys - existing_keys):
            cmd, env = desired[key]
            self.start_process(key, cmd, env)

        logging.info(
            "Runtime manager reconcile complete desired=%s running=%s",
            len(desired_keys),
            len(self.processes),
        )

    def run(self):
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        logging.info("Starting runtime manager")
        self.ensure_runtime_schema()

        if self.partition_manager_enabled():
            logging.info("Ensuring partitions before starting managed workers")
            result = subprocess.run(
                self.module_cmd("app.runtime.partitions", "--once"),
                cwd=self.project_root,
                env=os.environ.copy(),
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError("Partition manager failed during startup")

        while self.running:
            self.reconcile()
            time.sleep(self.sync_interval_seconds)


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        RuntimeManager().run()
    except Exception as e:
        logging.exception("Runtime manager failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
