#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

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

        self.project_root = os.path.dirname(os.path.abspath(__file__))
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
                    crc.sim_force_time
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
                WHERE s.is_enabled = TRUE
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

    def build_desired_processes(self) -> dict[str, tuple[list[str], dict]]:
        desired: dict[str, tuple[list[str], dict]] = {}

        if os.getenv("CLEANUP_ENABLED", "true").lower() in ("1", "true", "yes", "on"):
            desired["cleanup"] = (
                [sys.executable, "cleanup_service.py"],
                os.environ.copy(),
            )

        for city in self.get_enabled_cities():
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

            if city.sim_enabled:
                desired[f"simulation:{city.city_code}"] = (
                    [sys.executable, "simulation_service.py"],
                    base_env,
                )

            desired[f"fusion:{city.city_code}"] = (
                [sys.executable, "fusion_service.py"],
                base_env,
            )

            if self.stop_arrivals_enabled:
                desired[f"stop_arrivals:{city.city_code}"] = (
                    [sys.executable, "stop_arrivals_service.py"],
                    base_env,
                )

        for source in self.get_enabled_rt_sources():
            env = os.environ.copy()
            desired[f"ingestor:{source.city_code}:{source.feed_type}"] = (
                [
                    sys.executable,
                    "gtfs_rt_ingestor.py",
                    "--city-code",
                    source.city_code,
                    "--feed-type",
                    source.feed_type,
                    "--watch",
                ],
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
