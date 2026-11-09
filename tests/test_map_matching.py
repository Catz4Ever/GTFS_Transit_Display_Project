from __future__ import annotations

import pytest

from transit.models import ShapePoint
from transit.services.map_matching import (
    bearing_delta_deg,
    bearing_deg,
    haversine_m,
    match_point_to_shape,
    shape_bearing_at_match,
)


def test_haversine_zero_distance():
    assert haversine_m(42.0, 23.0, 42.0, 23.0) == pytest.approx(0.0)


def test_bearing_delta_wraps_across_zero_degrees():
    assert bearing_delta_deg(350, 10) == pytest.approx(20.0)
    assert bearing_delta_deg(None, 10) is None


def test_match_point_to_shape_projects_onto_nearest_segment():
    shape = [
        ShapePoint(lat=42.0, lon=23.0, seq=1, shape_dist_traveled=0.0),
        ShapePoint(lat=42.0, lon=23.01, seq=2, shape_dist_traveled=1000.0),
    ]

    match = match_point_to_shape(42.001, 23.005, shape)

    assert match is not None
    assert match.segment_index == 0
    assert match.segment_fraction == pytest.approx(0.5, abs=0.02)
    assert match.shape_dist_traveled == pytest.approx(500.0, abs=25.0)
    assert match.matched_lat == pytest.approx(42.0, abs=0.0001)
    assert match.matched_lon == pytest.approx(23.005, abs=0.0001)


def test_match_point_to_single_shape_point_returns_that_point():
    shape = [ShapePoint(lat=42.0, lon=23.0, seq=1, shape_dist_traveled=12.5)]

    match = match_point_to_shape(42.1, 23.1, shape)

    assert match is not None
    assert match.matched_lat == 42.0
    assert match.matched_lon == 23.0
    assert match.shape_dist_traveled == 12.5
    assert match.segment_index == 0


def test_shape_bearing_at_match_uses_matched_segment():
    shape = [
        ShapePoint(lat=42.0, lon=23.0, seq=1, shape_dist_traveled=0.0),
        ShapePoint(lat=42.01, lon=23.0, seq=2, shape_dist_traveled=1000.0),
    ]
    match = match_point_to_shape(42.005, 23.001, shape)

    assert shape_bearing_at_match(shape, match) == pytest.approx(bearing_deg(42.0, 23.0, 42.01, 23.0))
