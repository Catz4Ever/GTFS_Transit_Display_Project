from __future__ import annotations

import json

import pytest

gtfs_realtime_pb2 = pytest.importorskip("google.transit.gtfs_realtime_pb2")

from app.gtfs_rt import ingestor
from conftest import FakeConn, FakeResult


def make_vehicle_feed() -> gtfs_realtime_pb2.FeedMessage:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = 1_700_000_000

    entity = feed.entity.add()
    entity.id = "entity-1"
    entity.vehicle.trip.trip_id = "T1"
    entity.vehicle.trip.route_id = "R1"
    entity.vehicle.vehicle.id = "V1"
    entity.vehicle.vehicle.label = "Bus 1"
    entity.vehicle.position.latitude = 42.0
    entity.vehicle.position.longitude = 23.0
    entity.vehicle.position.bearing = 90.0
    entity.vehicle.timestamp = 1_700_000_010
    return feed


def test_parse_feed_round_trip_gtfs_realtime_bytes():
    feed = make_vehicle_feed()

    parsed = ingestor.parse_feed(feed.SerializeToString())

    assert parsed.header.gtfs_realtime_version == "2.0"
    assert parsed.entity[0].vehicle.vehicle.id == "V1"


def test_create_snapshot_persists_header_metadata():
    conn = FakeConn(responses=[FakeResult(one=(123,))])
    feed = make_vehicle_feed()

    snapshot_pk = ingestor.create_snapshot(conn, city_id=7, feed_type="vehicle_positions", feed=feed)

    assert snapshot_pk == 123
    sql, params = conn.execute_calls[0]
    assert "INSERT INTO gtfs_rt_feed_snapshots" in sql
    assert params == (7, "vehicle_positions", 1_700_000_000, "2.0", 1)


def test_ingest_vehicle_positions_resolves_trip_route_and_inserts_raw_json():
    conn = FakeConn(
        responses=[
            FakeResult(one=(101,)),  # trip_pk
            FakeResult(one=(202,)),  # route_pk
        ]
    )
    feed = make_vehicle_feed()

    inserted = ingestor.ingest_vehicle_positions(conn, city_id=7, snapshot_pk=55, feed=feed)

    assert inserted == 1
    insert_sql, insert_params = conn.execute_calls[-1]
    assert "INSERT INTO gtfs_rt_vehicle_positions" in insert_sql
    assert insert_params[0] == 7
    assert insert_params[1] == 55
    assert insert_params[2] == "entity-1"
    assert insert_params[3] == "V1"
    assert insert_params[5] == "T1"
    assert insert_params[6] == "R1"
    assert insert_params[11] == 101
    assert insert_params[12] == 202
    assert insert_params[13] == 42.0
    assert insert_params[14] == 23.0
    assert json.loads(insert_params[-1])["vehicle"]["vehicle"]["id"] == "V1"


def test_ingest_vehicle_positions_skips_entities_without_position():
    conn = FakeConn()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.entity.add().id = "no-vehicle"
    entity = feed.entity.add()
    entity.id = "vehicle-without-position"
    entity.vehicle.vehicle.id = "V2"

    inserted = ingestor.ingest_vehicle_positions(conn, city_id=7, snapshot_pk=55, feed=feed)

    assert inserted == 0
    assert conn.execute_calls == []
