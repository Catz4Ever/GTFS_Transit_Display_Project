import math

from transit.models import ShapePoint, StopTimeRow


def hhmmss_to_seconds(value: str) -> int:
    h, m, s = map(int, value.split(":"))
    return h * 3600 + m * 60 + s


class PositioningService:
    def __init__(self) -> None:
        self.stop_anchor_cache: dict[tuple[int, int], dict[int, int]] = {}
        self.shape_distance_cache: dict[int, list[float]] = {}

    def distance(self, a: ShapePoint, b: ShapePoint) -> float:
        lat1 = math.radians(a.lat)
        lon1 = math.radians(a.lon)
        lat2 = math.radians(b.lat)
        lon2 = math.radians(b.lon)
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        sin_dlat = math.sin(dlat / 2.0)
        sin_dlon = math.sin(dlon / 2.0)
        h = sin_dlat * sin_dlat + math.cos(lat1) * math.cos(lat2) * sin_dlon * sin_dlon
        return 6371000.0 * 2.0 * math.asin(min(1.0, math.sqrt(h)))

    def point_distance_m(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        a = ShapePoint(lat=lat1, lon=lon1, seq=0, shape_dist_traveled=None)
        b = ShapePoint(lat=lat2, lon=lon2, seq=0, shape_dist_traveled=None)
        return self.distance(a, b)

    def interpolate_along_shape(self, points: list[ShapePoint], trip_progress: float):
        if not points:
            raise ValueError("No shape points provided")
        if len(points) == 1:
            return points[0].lat, points[0].lon

        trip_progress = max(0.0, min(1.0, trip_progress))
        cumulative = [0.0]
        total = 0.0
        for i in range(1, len(points)):
            total += self.distance(points[i - 1], points[i])
            cumulative.append(total)
        if total == 0:
            return points[0].lat, points[0].lon

        target = total * trip_progress
        for i in range(1, len(points)):
            if cumulative[i] >= target:
                prev_d = cumulative[i - 1]
                seg_len = cumulative[i] - prev_d
                local = 0 if seg_len == 0 else (target - prev_d) / seg_len
                lat = points[i - 1].lat + (points[i].lat - points[i - 1].lat) * local
                lon = points[i - 1].lon + (points[i].lon - points[i - 1].lon) * local
                return lat, lon
        return points[-1].lat, points[-1].lon

    def get_shape_cumulative_distances(self, shape_pk: int, shape_points: list[ShapePoint]) -> list[float]:
        if shape_pk in self.shape_distance_cache:
            return self.shape_distance_cache[shape_pk]
        cumulative = [0.0]
        total = 0.0
        for i in range(1, len(shape_points)):
            prev = shape_points[i - 1]
            curr = shape_points[i]
            if prev.shape_dist_traveled is not None and curr.shape_dist_traveled is not None:
                seg_len = curr.shape_dist_traveled - prev.shape_dist_traveled
                if seg_len < 0:
                    seg_len = self.distance(prev, curr)
            else:
                seg_len = self.distance(prev, curr)
            total += seg_len
            cumulative.append(total)
        self.shape_distance_cache[shape_pk] = cumulative
        return cumulative

    def interpolate_along_shape_distance(self, shape_pk: int, shape_points: list[ShapePoint], target_distance: float):
        if not shape_points:
            raise ValueError("No shape points provided")
        if len(shape_points) == 1:
            return shape_points[0].lat, shape_points[0].lon
        cumulative = self.get_shape_cumulative_distances(shape_pk, shape_points)
        total = cumulative[-1]
        if total <= 0:
            return shape_points[0].lat, shape_points[0].lon
        target = max(0.0, min(total, target_distance))
        for i in range(1, len(shape_points)):
            if cumulative[i] >= target:
                prev_d = cumulative[i - 1]
                seg_len = cumulative[i] - prev_d
                local = 0.0 if seg_len == 0 else (target - prev_d) / seg_len
                lat = shape_points[i - 1].lat + (shape_points[i].lat - shape_points[i - 1].lat) * local
                lon = shape_points[i - 1].lon + (shape_points[i].lon - shape_points[i - 1].lon) * local
                return lat, lon
        return shape_points[-1].lat, shape_points[-1].lon

    def locate_current_segment(self, timeline: list[StopTimeRow], current_time_str: str, time_shift_secs: int = 0) -> dict:
        """Return schedule progress and current/next stop sequences for matching.

        GTFS-RT current_stop_sequence references stop_times.stop_sequence. For in-transit
        vehicles we treat the next stop as the RT-compatible current_stop_sequence, while
        keeping the previous stop separately for debugging/scoring.
        """
        if not timeline:
            return {"status": "no_timeline", "trip_progress": 0.0, "current_stop_sequence": None, "next_stop_sequence": None}

        now = hhmmss_to_seconds(current_time_str)
        first_arrival = hhmmss_to_seconds(timeline[0].arrival_time) + time_shift_secs
        last_departure = hhmmss_to_seconds(timeline[-1].departure_time) + time_shift_secs

        if now < first_arrival:
            return {"status": "not_started", "trip_progress": 0.0, "current_stop_sequence": timeline[0].stop_sequence, "next_stop_sequence": timeline[0].stop_sequence}
        if now > last_departure:
            return {"status": "completed", "trip_progress": 1.0, "current_stop_sequence": timeline[-1].stop_sequence, "next_stop_sequence": None}

        progress = (now - first_arrival) / (last_departure - first_arrival) if last_departure > first_arrival else 1.0
        progress = max(0.0, min(1.0, progress))

        current_seq = timeline[-1].stop_sequence
        next_seq = None
        previous_seq = None
        segment_progress = None
        for i in range(len(timeline) - 1):
            a = timeline[i]
            b = timeline[i + 1]
            dep_a = hhmmss_to_seconds(a.departure_time) + time_shift_secs
            arr_b = hhmmss_to_seconds(b.arrival_time) + time_shift_secs
            if dep_a <= now <= arr_b:
                previous_seq = a.stop_sequence
                current_seq = b.stop_sequence
                next_seq = b.stop_sequence
                segment_progress = 0.0 if arr_b == dep_a else max(0.0, min(1.0, (now - dep_a) / (arr_b - dep_a)))
                break
            arr_a = hhmmss_to_seconds(a.arrival_time) + time_shift_secs
            dep_b = hhmmss_to_seconds(b.departure_time) + time_shift_secs
            if arr_a <= now <= dep_b:
                current_seq = a.stop_sequence
                next_seq = b.stop_sequence

        return {
            "status": "active",
            "trip_progress": progress,
            "current_stop_sequence": current_seq,
            "next_stop_sequence": next_seq,
            "previous_stop_sequence": previous_seq,
            "segment_progress": segment_progress,
        }

    def simulate_trip_state(self, timeline: list[StopTimeRow], current_time_str: str, time_shift_secs: int = 0):
        return self.locate_current_segment(timeline, current_time_str, time_shift_secs)

    def build_stop_anchor_index_map(self, trip_pk: int, shape_pk: int, timeline: list[StopTimeRow], shape_points: list[ShapePoint]) -> dict[int, int]:
        cache_key = (trip_pk, shape_pk)
        if cache_key in self.stop_anchor_cache:
            return self.stop_anchor_cache[cache_key]
        anchor_map: dict[int, int] = {}
        search_start = 0
        for stop_idx, stop in enumerate(timeline):
            stops_remaining = max(0, len(timeline) - stop_idx - 1)
            max_search_end = max(search_start + 1, len(shape_points) - stops_remaining)
            best_idx = search_start
            best_dist = float("inf")
            for idx in range(search_start, max_search_end):
                sp = shape_points[idx]
                d = self.point_distance_m(sp.lat, sp.lon, stop.stop_lat, stop.stop_lon)
                if d < best_dist:
                    best_dist = d
                    best_idx = idx
            anchor_map[stop.stop_sequence] = best_idx
            search_start = best_idx
        self.stop_anchor_cache[cache_key] = anchor_map
        return anchor_map

    def compute_vehicle_position(
        self,
        trip_pk: int,
        shape_pk: int,
        timeline: list[StopTimeRow],
        shape_points: list[ShapePoint],
        current_time_str: str,
        time_shift_secs: int = 0,
    ):
        if len(timeline) < 2 or len(shape_points) < 2:
            return None
        now = hhmmss_to_seconds(current_time_str)
        for i in range(len(timeline) - 1):
            a = timeline[i]
            b = timeline[i + 1]
            dep_a = hhmmss_to_seconds(a.departure_time) + time_shift_secs
            arr_b = hhmmss_to_seconds(b.arrival_time) + time_shift_secs
            if dep_a <= now <= arr_b:
                segment_progress = 0.0 if arr_b == dep_a else (now - dep_a) / (arr_b - dep_a)
                segment_progress = max(0.0, min(1.0, segment_progress))
                if a.shape_dist_traveled is not None and b.shape_dist_traveled is not None:
                    target_distance = a.shape_dist_traveled + segment_progress * (b.shape_dist_traveled - a.shape_dist_traveled)
                    return self.interpolate_along_shape_distance(shape_pk, shape_points, target_distance)
                anchor_map = self.build_stop_anchor_index_map(trip_pk, shape_pk, timeline, shape_points)
                idx_a = anchor_map.get(a.stop_sequence)
                idx_b = anchor_map.get(b.stop_sequence)
                if idx_a is None or idx_b is None:
                    return None
                if idx_a == idx_b:
                    sp = shape_points[idx_a]
                    return sp.lat, sp.lon
                if idx_a > idx_b:
                    idx_a, idx_b = idx_b, idx_a
                subshape = shape_points[idx_a:idx_b + 1]
                return self.interpolate_along_shape(subshape, segment_progress)
        return None
