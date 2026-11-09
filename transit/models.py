from dataclasses import dataclass
from typing import Optional


@dataclass
class StopTimeRow:
    stop_sequence: int
    stop_id: str
    stop_name: str
    stop_lat: float
    stop_lon: float
    arrival_time: str
    departure_time: str
    shape_dist_traveled: Optional[float]


@dataclass
class ShapePoint:
    lat: float
    lon: float
    seq: int
    shape_dist_traveled: Optional[float]


@dataclass
class FrequencyRow:
    start_time: str
    end_time: str
    headway_secs: int


@dataclass
class TripRow:
    trip_pk: int
    trip_id: str
    route_pk: int
    route_id: str
    service_pk: int
    service_id: str
    shape_pk: int | None
    shape_id: str | None
