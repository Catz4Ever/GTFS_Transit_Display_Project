from __future__ import annotations

from dataclasses import dataclass, field

from transit.services.map_matching import MapMatchResult, bearing_delta_deg
import math

@dataclass
class MatchScore:
    score: float
    strategy: str
    reasons: list[str] = field(default_factory=list)
    distance_from_shape_m: float | None = None
    bearing_error_deg: float | None = None


def score_rt_against_sim(
    rt: dict,
    sim: dict,
    map_match: MapMatchResult | None,
    shape_bearing: float | None = None,
    max_shape_distance_m: float = 120.0,
) -> MatchScore:
    score = 0.0
    reasons: list[str] = []

    if rt.get("trip_pk") is not None and rt.get("trip_pk") == sim.get("trip_pk"):
        score += 55
        reasons.append("trip_pk")
    elif rt.get("trip_id") and rt.get("trip_id") == sim.get("trip_id"):
        score += 45
        reasons.append("trip_id")
    elif rt.get("trip_id") and sim.get("trip_id"):
        # Partial match: last numeric segment often identifies the service run
        # e.g. A100-A674-2-12-28074017761 vs A100-A2710-5-13-28074017761
        rt_parts = str(rt["trip_id"]).split("-")
        sim_parts = str(sim["trip_id"]).split("-")
        if rt_parts[-1] and rt_parts[-1] == sim_parts[-1] and len(rt_parts[-1]) >= 8:
            score += 15
            reasons.append("trip_id_suffix")

    if rt.get("route_pk") is not None and rt.get("route_pk") == sim.get("route_pk"):
        score += 25
        reasons.append("route_pk")
    elif rt.get("route_id") and rt.get("route_id") == sim.get("route_id"):
        score += 20
        reasons.append("route_id")

    distance = map_match.distance_from_shape_m if map_match else None
    if distance is not None:
        if distance <= 30:
            score += 25
            reasons.append("shape<=30m")
        elif distance <= 60:
            score += 18
            reasons.append("shape<=60m")
        elif distance <= max_shape_distance_m:
            score += 5
            reasons.append("shape<=threshold")
        else:
            score -= 30
            reasons.append("far_from_shape")

    rt_stop_sequence = rt.get("current_stop_sequence")
    sim_stop_sequence = sim.get("current_stop_sequence")
    if rt_stop_sequence is not None and sim_stop_sequence is not None:
        delta = abs(int(rt_stop_sequence) - int(sim_stop_sequence))
        if delta == 0:
            score += 15
            reasons.append("stop_seq_exact")
        elif delta <= 1:
            score += 8
            reasons.append("stop_seq_close")
        else:
            score -= 10
            reasons.append("stop_seq_far")

    bearing_error = bearing_delta_deg(rt.get("bearing"), shape_bearing)
    if bearing_error is not None:
        if bearing_error <= 25:
            score += 10
            reasons.append("bearing_good")
        elif bearing_error <= 60:
            score += 4
            reasons.append("bearing_ok")
        else:
            score -= 8
            reasons.append("bearing_bad")

    # Position proximity tiebreaker
    try:
        import math as _math
        _dlat = math.radians(float(sim["lat"]) - float(rt["lat"]))
        _dlon = math.radians(float(sim["lon"]) - float(rt["lon"]))
        _a = _math.sin(_dlat/2)**2 + _math.cos(_math.radians(float(rt["lat"]))) * _math.cos(_math.radians(float(sim["lat"]))) * _math.sin(_dlon/2)**2
        _dist_m = 2 * 6371000 * _math.atan2(_math.sqrt(_a), _math.sqrt(1 - _a))
        if _dist_m <= 100:
            score += 10
            reasons.append("pos_close")
        elif _dist_m <= 300:
            score += 5
            reasons.append("pos_near")
    except Exception:
        pass

    if rt.get("start_time") and "@" in str(sim.get("trip_instance_id", "")):
        try:
            sim_start = int(str(sim["trip_instance_id"]).rsplit("@", 1)[1])
            parts = str(rt["start_time"]).split(":")
            rt_start = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            if abs(rt_start - sim_start) <= 60:
                score += 10
                reasons.append("start_time")
        except Exception:
            pass

    if score >= 70:
        strategy = "scored_high"
    elif score >= 55:
        strategy = "scored_medium"
    elif score >= 35:
        strategy = "scored_low"
    else:
        strategy = "rt_only"

    return MatchScore(score=score, strategy=strategy, reasons=reasons, distance_from_shape_m=distance, bearing_error_deg=bearing_error)
