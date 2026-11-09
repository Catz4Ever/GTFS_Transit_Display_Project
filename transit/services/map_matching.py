from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from transit.models import ShapePoint

EARTH_RADIUS_M = 6371000.0


@dataclass(frozen=True)
class MapMatchResult:
    matched_lat: float
    matched_lon: float
    distance_from_shape_m: float
    shape_dist_traveled: float | None
    segment_index: int
    segment_fraction: float


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return EARTH_RADIUS_M * 2 * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def bearing_delta_deg(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    diff = abs((float(a) - float(b) + 180.0) % 360.0 - 180.0)
    return diff


def _project_to_segment_local_m(
    lat: float,
    lon: float,
    a: ShapePoint,
    b: ShapePoint,
) -> tuple[float, float, float, float]:
    """Project WGS84 point to segment using an equirectangular local meter plane."""
    lat0 = math.radians((a.lat + b.lat + lat) / 3.0)
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(lat0)

    ax = a.lon * m_per_deg_lon
    ay = a.lat * m_per_deg_lat
    bx = b.lon * m_per_deg_lon
    by = b.lat * m_per_deg_lat
    px = lon * m_per_deg_lon
    py = lat * m_per_deg_lat

    vx = bx - ax
    vy = by - ay
    wx = px - ax
    wy = py - ay
    denom = vx * vx + vy * vy
    if denom <= 1e-9:
        t = 0.0
    else:
        t = max(0.0, min(1.0, (wx * vx + wy * vy) / denom))

    qx = ax + t * vx
    qy = ay + t * vy
    qlon = qx / m_per_deg_lon if abs(m_per_deg_lon) > 1e-9 else a.lon
    qlat = qy / m_per_deg_lat
    dist = math.hypot(px - qx, py - qy)
    return qlat, qlon, dist, t


def _segment_shape_distance(a: ShapePoint, b: ShapePoint, fraction: float) -> float | None:
    if a.shape_dist_traveled is not None and b.shape_dist_traveled is not None:
        return float(a.shape_dist_traveled) + fraction * (float(b.shape_dist_traveled) - float(a.shape_dist_traveled))
    return None


def match_point_to_shape(lat: float, lon: float, shape_points: Sequence[ShapePoint]) -> MapMatchResult | None:
    if not shape_points:
        return None
    if len(shape_points) == 1:
        p = shape_points[0]
        return MapMatchResult(p.lat, p.lon, haversine_m(lat, lon, p.lat, p.lon), p.shape_dist_traveled, 0, 0.0)

    best: MapMatchResult | None = None
    for i in range(len(shape_points) - 1):
        a = shape_points[i]
        b = shape_points[i + 1]
        qlat, qlon, dist, fraction = _project_to_segment_local_m(lat, lon, a, b)
        dist_h = haversine_m(lat, lon, qlat, qlon)
        shape_dist = _segment_shape_distance(a, b, fraction)
        result = MapMatchResult(qlat, qlon, dist_h if math.isfinite(dist_h) else dist, shape_dist, i, fraction)
        if best is None or result.distance_from_shape_m < best.distance_from_shape_m:
            best = result
    return best


def shape_bearing_at_match(shape_points: Sequence[ShapePoint], match: MapMatchResult | None) -> float | None:
    if match is None or len(shape_points) < 2:
        return None
    i = min(max(match.segment_index, 0), len(shape_points) - 2)
    a = shape_points[i]
    b = shape_points[i + 1]
    return bearing_deg(a.lat, a.lon, b.lat, b.lon)
