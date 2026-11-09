from __future__ import annotations

import os
import sys
import types
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

# Ensure project root is on sys.path so `from transit.*` and `from app.*` work
# regardless of how pytest is invoked (from project root or tests/ directory).
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Let modules that validate DB_PASSWORD at import time load in unit tests.
os.environ.setdefault("DB_PASSWORD", "test-password")
os.environ.setdefault("SIM_CITY_CODE", "test")

# The unit tests below mock database behavior and do not require a real psycopg install.
try:  # pragma: no cover
    import psycopg  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    psycopg_stub = types.ModuleType("psycopg")

    def _missing_connect(*args, **kwargs):
        raise RuntimeError("psycopg is not installed; tests should mock DB access")

    psycopg_stub.connect = _missing_connect
    sys.modules["psycopg"] = psycopg_stub

try:  # pragma: no cover
    import psycopg_pool  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    psycopg_pool_stub = types.ModuleType("psycopg_pool")

    class ConnectionPool:
        def __init__(self, *args, **kwargs):
            self.opened = False

        def open(self):
            self.opened = True

        def close(self):
            self.opened = False

        @contextmanager
        def connection(self):
            yield FakeConn()

    psycopg_pool_stub.ConnectionPool = ConnectionPool
    sys.modules["psycopg_pool"] = psycopg_pool_stub


class FakeCursor:
    def __init__(self, conn: "FakeConn"):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        self.conn.executemany_calls.append((sql, list(rows)))


class FakeResult:
    def __init__(self, rows=None, one=None):
        self._rows = rows or []
        self._one = one

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._rows


@dataclass
class FakeConn:
    execute_calls: list[tuple[str, tuple[Any, ...] | None]] = field(default_factory=list)
    executemany_calls: list[tuple[str, list[tuple[Any, ...]]]] = field(default_factory=list)
    responses: list[FakeResult] = field(default_factory=list)

    def cursor(self):
        return FakeCursor(self)

    def execute(self, sql: str, params: tuple[Any, ...] | None = None):
        self.execute_calls.append((sql, params))
        if self.responses:
            return self.responses.pop(0)
        return FakeResult()

    def commit(self):
        pass


@contextmanager
def conn_context(conn: FakeConn):
    yield conn
