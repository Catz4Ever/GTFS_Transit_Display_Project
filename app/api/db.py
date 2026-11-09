import os

import psycopg
from dotenv import load_dotenv

from app.api.pool import get_conn  # noqa: F401 – re-exported for routers

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "transit_db"),
    "user": os.getenv("DB_USER", "transit_user"),
    "password": os.getenv("DB_PASSWORD"),
}

if not DB_CONFIG["password"]:
    raise RuntimeError("DB_PASSWORD is not set in .env")


def get_db_connection():
    """Legacy helper: opens a direct (unpooled) connection.

    Retained for the admin import flow and the city_import_service,
    which run outside the normal request lifecycle.  All API request
    handlers should use get_conn() from app.api.pool instead.
    """
    return psycopg.connect(**DB_CONFIG)
