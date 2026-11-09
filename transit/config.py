from dataclasses import dataclass
import os

from dotenv import load_dotenv


@dataclass
class Settings:
    db_config: dict
    city_code: str
    interval_seconds: int
    mqtt_enabled: bool
    mqtt_host: str
    mqtt_port: int
    mqtt_keepalive: int
    mqtt_topic_prefix: str
    mqtt_username: str
    mqtt_password: str
    force_date: str | None
    force_time: str | None
    service_day_rollover_hour: int
    log_level: str


def load_settings() -> Settings:
    load_dotenv()

    db_config = {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": os.getenv("DB_NAME", "transit_db"),
        "user": os.getenv("DB_USER", "transit_user"),
        "password": os.getenv("DB_PASSWORD"),
    }

    if not db_config["password"]:
        raise RuntimeError("DB_PASSWORD is not set in .env")

    city_code = os.getenv("SIM_CITY_CODE")
    if not city_code:
        raise RuntimeError("SIM_CITY_CODE is required")

    return Settings(
        db_config=db_config,
        city_code=city_code,
        interval_seconds=int(os.getenv("SIM_INTERVAL_SECONDS", "5")),
        mqtt_enabled=os.getenv("MQTT_ENABLED", "true").lower() == "true",
        mqtt_host=os.getenv("MQTT_HOST", "localhost"),
        mqtt_port=int(os.getenv("MQTT_PORT", "1883")),
        mqtt_keepalive=int(os.getenv("MQTT_KEEPALIVE", "60")),
        mqtt_topic_prefix=os.getenv("MQTT_TOPIC_PREFIX", "transit"),
        mqtt_username=os.getenv("MQTT_USERNAME", ""),
        mqtt_password=os.getenv("MQTT_PASSWORD", ""),
        force_date=os.getenv("SIM_FORCE_DATE"),
        force_time=os.getenv("SIM_FORCE_TIME"),
        service_day_rollover_hour=int(os.getenv("SIM_SERVICE_DAY_ROLLOVER_HOUR", "4")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
