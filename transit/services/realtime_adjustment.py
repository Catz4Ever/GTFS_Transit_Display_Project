from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from transit.models import StopTimeRow
from transit.services.positioning_service import hhmmss_to_seconds


def seconds_to_hhmmss(value: int) -> str:
    if value < 0:
        value = 0
    h = value // 3600
    m = (value % 3600) // 60
    s = value % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def apply_trip_update_delays(timeline: list[StopTimeRow], stop_updates: Iterable[dict]) -> list[StopTimeRow]:
    """Return a timeline adjusted by GTFS-RT stop_time_update delays.

    Expected stop_update keys: stop_sequence, arrival_delay_seconds, departure_delay_seconds.
    This helper is intentionally side-effect free so it can be tested before wiring it into
    simulation_service.py / stop_arrivals_service.py.
    """
    by_seq: dict[int, dict] = {}
    for upd in stop_updates:
        seq = upd.get("stop_sequence")
        if seq is not None:
            by_seq[int(seq)] = upd

    adjusted: list[StopTimeRow] = []
    last_known_delay: int | None = None

    for stop in timeline:
        upd = by_seq.get(stop.stop_sequence)
        if upd:
            arr_delay = upd.get("arrival_delay_seconds")
            dep_delay = upd.get("departure_delay_seconds")
            delay = dep_delay if dep_delay is not None else arr_delay
            if delay is not None:
                last_known_delay = int(delay)

        delay = last_known_delay or 0
        arr = hhmmss_to_seconds(stop.arrival_time) + delay
        dep = hhmmss_to_seconds(stop.departure_time) + delay
        adjusted.append(replace(stop, arrival_time=seconds_to_hhmmss(arr), departure_time=seconds_to_hhmmss(dep)))

    return adjusted


def estimate_timeline_delay_seconds(stop_updates: Iterable[dict]) -> int | None:
    """Pick a representative delay from a GTFS-RT TripUpdate.

    Prefer the last explicit departure/arrival delay because GTFS-RT delay is often propagated
    forward until the next explicit StopTimeUpdate.
    """
    last: int | None = None
    for upd in stop_updates:
        dep = upd.get("departure_delay_seconds")
        arr = upd.get("arrival_delay_seconds")
        value = dep if dep is not None else arr
        if value is not None:
            last = int(value)
    return last
