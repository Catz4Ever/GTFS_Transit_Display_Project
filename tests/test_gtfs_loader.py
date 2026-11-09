from __future__ import annotations

import zipfile

import pytest

from app.ingest import gtfs_loader
from conftest import FakeConn


def make_gtfs_zip(path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)


def test_validate_required_files_accepts_minimal_feed(tmp_path):
    zip_path = tmp_path / "feed.zip"
    make_gtfs_zip(
        zip_path,
        {
            "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,Main,42.0,23.0\n",
            "routes.txt": "route_id,route_type\nR1,3\n",
            "trips.txt": "route_id,service_id,trip_id,shape_id\nR1,WEEK,T1,SH1\n",
            "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nT1,08:00:00,08:00:00,S1,1\n",
            "shapes.txt": "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\nSH1,42.0,23.0,1\n",
        },
    )

    with zipfile.ZipFile(zip_path) as zf:
        gtfs_loader.validate_required_files(zf)


def test_validate_required_files_reports_missing_names(tmp_path):
    zip_path = tmp_path / "feed.zip"
    make_gtfs_zip(zip_path, {"stops.txt": "stop_id\nS1\n"})

    with zipfile.ZipFile(zip_path) as zf:
        with pytest.raises(FileNotFoundError) as exc:
            gtfs_loader.validate_required_files(zf)

    message = str(exc.value)
    assert "routes.txt" in message
    assert "trips.txt" in message


def test_read_csv_from_zip_handles_utf8_bom(tmp_path):
    zip_path = tmp_path / "feed.zip"
    make_gtfs_zip(zip_path, {"stops.txt": "\ufeffstop_id,stop_name\nS1,Main\n"})

    with zipfile.ZipFile(zip_path) as zf:
        rows = list(gtfs_loader.read_csv_from_zip(zf, "stops.txt"))

    assert rows == [{"stop_id": "S1", "stop_name": "Main"}]


def test_collect_service_and_shape_ids_from_calendar_trips_and_shapes(tmp_path):
    zip_path = tmp_path / "feed.zip"
    make_gtfs_zip(
        zip_path,
        {
            "trips.txt": "route_id,service_id,trip_id,shape_id\nR1,WEEK,T1,SH_FROM_TRIPS\n",
            "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nWEEK,1,1,1,1,1,0,0,20250101,20251231\n",
            "calendar_dates.txt": "service_id,date,exception_type\nHOLIDAY,20250101,1\n",
            "shapes.txt": "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\nSH_FROM_SHAPES,42,23,1\n",
        },
    )

    with zipfile.ZipFile(zip_path) as zf:
        assert gtfs_loader.collect_service_ids(zf) == {"WEEK", "HOLIDAY"}
        assert gtfs_loader.collect_shape_ids(zf) == {"SH_FROM_TRIPS", "SH_FROM_SHAPES"}


def test_import_stops_skips_invalid_rows_and_batches_valid_rows():
    conn = FakeConn()

    count = gtfs_loader.import_stops(
        conn,
        city_id=7,
        rows=[
            {"stop_id": "S1", "stop_name": "Main", "stop_lat": "42.0", "stop_lon": "23.0", "location_type": "0"},
            {"stop_id": "S2", "stop_name": "Missing lon", "stop_lat": "42.1", "stop_lon": ""},
        ],
        batch_size=10,
    )

    assert count == 1
    assert len(conn.executemany_calls) == 1
    inserted_row = conn.executemany_calls[0][1][0]
    assert inserted_row[0:6] == (7, "S1", None, "Main", None, 42.0)
    assert inserted_row[6] == 23.0
    assert inserted_row[7] == 0
    assert any("UPDATE stops child" in sql for sql, _ in conn.execute_calls)
