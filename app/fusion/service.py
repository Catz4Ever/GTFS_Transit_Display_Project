#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

from transit.config import load_settings
from transit.db import connect_db
from transit.repositories.gtfs_repository import GtfsRepository
from transit.services.map_matching import match_point_to_shape, shape_bearing_at_match
from transit.services.matching_score import MatchScore, score_rt_against_sim
from transit.services.publisher import MqttPublisher
from transit.services.smoothing import smooth_position


# Pre-filter candidates further than this from the RT vehicle before running
# full geometry scoring. Eliminates the expensive match_point_to_shape call
# for obviously wrong candidates (different part of city).
_SPATIAL_PREFILTER_KM = 2.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class FusionService:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.publisher = MqttPublisher(self.settings)
        self.repo = GtfsRepository()
        self.running = True
        self.interval_seconds = int(os.getenv("FUSION_INTERVAL_SECONDS", str(self.settings.interval_seconds)))
        self.vehicle_positions_max_age_seconds = int(os.getenv("FUSION_RT_MAX_AGE_SECONDS", os.getenv("VEHICLE_POSITIONS_MAX_AGE_SECONDS", "120")))

        self.max_shape_distance_m = float(os.getenv("FUSION_MAX_SHAPE_DISTANCE_M", "120"))
        self.min_match_score = float(os.getenv("FUSION_MIN_MATCH_SCORE", "55"))
        self.snap_to_shape_max_distance_m = float(os.getenv("FUSION_SNAP_TO_SHAPE_MAX_DISTANCE_M", "80"))
        self.enable_snap_to_shape = os.getenv("FUSION_SNAP_TO_SHAPE", "true").lower() in {"1", "true", "yes"}
        self.enable_smoothing = os.getenv("FUSION_SMOOTHING", "true").lower() in {"1", "true", "yes"}
        self.smoothing_alpha = float(os.getenv("FUSION_SMOOTHING_ALPHA", "0.70"))
        self.smoothing_max_jump_m = float(os.getenv("FUSION_SMOOTHING_MAX_JUMP_M", "500"))
        self.city_bounds_ttl_seconds = int(os.getenv("FUSION_CITY_BOUNDS_TTL_SECONDS", "300"))
        self._city_bounds_cache: dict[int, tuple[float, tuple[float, float, float, float] | None]] = {}

    def stop(self, *_args):
        logging.info("Stopping fusion worker for city=%s", self.settings.city_code)
        self.running = False
        self.publisher.stop()

    def get_city_row(self, conn):
        row = conn.execute(
            """
            SELECT city_id, city_name
            FROM cities
            WHERE city_code = %s
            """,
            (self.settings.city_code,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"City not found in database: {self.settings.city_code}")
        return int(row[0]), str(row[1])

    @staticmethod
    def normalize_db_timestamp(value):
        if value is None:
            return None
        if getattr(value, "tzinfo", None) is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def get_latest_rt_snapshot(self, conn, city_id: int):
        return conn.execute(
            """
            SELECT snapshot_pk, fetched_at, entity_count
            FROM gtfs_rt_feed_snapshots
            WHERE city_id = %s AND feed_type = 'vehicle_positions'
            ORDER BY snapshot_pk DESC
            LIMIT 1
            """,
            (city_id,),
        ).fetchone()

    def get_rt_vehicles(self, conn, city_id: int, snapshot_pk: int) -> list[dict]:
        rows = conn.execute(
            """
            SELECT
                vp.rt_vehicle_pk,
                vp.entity_id,
                vp.vehicle_id,
                vp.vehicle_label,
                vp.trip_id AS raw_rt_trip_id,
                COALESCE(t.trip_id, vp.trip_id) AS canonical_trip_id,
                vp.route_id AS raw_route_id,
                COALESCE(r.route_id, vp.route_id) AS canonical_route_id,
                vp.start_date,
                vp.start_time,
                vp.trip_pk,
                vp.route_pk,
                vp.latitude,
                vp.longitude,
                vp.position_timestamp,
                vp.bearing,
                vp.speed,
                vp.current_stop_sequence,
                t.shape_pk,
                s.shape_id
            FROM gtfs_rt_vehicle_positions vp
            LEFT JOIN trips t ON t.trip_pk = vp.trip_pk
            LEFT JOIN shapes s ON s.shape_pk = t.shape_pk
            LEFT JOIN routes r ON r.route_pk = vp.route_pk
            WHERE vp.city_id = %s AND vp.snapshot_pk = %s
            ORDER BY vp.rt_vehicle_pk
            """,
            (city_id, snapshot_pk),
        ).fetchall()

        # Build a set of vehicle_ids that appear more than once in this snapshot
        # so we can use entity_id as tiebreaker for duplicates.
        from collections import Counter
        vehicle_id_counts: Counter = Counter(
            str(r[2]) for r in rows if r[2] is not None
        )

        vehicles = []
        for r in rows:
            rt_vehicle_pk = int(r[0])
            entity_id = str(r[1]) if r[1] is not None else None
            vehicle_id = str(r[2]) if r[2] is not None else None
            vehicle_label = str(r[3]) if r[3] is not None else None
            raw_rt_trip_id = str(r[4]) if r[4] is not None else None
            trip_id = str(r[5]) if r[5] is not None else None
            raw_route_id = str(r[6]) if r[6] is not None else None
            route_id = str(r[7]) if r[7] is not None else None
            start_date = str(r[8]) if r[8] is not None else None
            start_time = str(r[9]) if r[9] is not None else None
            trip_pk = int(r[10]) if r[10] is not None else None
            route_pk = int(r[11]) if r[11] is not None else None
            lat = float(r[12])
            lon = float(r[13])
            position_timestamp = int(r[14]) if r[14] is not None else None
            bearing = float(r[15]) if r[15] is not None else None
            speed = float(r[16]) if r[16] is not None else None
            current_stop_sequence = int(r[17]) if r[17] is not None else None
            shape_pk = int(r[18]) if r[18] is not None else None
            shape_id = str(r[19]) if r[19] is not None else None

            vehicle_id_is_unique = vehicle_id is None or vehicle_id_counts[vehicle_id] == 1

            if vehicle_id and vehicle_id_is_unique:
                trip_instance_id = f"rtveh:{vehicle_id}"
            elif vehicle_id and entity_id:
                # Duplicate vehicle_id in snapshot — use entity_id to keep them separate
                trip_instance_id = f"rtentity:{entity_id}"
            elif vehicle_id:
                trip_instance_id = f"rtveh:{vehicle_id}"
            elif trip_id and route_id and start_time:
                trip_instance_id = f"rt:{trip_id}:{route_id}:{start_time}"
            elif trip_id and route_id:
                trip_instance_id = f"rt:{trip_id}:{route_id}"
            elif entity_id:
                trip_instance_id = f"rtentity:{entity_id}"
            else:
                trip_instance_id = f"rtpk:{rt_vehicle_pk}"

            vehicles.append({
                "trip_instance_id": trip_instance_id,
                "trip_pk": trip_pk,
                "trip_id": trip_id,
                "raw_rt_trip_id": raw_rt_trip_id,
                "route_pk": route_pk,
                "route_id": route_id,
                "raw_route_id": raw_route_id,
                "service_pk": None,
                "service_id": None,
                "shape_pk": shape_pk,
                "shape_id": shape_id,
                "status": "active",
                "source": "gtfs_rt_vehicle_position",
                "lat": lat,
                "lon": lon,
                "trip_progress": None,
                "position_timestamp": position_timestamp,
                "vehicle_id": vehicle_id,
                "vehicle_label": vehicle_label,
                "start_date": start_date,
                "start_time": start_time,
                "bearing": bearing,
                "speed": speed,
                "current_stop_sequence": current_stop_sequence,
            })
        return vehicles

    def get_simulated_vehicles(self, conn, city_id: int) -> list[dict]:
        rows = conn.execute(
            """
            SELECT trip_instance_id, trip_pk, trip_id, route_pk, route_id, service_pk, service_id,
                   shape_pk, shape_id, status, source, lat, lon, trip_progress, updated_at,
                   current_stop_sequence, next_stop_sequence, previous_stop_sequence,
                   scheduled_delay_seconds, adjusted_progress
            FROM vehicle_states_simulated_current
            WHERE city_id = %s
            ORDER BY route_id, trip_instance_id
            """,
            (city_id,),
        ).fetchall()
        return [{
            "trip_instance_id": str(r[0]),
            "trip_pk": int(r[1]) if r[1] is not None else None,
            "trip_id": str(r[2]) if r[2] is not None else None,
            "route_pk": int(r[3]) if r[3] is not None else None,
            "route_id": str(r[4]) if r[4] is not None else None,
            "service_pk": int(r[5]) if r[5] is not None else None,
            "service_id": str(r[6]) if r[6] is not None else None,
            "shape_pk": int(r[7]) if r[7] is not None else None,
            "shape_id": str(r[8]) if r[8] is not None else None,
            "status": str(r[9]),
            "source": str(r[10]),
            "lat": float(r[11]),
            "lon": float(r[12]),
            "trip_progress": float(r[13]) if r[13] is not None else None,
            "updated_at": r[14],
            "current_stop_sequence": int(r[15]) if r[15] is not None else None,
            "next_stop_sequence": int(r[16]) if r[16] is not None else None,
            "previous_stop_sequence": int(r[17]) if r[17] is not None else None,
            "scheduled_delay_seconds": int(r[18]) if r[18] is not None else None,
            "adjusted_progress": float(r[19]) if r[19] is not None else None,
        } for r in rows]

    def get_previous_positions_batch(self, conn, city_id: int, vehicle_keys: list[str], now: datetime) -> dict[str, dict]:
        if not vehicle_keys:
            return {}
        rows = conn.execute(
            """
            SELECT DISTINCT ON (vehicle_key)
                vehicle_key, observed_at, lat, lon
            FROM vehicle_position_history
            WHERE city_id = %s AND vehicle_key = ANY(%s)
            ORDER BY vehicle_key, observed_at DESC
            """,
            (city_id, vehicle_keys),
        ).fetchall()
        result = {}
        for row in rows:
            observed_at = self.normalize_db_timestamp(row[1])
            result[str(row[0])] = {
                "lat": float(row[2]),
                "lon": float(row[3]),
                "dt_seconds": (now - observed_at).total_seconds() if observed_at else None,
            }
        return result

    def vehicle_key(self, vehicle: dict) -> str:
        if vehicle.get("vehicle_id"):
            return f"veh:{vehicle['vehicle_id']}"
        if vehicle.get("trip_instance_id"):
            return f"trip:{vehicle['trip_instance_id']}"
        return f"anon:{vehicle.get('route_id','unknown')}:{vehicle.get('lat')}:{vehicle.get('lon')}"

    def get_city_bounds(self, conn, city_id: int, buffer_km: float = 5.0) -> tuple[float, float, float, float] | None:
        """Returns (min_lat, max_lat, min_lon, max_lon) with buffer, or None if no stops."""
        row = conn.execute(
            """
            SELECT MIN(stop_lat), MAX(stop_lat), MIN(stop_lon), MAX(stop_lon)
            FROM stops WHERE city_id = %s
            """,
            (city_id,),
        ).fetchone()
        if not row or row[0] is None:
            return None
        import math
        lat_buf = buffer_km / 111.0
        lon_buf = buffer_km / (111.0 * math.cos(math.radians((float(row[0]) + float(row[1])) / 2)))
        return (
            float(row[0]) - lat_buf,
            float(row[1]) + lat_buf,
            float(row[2]) - lon_buf,
            float(row[3]) + lon_buf,
        )

    def get_cached_city_bounds(self, conn, city_id: int) -> tuple[float, float, float, float] | None:
        now = time.monotonic()
        cached = self._city_bounds_cache.get(city_id)

        if cached is not None:
            cached_at, cached_bounds = cached
            if now - cached_at < self.city_bounds_ttl_seconds:
                return cached_bounds

        bounds = self.get_city_bounds(conn, city_id)

        # During a GTFS import, stops may be temporarily incomplete. Keep the
        # previous bounds instead of replacing them with None.
        if bounds is None and cached is not None:
            return cached[1]

        self._city_bounds_cache[city_id] = (now, bounds)
        return bounds

    def get_previous_assignments(self, conn, city_id: int) -> dict[str, str]:
        """Returns {vehicle_key: trip_instance_id} from the current vehicle states table (previous cycle)."""
        rows = conn.execute(
            """
            SELECT vehicle_id, trip_instance_id
            FROM vehicle_states_current
            WHERE city_id = %s AND trip_instance_id IS NOT NULL
            """,
            (city_id,),
        ).fetchall()
        result = {}
        for row in rows:
            vehicle_id = str(row[0]) if row[0] is not None else None
            if vehicle_id:
                result[f"veh:{vehicle_id}"] = str(row[1])
        return result

    def build_candidate(self, conn, rt: dict, sim: dict) -> tuple[dict, MatchScore, object | None, float | None]:
        shape_points = []
        if sim.get("shape_pk") is not None:
            shape_points = self.repo.get_shape_points(conn, int(sim["shape_pk"]))
        map_match = match_point_to_shape(rt["lat"], rt["lon"], shape_points) if shape_points else None
        shape_bearing = shape_bearing_at_match(shape_points, map_match) if shape_points else None
        score = score_rt_against_sim(rt, sim, map_match, shape_bearing, self.max_shape_distance_m)
        return sim, score, map_match, shape_bearing

    def choose_best_candidate(self, conn, rt: dict, candidates: list[tuple[int, dict]], used_sim_indices: set[int], prev_assignments: dict[str, str] | None = None):
        best = None
        rt_lat = rt.get("lat")
        rt_lon = rt.get("lon")
        for idx, sim in candidates:
            if idx in used_sim_indices:
                continue
            # Spatial pre-filter: skip candidates far from the RT vehicle
            # before running the expensive map_matching geometry.
            sim_lat = sim.get("lat")
            sim_lon = sim.get("lon")
            if (
                rt_lat is not None and rt_lon is not None
                and sim_lat is not None and sim_lon is not None
                and _haversine_km(rt_lat, rt_lon, sim_lat, sim_lon) > _SPATIAL_PREFILTER_KM
            ):
                # Skip prefilter for direct trip_pk matches — the trip is correct
                # even if the simulated position is temporarily far from RT.
                if rt.get("trip_pk") is not None and rt.get("trip_pk") == sim.get("trip_pk"):
                    pass
                else:
                    continue
            sim, score, map_match, shape_bearing = self.build_candidate(conn, rt, sim)
            # Continuity bonus: if this RT vehicle was matched to this sim trip last cycle, boost score.
            # Skip if the vehicle is far from the shape — don't lock in a bad match.
            if prev_assignments and "far_from_shape" not in score.reasons:
                vk = self.vehicle_key(rt)
                if prev_assignments.get(vk) == sim.get("trip_instance_id"):
                    score = MatchScore(
                        score=score.score + 20,
                        strategy=score.strategy,
                        reasons=score.reasons + ["continuity"],
                        distance_from_shape_m=score.distance_from_shape_m,
                        bearing_error_deg=score.bearing_error_deg,
                    )
            if best is None or score.score > best[1].score:
                best = (idx, score, sim, map_match, shape_bearing)
        if best is None or best[1].score < self.min_match_score:
            return None
        return best

    def build_fused_vehicle(
        self,
        rt: dict,
        sim: dict | None,
        score: MatchScore | None,
        map_match,
        now: datetime,
        previous_position: dict | None,
        shape_bearing: float | None = None,
    ) -> dict:
        if sim is None:
            fused = dict(rt)
            fused["source"] = "gtfs_rt_vehicle_position"
            fused["match_strategy"] = "rt_only"
            fused["match_confidence"] = 0.0
            fused["fusion_score"] = score.score if score else 0.0
            fused["is_position_snapped"] = False
            fused["matched_lat"] = None
            fused["matched_lon"] = None
            fused["distance_from_shape_m"] = None
            fused["shape_dist_traveled"] = None
            fused["bearing_error_deg"] = None
        else:
            fused = dict(sim)
            fused.update({
                "trip_instance_id": sim["trip_instance_id"],
                "trip_pk": sim.get("trip_pk") or rt.get("trip_pk"),
                "trip_id": sim.get("trip_id") or rt.get("trip_id"),
                "route_pk": sim.get("route_pk") or rt.get("route_pk"),
                "route_id": sim.get("route_id") or rt.get("route_id"),
                "service_pk": sim.get("service_pk"),
                "service_id": sim.get("service_id"),
                "shape_pk": sim.get("shape_pk") or rt.get("shape_pk"),
                "shape_id": sim.get("shape_id") or rt.get("shape_id"),
                "status": rt.get("status") or sim.get("status"),
                "source": "fused",
                "lat": rt["lat"],
                "lon": rt["lon"],
                "trip_progress": sim.get("trip_progress"),
                "position_timestamp": rt.get("position_timestamp"),
                "vehicle_id": rt.get("vehicle_id"),
                "vehicle_label": rt.get("vehicle_label"),
                "raw_rt_trip_id": rt.get("raw_rt_trip_id") or rt.get("trip_id"),
                "start_date": rt.get("start_date"),
                "start_time": rt.get("start_time"),
                "bearing": shape_bearing if shape_bearing is not None else rt.get("bearing"),
                "speed": rt.get("speed"),
                "current_stop_sequence": sim.get("current_stop_sequence") or rt.get("current_stop_sequence"),
                "next_stop_sequence": sim.get("next_stop_sequence"),
                "previous_stop_sequence": sim.get("previous_stop_sequence"),
                "scheduled_delay_seconds": sim.get("scheduled_delay_seconds"),
                "adjusted_progress": sim.get("adjusted_progress"),
                "match_strategy": score.strategy if score else "scored_unknown",
                "match_confidence": min(1.0, max(0.0, (score.score if score else 0.0) / 100.0)),
                "fusion_score": score.score if score else 0.0,
                "match_reasons": ",".join(score.reasons) if score else None,
                "matched_lat": map_match.matched_lat if map_match else None,
                "matched_lon": map_match.matched_lon if map_match else None,
                "distance_from_shape_m": map_match.distance_from_shape_m if map_match else None,
                "shape_dist_traveled": map_match.shape_dist_traveled if map_match else None,
                "bearing_error_deg": score.bearing_error_deg if score else None,
            })

            should_snap = (
                self.enable_snap_to_shape
                and map_match is not None
                and map_match.distance_from_shape_m <= self.snap_to_shape_max_distance_m
            )
            fused["is_position_snapped"] = bool(should_snap)
            if should_snap:
                fused["lat"] = map_match.matched_lat
                fused["lon"] = map_match.matched_lon

        if self.enable_smoothing:
            smoothed = smooth_position(
                previous_position,
                float(fused["lat"]),
                float(fused["lon"]),
                alpha=self.smoothing_alpha,
                max_jump_m=self.smoothing_max_jump_m,
            )
            fused["raw_lat"] = rt.get("lat", fused["lat"])
            fused["raw_lon"] = rt.get("lon", fused["lon"])
            fused["lat"] = smoothed.lat
            fused["lon"] = smoothed.lon
            fused["is_position_smoothed"] = smoothed.used_previous
            fused["jump_distance_m"] = smoothed.jump_distance_m
            fused["smoothing_reset_reason"] = smoothed.reset_reason
        else:
            fused["raw_lat"] = rt.get("lat", fused["lat"])
            fused["raw_lon"] = rt.get("lon", fused["lon"])
            fused["is_position_smoothed"] = False
            fused["jump_distance_m"] = None
            fused["smoothing_reset_reason"] = None
        return fused

    def fuse(self, conn, city_id: int, rt_vehicles: list[dict], simulated_vehicles: list[dict], now: datetime, previous_positions: dict[str, dict] | None = None, prev_assignments: dict[str, str] | None = None) -> tuple[list[dict], dict]:
        sim_by_trip_pk: dict[int, list[tuple[int, dict]]] = {}
        sim_by_trip_id: dict[str, list[tuple[int, dict]]] = {}
        sim_by_route_id: dict[str, list[tuple[int, dict]]] = {}
        for idx, sim in enumerate(simulated_vehicles):
            if sim.get("trip_pk") is not None:
                sim_by_trip_pk.setdefault(sim["trip_pk"], []).append((idx, sim))
            if sim.get("trip_id"):
                sim_by_trip_id.setdefault(sim["trip_id"], []).append((idx, sim))
            if sim.get("route_id"):
                sim_by_route_id.setdefault(sim["route_id"], []).append((idx, sim))

        fused: list[dict] = []
        used_sim_indices: set[int] = set()
        stats = {"matched_total": 0, "unmatched_rt": 0, "unmatched_sim": 0, "low_score_rt": 0}
        if previous_positions is None:
            previous_positions = {}
        if prev_assignments is None:
            prev_assignments = {}

        # Process RT vehicles with a direct trip_pk sim match first so they
        # can't be crowded out by vehicles matching only by suffix/route.
        sim_trip_pks = {sim.get("trip_pk") for sim in simulated_vehicles if sim.get("trip_pk") is not None}
        def _rt_priority(rt: dict) -> int:
            if rt.get("trip_pk") in sim_trip_pks:
                return 0  # direct trip_pk match available — highest priority
            if rt.get("trip_id"):
                return 1  # may match by trip_id or suffix
            return 2      # route-only or no match

        # Build set of trip_pks that are exclusively claimed by a specific RT vehicle.
        # A sim trip whose trip_pk matches exactly one RT vehicle is reserved for that vehicle only.
        rt_trip_pk_counts: dict[int, int] = {}
        for rt in rt_vehicles:
            pk = rt.get("trip_pk")
            if pk is not None:
                rt_trip_pk_counts[pk] = rt_trip_pk_counts.get(pk, 0) + 1
        # trip_pks claimed by exactly one RT vehicle — reserved for that vehicle
        exclusive_rt_trip_pks: dict[int, dict] = {
            rt["trip_pk"]: rt
            for rt in rt_vehicles
            if rt.get("trip_pk") is not None and rt_trip_pk_counts[rt["trip_pk"]] == 1
        }

        for rt in sorted(rt_vehicles, key=_rt_priority):
            pools: list[tuple[int, dict]] = []
            if rt.get("trip_pk") in sim_by_trip_pk:
                pools.extend(sim_by_trip_pk[rt["trip_pk"]])
            if rt.get("trip_id") in sim_by_trip_id:
                pools.extend(sim_by_trip_id[rt["trip_id"]])
            if rt.get("route_id") in sim_by_route_id:
                pools.extend(sim_by_route_id[rt["route_id"]])
            # de-duplicate by simulated index
            seen = set()
            candidates = []
            for idx, sim in pools:
                if idx not in seen:
                    # Skip sim trips that are exclusively reserved for a different RT vehicle
                    sim_trip_pk = sim.get("trip_pk")
                    if (sim_trip_pk is not None
                            and sim_trip_pk in exclusive_rt_trip_pks
                            and exclusive_rt_trip_pks[sim_trip_pk] is not rt):
                        seen.add(idx)
                        continue
                    candidates.append((idx, sim))
                    seen.add(idx)

            chosen = self.choose_best_candidate(conn, rt, candidates, used_sim_indices, prev_assignments)
            prev = previous_positions.get(self.vehicle_key(rt))
            if chosen is None:
                fused.append(self.build_fused_vehicle(rt, None, None, None, now, prev))
                stats["unmatched_rt"] += 1
                continue

            idx, score, sim, map_match, _shape_bearing = chosen
            used_sim_indices.add(idx)
            fused_vehicle = self.build_fused_vehicle(rt, sim, score, map_match, now, prev, shape_bearing=_shape_bearing)
            fused.append(fused_vehicle)
            stats["matched_total"] += 1

        shape_points_cache: dict[int, list] = {}

        for idx, sim in enumerate(simulated_vehicles):
            if idx in used_sim_indices:
                continue
            # Compute shape bearing for simulated vehicles
            sim_shape_bearing = None
            shape_pk = sim.get("shape_pk")
            if shape_pk is not None:
                try:
                    if shape_pk not in shape_points_cache:
                        shape_points_cache[shape_pk] = self.repo.get_shape_points(conn, int(shape_pk))
                    shape_points = shape_points_cache[shape_pk]
                    if shape_points:
                        vp = match_point_to_shape(sim["lat"], sim["lon"], shape_points)
                        if vp is not None:
                            sim_shape_bearing = shape_bearing_at_match(shape_points, vp)
                except Exception:
                    pass

            fallback = dict(sim)
            fallback.update({
                "match_strategy": "sim_only",
                "match_confidence": 1.0,
                "fusion_score": 0.0,
                "is_position_snapped": False,
                "is_position_smoothed": False,
                "raw_lat": sim.get("lat"),
                "raw_lon": sim.get("lon"),
                "matched_lat": None,
                "matched_lon": None,
                "distance_from_shape_m": None,
                "shape_dist_traveled": None,
                "bearing_error_deg": None,
                "jump_distance_m": None,
                "smoothing_reset_reason": None,
                "match_reasons": None,
                "bearing": sim_shape_bearing,
                "current_stop_sequence": sim.get("current_stop_sequence"),
                "next_stop_sequence": sim.get("next_stop_sequence"),
                "previous_stop_sequence": sim.get("previous_stop_sequence"),
                "scheduled_delay_seconds": sim.get("scheduled_delay_seconds"),
                "adjusted_progress": sim.get("adjusted_progress"),
            })
            fused.append(fallback)
            stats["unmatched_sim"] += 1
        return fused, stats

    def replace_current_vehicle_states(self, conn, city_id: int, vehicles: list[dict], updated_at: datetime):
        conn.execute("DELETE FROM vehicle_states_current WHERE city_id = %s", (city_id,))
        for vehicle in vehicles:
            conn.execute(
                """
                INSERT INTO vehicle_states_current (
                    city_id, trip_instance_id, trip_pk, route_pk, service_pk, shape_pk,
                    trip_id, route_id, service_id, shape_id, status, source,
                    vehicle_id, vehicle_label, raw_rt_trip_id, start_date, start_time, position_timestamp,
                    match_strategy, match_confidence, lat, lon, trip_progress,
                    current_stop_sequence, next_stop_sequence, previous_stop_sequence,
                    scheduled_delay_seconds, adjusted_progress, updated_at,
                    raw_lat, raw_lon, matched_lat, matched_lon, distance_from_shape_m,
                    shape_dist_traveled, bearing_error_deg, fusion_score, match_reasons,
                    is_position_snapped, is_position_smoothed, jump_distance_m, smoothing_reset_reason,
                    bearing
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (city_id, trip_instance_id) DO UPDATE SET
                    trip_pk=EXCLUDED.trip_pk, route_pk=EXCLUDED.route_pk, service_pk=EXCLUDED.service_pk,
                    shape_pk=EXCLUDED.shape_pk, trip_id=EXCLUDED.trip_id, route_id=EXCLUDED.route_id,
                    service_id=EXCLUDED.service_id, shape_id=EXCLUDED.shape_id, status=EXCLUDED.status,
                    source=EXCLUDED.source, vehicle_id=EXCLUDED.vehicle_id, vehicle_label=EXCLUDED.vehicle_label,
                    raw_rt_trip_id=EXCLUDED.raw_rt_trip_id, start_date=EXCLUDED.start_date,
                    start_time=EXCLUDED.start_time, position_timestamp=EXCLUDED.position_timestamp,
                    match_strategy=EXCLUDED.match_strategy, match_confidence=EXCLUDED.match_confidence,
                    lat=EXCLUDED.lat, lon=EXCLUDED.lon, trip_progress=EXCLUDED.trip_progress,
                    current_stop_sequence=EXCLUDED.current_stop_sequence,
                    next_stop_sequence=EXCLUDED.next_stop_sequence,
                    previous_stop_sequence=EXCLUDED.previous_stop_sequence,
                    scheduled_delay_seconds=EXCLUDED.scheduled_delay_seconds,
                    adjusted_progress=EXCLUDED.adjusted_progress,
                    updated_at=EXCLUDED.updated_at, raw_lat=EXCLUDED.raw_lat, raw_lon=EXCLUDED.raw_lon,
                    matched_lat=EXCLUDED.matched_lat, matched_lon=EXCLUDED.matched_lon,
                    distance_from_shape_m=EXCLUDED.distance_from_shape_m,
                    shape_dist_traveled=EXCLUDED.shape_dist_traveled,
                    bearing_error_deg=EXCLUDED.bearing_error_deg, fusion_score=EXCLUDED.fusion_score,
                    match_reasons=EXCLUDED.match_reasons,
                    is_position_snapped=EXCLUDED.is_position_snapped,
                    is_position_smoothed=EXCLUDED.is_position_smoothed,
                    jump_distance_m=EXCLUDED.jump_distance_m,
                    smoothing_reset_reason=EXCLUDED.smoothing_reset_reason,
                    bearing=EXCLUDED.bearing
                """,
                (
                    city_id, vehicle["trip_instance_id"], vehicle.get("trip_pk"), vehicle.get("route_pk"),
                    vehicle.get("service_pk"), vehicle.get("shape_pk"), vehicle.get("trip_id"),
                    vehicle.get("route_id"), vehicle.get("service_id"), vehicle.get("shape_id"),
                    vehicle["status"], vehicle["source"], vehicle.get("vehicle_id"),
                    vehicle.get("vehicle_label"), vehicle.get("raw_rt_trip_id"), vehicle.get("start_date"),
                    vehicle.get("start_time"), vehicle.get("position_timestamp"), vehicle.get("match_strategy"),
                    vehicle.get("match_confidence"), vehicle["lat"], vehicle["lon"], vehicle.get("trip_progress"),
                    vehicle.get("current_stop_sequence"), vehicle.get("next_stop_sequence"),
                    vehicle.get("previous_stop_sequence"), vehicle.get("scheduled_delay_seconds"),
                    vehicle.get("adjusted_progress"), updated_at, vehicle.get("raw_lat"), vehicle.get("raw_lon"), vehicle.get("matched_lat"),
                    vehicle.get("matched_lon"), vehicle.get("distance_from_shape_m"), vehicle.get("shape_dist_traveled"),
                    vehicle.get("bearing_error_deg"), vehicle.get("fusion_score"), vehicle.get("match_reasons"),
                    vehicle.get("is_position_snapped"), vehicle.get("is_position_smoothed"),
                    vehicle.get("jump_distance_m"), vehicle.get("smoothing_reset_reason"),
                    vehicle.get("bearing"),
                ),
            )

    def append_position_history(self, conn, city_id: int, vehicles: list[dict], observed_at: datetime):
        for vehicle in vehicles:
            key = self.vehicle_key(vehicle)
            conn.execute(
                """
                INSERT INTO vehicle_position_history (
                    city_id, vehicle_key, observed_at, lat, lon, raw_lat, raw_lon,
                    matched_lat, matched_lon, source, match_strategy, fusion_score
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
                """,
                (
                    city_id, key, observed_at, vehicle["lat"], vehicle["lon"], vehicle.get("raw_lat"),
                    vehicle.get("raw_lon"), vehicle.get("matched_lat"), vehicle.get("matched_lon"),
                    vehicle.get("source"), vehicle.get("match_strategy"), vehicle.get("fusion_score"),
                ),
            )

    def run(self):
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        self.publisher.start()
        logging.info("Starting fusion worker for city=%s", self.settings.city_code)
        with connect_db(self.settings.db_config) as conn:
            city_id, city_name = self.get_city_row(conn)
            while self.running:
                started = time.time()
                now = datetime.now(timezone.utc)

                # Pause during GTFS import to avoid lock contention with DELETE FROM trips
                import_in_progress = conn.execute(
                    """
                    SELECT COALESCE(crc.gtfs_import_in_progress, FALSE)
                    FROM cities c
                    JOIN city_runtime_config crc ON crc.city_id = c.city_id
                    WHERE c.city_code = %s
                    """,
                    (self.settings.city_code,),
                ).fetchone()
                if import_in_progress and import_in_progress[0]:
                    logging.info("Fusion paused: GTFS import in progress city=%s", self.settings.city_code)
                    time.sleep(self.interval_seconds)
                    continue

                latest_snapshot = self.get_latest_rt_snapshot(conn, city_id)
                rt_vehicles = []
                if latest_snapshot is not None:
                    snapshot_pk = int(latest_snapshot[0])
                    fetched_at = self.normalize_db_timestamp(latest_snapshot[1])
                    snapshot_age_seconds = abs((now - fetched_at).total_seconds()) if fetched_at is not None else None
                    if snapshot_age_seconds is None or snapshot_age_seconds <= self.vehicle_positions_max_age_seconds:
                        rt_vehicles = self.get_rt_vehicles(conn, city_id, snapshot_pk)
                    else:
                        logging.warning("Ignoring stale vehicle positions city=%s snapshot_pk=%s age_seconds=%.1f", self.settings.city_code, snapshot_pk, snapshot_age_seconds)

                simulated_vehicles = self.get_simulated_vehicles(conn, city_id)
                rt_keys = [self.vehicle_key(rt) for rt in rt_vehicles]
                previous_positions = self.get_previous_positions_batch(conn, city_id, rt_keys, now)
                prev_assignments = self.get_previous_assignments(conn, city_id)
                # Filter out RT vehicles with implausible positions (outside city bounds)
                bounds = self.get_cached_city_bounds(conn, city_id)
                if bounds:
                    min_lat, max_lat, min_lon, max_lon = bounds
                    valid_rt = [
                        rt for rt in rt_vehicles
                        if min_lat <= rt["lat"] <= max_lat and min_lon <= rt["lon"] <= max_lon
                    ]
                    skipped = len(rt_vehicles) - len(valid_rt)
                    if skipped:
                        logging.warning("Skipped %s RT vehicles outside city bounds city=%s", skipped, self.settings.city_code)
                    rt_vehicles = valid_rt
                fused_vehicles, stats = self.fuse(conn, city_id, rt_vehicles, simulated_vehicles, now, previous_positions, prev_assignments)
                self.replace_current_vehicle_states(conn, city_id, fused_vehicles, now)
                self.append_position_history(conn, city_id, fused_vehicles, now)
                conn.commit()
                self.publisher.publish_vehicle_states(fused_vehicles, now)
                logging.info(
                    "Fusion cycle city=%s city_name=%s rt=%s simulated=%s matched=%s unmatched_rt=%s unmatched_sim=%s fused=%s duration_s=%.3f",
                    self.settings.city_code, city_name, len(rt_vehicles), len(simulated_vehicles),
                    stats["matched_total"], stats["unmatched_rt"], stats["unmatched_sim"],
                    len(fused_vehicles), time.time() - started,
                )
                time.sleep(self.interval_seconds)


def main():
    settings = load_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(message)s")
    try:
        FusionService().run()
    except Exception as e:
        logging.exception("Fusion worker failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
