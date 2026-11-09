"""
Shared psycopg ConnectionPool for the FastAPI process.

Imported once at module load; the pool is opened lazily on first use
and closed when the application shuts down (via the lifespan hook in
main.py).  Worker processes (fusion, simulation, arrivals) keep using
their own long-lived psycopg.connect() connections — the pool is only
for the API.
"""

import os
from contextlib import contextmanager

from dotenv import load_dotenv
from psycopg_pool import ConnectionPool

load_dotenv()

_conninfo = (
    f"host={os.getenv('DB_HOST', 'localhost')} "
    f"port={os.getenv('DB_PORT', '5432')} "
    f"dbname={os.getenv('DB_NAME', 'transit_db')} "
    f"user={os.getenv('DB_USER', 'transit_user')} "
    f"password={os.getenv('DB_PASSWORD', '')}"
)

if not os.getenv("DB_PASSWORD"):
    raise RuntimeError("DB_PASSWORD is not set in .env")

# min_size=1 keeps one connection warm; max_size caps burst usage.
# Both values are tunable via env vars without code changes.
pool: ConnectionPool = ConnectionPool(
    conninfo=_conninfo,
    min_size=int(os.getenv("DB_POOL_MIN_SIZE", "1")),
    max_size=int(os.getenv("DB_POOL_MAX_SIZE", "10")),
    open=False,  # opened explicitly in the lifespan hook
)


@contextmanager
def get_conn():
    """Yield a pooled connection, returning it to the pool on exit."""
    with pool.connection() as conn:
        yield conn
