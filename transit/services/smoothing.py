from __future__ import annotations

from dataclasses import dataclass

from transit.services.map_matching import haversine_m


@dataclass(frozen=True)
class SmoothedPosition:
    lat: float
    lon: float
    used_previous: bool
    jump_distance_m: float | None
    reset_reason: str | None = None


def smooth_position(
    previous: dict | None,
    current_lat: float,
    current_lon: float,
    alpha: float = 0.70,
    max_jump_m: float = 500.0,
    min_dt_seconds: float = 1.0,
    max_dt_seconds: float = 180.0,
) -> SmoothedPosition:
    if not previous:
        return SmoothedPosition(current_lat, current_lon, False, None, "no_previous")

    prev_lat = float(previous["lat"])
    prev_lon = float(previous["lon"])
    dt = previous.get("dt_seconds")
    if dt is not None and (float(dt) < min_dt_seconds or float(dt) > max_dt_seconds):
        return SmoothedPosition(current_lat, current_lon, False, None, "dt_out_of_range")

    jump = haversine_m(prev_lat, prev_lon, current_lat, current_lon)
    if jump > max_jump_m:
        return SmoothedPosition(current_lat, current_lon, False, jump, "teleport_reset")

    a = max(0.0, min(1.0, alpha))
    return SmoothedPosition(
        lat=a * current_lat + (1.0 - a) * prev_lat,
        lon=a * current_lon + (1.0 - a) * prev_lon,
        used_previous=True,
        jump_distance_m=jump,
    )
