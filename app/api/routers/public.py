import time
from fastapi import APIRouter, HTTPException, Response

from app.api.pool import get_conn

# Simple in-process cache for expensive static endpoints
# Keyed by (endpoint, city_code), value is (timestamp, response_dict)
_cache: dict[tuple, tuple[float, object]] = {}
_ROUTES_TTL = 15          # seconds — routes have live vehicle counts
_STOPS_TTL = 300          # seconds — stops are static, rarely change
_CITIES_TTL = 60          # seconds

def _cache_get(key: tuple, ttl: float) -> object | None:
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < ttl:
        return entry[1]
    return None

def _cache_set(key: tuple, value: object) -> None:
    _cache[key] = (time.time(), value)
from transit.services.live_stop_arrivals import get_live_stop_arrivals


router = APIRouter()


@router.get("/health")
def health():
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/cities")
def list_cities(response: Response):
    cached = _cache_get(("cities",), _CITIES_TTL)
    if cached is not None:
        response.headers["X-Cache"] = "HIT"
        return cached
    try:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT
                    c.city_code,
                    c.city_name,
                    COUNT(DISTINCT s.stop_pk) AS stop_count,
                    COUNT(DISTINCT r.route_pk) AS route_count,
                    AVG(s.stop_lon) AS center_lon,
                    AVG(s.stop_lat) AS center_lat,
                    MIN(s.stop_lon) AS min_lon,
                    MIN(s.stop_lat) AS min_lat,
                    MAX(s.stop_lon) AS max_lon,
                    MAX(s.stop_lat) AS max_lat,
                    crc.timezone_name,
                    crc.service_day_rollover_hour
                FROM cities c
                LEFT JOIN stops s ON s.city_id = c.city_id
                LEFT JOIN routes r ON r.city_id = c.city_id
                LEFT JOIN city_runtime_config crc ON crc.city_id = c.city_id
                WHERE COALESCE(crc.is_enabled, TRUE) = TRUE
                GROUP BY c.city_id, c.city_code, c.city_name, crc.timezone_name, crc.service_day_rollover_hour
                ORDER BY c.city_name
            """).fetchall()

            cities = []
            for r in rows:
                center_lon = float(r[4]) if r[4] is not None else None
                center_lat = float(r[5]) if r[5] is not None else None

                cities.append(
                    {
                        "city_code": r[0],
                        "city_name": r[1],
                        "stop_count": int(r[2] or 0),
                        "route_count": int(r[3] or 0),
                        "center": [center_lon, center_lat] if center_lon is not None and center_lat is not None else None,
                        "bounds": [
                            [float(r[6]), float(r[7])],
                            [float(r[8]), float(r[9])],
                        ] if all(value is not None for value in r[6:10]) else None,
                        "timezone_name": r[10],
                        "rollover_hour": r[11],
                    }
                )

            result = {"cities": cities}
            _cache_set(("cities",), result)
            return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/cities/{city_code}/routes")
def list_city_routes(city_code: str, response: Response):
    cached = _cache_get(("routes", city_code), _ROUTES_TTL)
    if cached is not None:
        response.headers["X-Cache"] = "HIT"
        return cached
    try:
        with get_conn() as conn:
            city_row = conn.execute("""
                SELECT c.city_id, c.city_name
                FROM cities c
                LEFT JOIN city_runtime_config crc ON crc.city_id = c.city_id
                WHERE c.city_code = %s
                  AND COALESCE(crc.is_enabled, TRUE) = TRUE
            """, (city_code,)).fetchone()

            if city_row is None:
                raise HTTPException(status_code=404, detail="City not found")

            city_id, city_name = city_row

            rows = conn.execute("""
                SELECT
                    r.route_id,
                    r.route_short_name,
                    r.route_long_name,
                    r.route_type,
                    r.route_color,
                    r.route_text_color,
                    COUNT(DISTINCT v.trip_instance_id) AS active_vehicle_count,
                    COUNT(DISTINCT s.trip_instance_id) AS simulated_vehicle_count
                FROM routes r
                LEFT JOIN vehicle_states_current v
                  ON v.city_id = r.city_id
                 AND v.route_id = r.route_id
                LEFT JOIN vehicle_states_simulated_current s
                  ON s.city_id = r.city_id
                 AND s.route_id = r.route_id
                WHERE r.city_id = %s
                GROUP BY
                    r.route_pk,
                    r.route_id,
                    r.route_short_name,
                    r.route_long_name,
                    r.route_type,
                    r.route_color,
                    r.route_text_color
                ORDER BY COALESCE(r.route_short_name, r.route_id)
            """, (city_id,)).fetchall()

            routes = [
                {
                    "route_id": r[0],
                    "route_short_name": r[1],
                    "route_long_name": r[2],
                    "route_type": r[3],
                    "route_color": r[4],
                    "route_text_color": r[5],
                    "active_vehicle_count": int(r[6] or 0),
                    "simulated_vehicle_count": int(r[7] or 0),
                }
                for r in rows
            ]

            result = {
                "city": {"code": city_code, "name": city_name},
                "route_count": len(routes),
                "routes": routes,
            }
            _cache_set(("routes", city_code), result)
            return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/cities/{city_code}/stops")
def list_city_stops(city_code: str, response: Response):
    cached = _cache_get(("stops", city_code), _STOPS_TTL)
    if cached is not None:
        response.headers["X-Cache"] = "HIT"
        return cached
    try:
        with get_conn() as conn:
            city_row = conn.execute("""
                SELECT c.city_id, c.city_name
                FROM cities c
                LEFT JOIN city_runtime_config crc ON crc.city_id = c.city_id
                WHERE c.city_code = %s
                  AND COALESCE(crc.is_enabled, TRUE) = TRUE
            """, (city_code,)).fetchone()

            if city_row is None:
                raise HTTPException(status_code=404, detail="City not found")

            city_id, city_name = city_row

            rows = conn.execute("""
                SELECT
                    stop_id,
                    stop_code,
                    stop_name,
                    stop_lat,
                    stop_lon,
                    location_type,
                    parent_station_id
                FROM stops
                WHERE city_id = %s
                ORDER BY stop_name, stop_id
            """, (city_id,)).fetchall()

            stops = [
                {
                    "stop_id": r[0],
                    "stop_code": r[1],
                    "stop_name": r[2],
                    "lat": float(r[3]),
                    "lon": float(r[4]),
                    "location_type": r[5],
                    "parent_station_id": r[6],
                }
                for r in rows
            ]

            result = {
                "city": {"code": city_code, "name": city_name},
                "stop_count": len(stops),
                "stops": stops,
            }
            _cache_set(("stops", city_code), result)
            return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@router.get("/cities/{city_code}/routes/{route_id}/shape")
def get_city_route_shape(city_code: str, route_id: str):
    try:
        with get_conn() as conn:
            city_row = conn.execute("""
                SELECT c.city_id, c.city_name
                FROM cities c
                LEFT JOIN city_runtime_config crc ON crc.city_id = c.city_id
                WHERE c.city_code = %s
                  AND COALESCE(crc.is_enabled, TRUE) = TRUE
            """, (city_code,)).fetchone()

            if city_row is None:
                raise HTTPException(status_code=404, detail="City not found")

            city_id, city_name = city_row

            route_row = conn.execute("""
                SELECT route_pk, route_id, route_short_name, route_long_name
                FROM routes
                WHERE city_id = %s
                  AND (route_id = %s OR route_short_name = %s)
                ORDER BY CASE WHEN route_id = %s THEN 0 ELSE 1 END
                LIMIT 1
            """, (city_id, route_id, route_id, route_id)).fetchone()

            if route_row is None:
                raise HTTPException(status_code=404, detail="Route not found")

            route_pk, canonical_route_id, route_short_name, route_long_name = route_row

            # Prefer the shape used by currently active vehicles on this route,
            # but among those pick the one with the most points (full route vs terminal loop).
            shape_rows = conn.execute("""
                SELECT sh.shape_pk, sh.shape_id, COUNT(*) AS trip_count
                FROM trips t
                JOIN shapes sh ON sh.shape_pk = t.shape_pk
                WHERE t.city_id = %s
                  AND t.route_pk = %s
                  AND t.shape_pk IS NOT NULL
                  AND t.trip_pk IN (
                      SELECT trip_pk FROM vehicle_states_current
                      WHERE city_id = %s AND trip_pk IS NOT NULL
                      UNION
                      SELECT trip_pk FROM vehicle_states_simulated_current
                      WHERE city_id = %s AND trip_pk IS NOT NULL
                  )
                GROUP BY sh.shape_pk, sh.shape_id
                ORDER BY trip_count DESC, sh.shape_id
                LIMIT 5
            """, (city_id, route_pk, city_id, city_id)).fetchall()

            # Among active shapes, prefer the one with the most points
            # to avoid short terminal loops winning over the full route.
            if shape_rows:
                shape_row = conn.execute("""
                    SELECT sh.shape_pk, sh.shape_id, COUNT(sp.shape_pt_sequence) AS point_count
                    FROM shapes sh
                    JOIN shape_points sp ON sp.shape_pk = sh.shape_pk
                    WHERE sh.shape_pk = ANY(%s)
                    GROUP BY sh.shape_pk, sh.shape_id
                    ORDER BY point_count DESC
                    LIMIT 1
                """, ([r[0] for r in shape_rows],)).fetchone()
            else:
                shape_row = None

            # No active vehicles — fall back to the shape with the most points
            # (longest/most complete shape rather than most common by trip count).
            if shape_row is None:
                shape_row = conn.execute("""
                SELECT sh.shape_pk, sh.shape_id, COUNT(sp.shape_pt_sequence) AS point_count
                FROM trips t
                JOIN shapes sh ON sh.shape_pk = t.shape_pk
                JOIN shape_points sp ON sp.shape_pk = sh.shape_pk
                WHERE t.city_id = %s
                  AND t.route_pk = %s
                  AND t.shape_pk IS NOT NULL
                GROUP BY sh.shape_pk, sh.shape_id
                ORDER BY point_count DESC
                LIMIT 1
            """, (city_id, route_pk)).fetchone()

            if shape_row is None:
                return {
                    "city": {"code": city_code, "name": city_name},
                    "route_id": canonical_route_id,
                    "route_short_name": route_short_name,
                    "route_long_name": route_long_name,
                    "shape_id": None,
                    "point_count": 0,
                    "stop_count": 0,
                    "points": [],
                    "stops": [],
                }

            shape_pk, shape_id, _trip_count = shape_row

            point_rows = conn.execute("""
                SELECT shape_pt_sequence, shape_pt_lat, shape_pt_lon, shape_dist_traveled
                FROM shape_points
                WHERE shape_pk = %s
                ORDER BY shape_pt_sequence
            """, (shape_pk,)).fetchall()

            # Pick a representative trip for the route/shape and use its ordered stop_times.
            trip_row = conn.execute("""
                SELECT trip_pk, trip_id
                FROM trips
                WHERE city_id = %s
                  AND route_pk = %s
                  AND shape_pk = %s
                ORDER BY trip_id
                LIMIT 1
            """, (city_id, route_pk, shape_pk)).fetchone()

            stop_rows = []
            if trip_row is not None:
                stop_rows = conn.execute("""
                    SELECT
                        st.stop_sequence,
                        st.arrival_time,
                        st.departure_time,
                        s.stop_id,
                        s.stop_code,
                        s.stop_name,
                        s.stop_lat,
                        s.stop_lon,
                        s.location_type,
                        s.parent_station_id
                    FROM stop_times st
                    JOIN stops s ON s.stop_pk = st.stop_pk
                    WHERE st.trip_pk = %s
                    ORDER BY st.stop_sequence
                """, (trip_row[0],)).fetchall()

            points = [
                {
                    "sequence": int(r[0]),
                    "lat": float(r[1]),
                    "lon": float(r[2]),
                    "distance_traveled": float(r[3]) if r[3] is not None else None,
                }
                for r in point_rows
            ]

            stops = [
                {
                    "stop_sequence": int(r[0]),
                    "arrival_time": r[1],
                    "departure_time": r[2],
                    "stop_id": r[3],
                    "stop_code": r[4],
                    "stop_name": r[5],
                    "lat": float(r[6]),
                    "lon": float(r[7]),
                    "location_type": r[8],
                    "parent_station_id": r[9],
                }
                for r in stop_rows
            ]

            return {
                "city": {"code": city_code, "name": city_name},
                "route_id": canonical_route_id,
                "route_short_name": route_short_name,
                "route_long_name": route_long_name,
                "shape_id": shape_id,
                "point_count": len(points),
                "stop_count": len(stops),
                "points": points,
                "stops": stops,
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/cities/{city_code}/trips/{trip_id}/stops")
def get_city_trip_stops(city_code: str, trip_id: str):
    try:
        with get_conn() as conn:
            city_row = conn.execute("""
                SELECT c.city_id, c.city_name
                FROM cities c
                LEFT JOIN city_runtime_config crc ON crc.city_id = c.city_id
                WHERE c.city_code = %s
                  AND COALESCE(crc.is_enabled, TRUE) = TRUE
            """, (city_code,)).fetchone()

            if city_row is None:
                raise HTTPException(status_code=404, detail="City not found")

            city_id, city_name = city_row

            trip_row = conn.execute("""
                SELECT t.trip_pk, t.trip_id, r.route_id, r.route_short_name, t.trip_headsign
                FROM trips t
                JOIN routes r ON r.route_pk = t.route_pk
                WHERE t.city_id = %s
                  AND t.trip_id = %s
            """, (city_id, trip_id)).fetchone()

            if trip_row is None:
                raise HTTPException(status_code=404, detail="Trip not found")

            trip_pk, canonical_trip_id, route_id, route_short_name, trip_headsign = trip_row

            rows = conn.execute("""
                SELECT
                    st.stop_sequence,
                    st.arrival_time,
                    st.departure_time,
                    s.stop_id,
                    s.stop_code,
                    s.stop_name,
                    s.stop_lat,
                    s.stop_lon
                FROM stop_times st
                JOIN stops s ON s.stop_pk = st.stop_pk
                WHERE st.trip_pk = %s
                ORDER BY st.stop_sequence
            """, (trip_pk,)).fetchall()

            # Fetch RT stop time updates for this trip to enrich with live ETAs
            import time as _time
            now_epoch = _time.time()
            rt_rows = conn.execute("""
                SELECT stop_sequence, stop_id, arrival_time_epoch, departure_time_epoch,
                       arrival_delay_seconds, departure_delay_seconds
                FROM gtfs_rt_stop_time_updates_current
                WHERE city_id = %s AND trip_key = %s
            """, (city_id, trip_id)).fetchall()

            # Build lookup: stop_sequence -> eta_seconds
            rt_by_seq = {}
            rt_by_stop_id = {}
            for rt in rt_rows:
                seq, stop_id_rt, arr_epoch, dep_epoch, arr_delay, dep_delay = rt
                epoch = arr_epoch or dep_epoch
                if epoch:
                    eta = int(epoch - now_epoch)
                    if seq is not None:
                        rt_by_seq[int(seq)] = eta
                    if stop_id_rt:
                        rt_by_stop_id[str(stop_id_rt)] = eta

            stops = []
            for r in rows:
                seq = int(r[0])
                stop_id = r[3]
                eta_seconds = rt_by_seq.get(seq) or rt_by_stop_id.get(str(stop_id))
                stops.append({
                    "stop_sequence": seq,
                    "arrival_time": r[1],
                    "departure_time": r[2],
                    "stop_id": stop_id,
                    "stop_code": r[4],
                    "stop_name": r[5],
                    "lat": float(r[6]),
                    "lon": float(r[7]),
                    **({"eta_seconds": eta_seconds} if eta_seconds is not None else {}),
                })

            return {
                "city": {"code": city_code, "name": city_name},
                "trip_id": canonical_trip_id,
                "route_id": route_id,
                "route_short_name": route_short_name,
                "trip_headsign": trip_headsign,
                "stop_count": len(stops),
                "stops": stops,
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/cities/{city_code}/stops/{stop_id}/routes")
def get_stop_routes(city_code: str, stop_id: str):
    """Return all routes that serve a given stop, with metadata."""
    with get_conn() as conn:
        city = conn.execute(
            "SELECT city_id FROM cities WHERE city_code = %s", (city_code,)
        ).fetchone()
        if not city:
            raise HTTPException(status_code=404, detail="City not found")
        city_id = int(city[0])

        rows = conn.execute(
            """
            SELECT DISTINCT
                r.route_id,
                r.route_short_name,
                r.route_long_name,
                r.route_type,
                r.route_color,
                r.route_text_color
            FROM stop_times st
            JOIN trips t ON t.trip_pk = st.trip_pk
            JOIN routes r ON r.route_pk = t.route_pk
            JOIN stops s ON s.stop_pk = st.stop_pk
            WHERE s.city_id = %s
              AND s.stop_id = %s
              AND r.city_id = %s
            ORDER BY r.route_short_name, r.route_id
            """,
            (city_id, stop_id, city_id),
        ).fetchall()

    return {
        "stop_id": stop_id,
        "routes": [
            {
                "route_id": str(r[0]),
                "route_short_name": str(r[1]) if r[1] else None,
                "route_long_name": str(r[2]) if r[2] else None,
                "route_type": int(r[3]) if r[3] is not None else None,
                "route_color": str(r[4]) if r[4] else None,
                "route_text_color": str(r[5]) if r[5] else None,
            }
            for r in rows
        ],
    }
def get_city_stop_arrivals(city_code: str, stop_id: str, limit: int = 12, horizon_seconds: int = 3600):
    try:
        limit = max(1, min(int(limit), 50))
        horizon_seconds = max(60, min(int(horizon_seconds), 6 * 3600))

        with get_conn() as conn:
            city_row = conn.execute(
                """
                SELECT city_id, city_name
                FROM cities
                WHERE city_code = %s
                """,
                (city_code,),
            ).fetchone()

            if city_row is None:
                raise HTTPException(status_code=404, detail="City not found")

            return get_live_stop_arrivals(
                conn,
                city_id=int(city_row[0]),
                city_code=city_code,
                city_name=str(city_row[1]),
                stop_id=stop_id,
                limit=limit,
                horizon_seconds=horizon_seconds,
            )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/cities/{city_code}/vehicles")
def get_city_vehicles(city_code: str):
    try:
        with get_conn() as conn:
            city_row = conn.execute(
                '''
                SELECT city_id, city_name
                FROM cities
                WHERE city_code = %s
                ''',
                (city_code,),
            ).fetchone()

            if city_row is None:
                raise HTTPException(status_code=404, detail="City not found")

            city_id, city_name = city_row

            rows = conn.execute(
                '''
                SELECT
                    trip_instance_id,
                    trip_id,
                    route_id,
                    status,
                    source,
                    vehicle_id,
                    vehicle_label,
                    raw_rt_trip_id,
                    start_date,
                    start_time,
                    position_timestamp,
                    match_strategy,
                    match_confidence,
                    lat,
                    lon,
                    updated_at
                FROM vehicle_states_current
                WHERE city_id = %s
                ORDER BY route_id, trip_instance_id
                ''',
                (city_id,),
            ).fetchall()

            vehicles = [
                {
                    "trip_instance_id": r[0],
                    "trip_id": r[1],
                    "route_id": r[2],
                    "status": r[3],
                    "source": r[4],
                    "vehicle_id": r[5],
                    "vehicle_label": r[6],
                    "raw_rt_trip_id": r[7],
                    "start_date": r[8],
                    "start_time": r[9],
                    "position_timestamp": r[10],
                    "match_strategy": r[11],
                    "match_confidence": float(r[12]) if r[12] is not None else None,
                    "lat": float(r[13]),
                    "lon": float(r[14]),
                    "updated_at": r[15].isoformat() if r[15] else None,
                }
                for r in rows
            ]

            return {
                "city": {
                    "code": city_code,
                    "name": city_name,
                },
                "vehicle_count": len(vehicles),
                "vehicles": vehicles,
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/cities/{city_code}/vehicles/map")
def get_city_vehicles_map(city_code: str):
    try:
        with get_conn() as conn:
            city_row = conn.execute(
                '''
                SELECT city_id, city_name
                FROM cities
                WHERE city_code = %s
                ''',
                (city_code,),
            ).fetchone()

            if city_row is None:
                raise HTTPException(status_code=404, detail="City not found")

            city_id, city_name = city_row

            rows = conn.execute(
                '''
                SELECT
                    trip_instance_id,
                    route_id,
                    lat,
                    lon,
                    status,
                    source,
                    vehicle_id,
                    vehicle_label,
                    match_strategy,
                    match_confidence,
                    updated_at
                FROM vehicle_states_current
                WHERE city_id = %s
                ORDER BY route_id, trip_instance_id
                ''',
                (city_id,),
            ).fetchall()

            vehicles = [
                {
                    "id": r[0],
                    "route": r[1],
                    "lat": r[2],
                    "lon": r[3],
                    "status": r[4],
                    "source": r[5],
                    "vehicle_id": r[6],
                    "vehicle_label": r[7],
                    "match_strategy": r[8],
                    "match_confidence": float(r[9]) if r[9] is not None else None,
                    "updated_at": r[10].isoformat() if r[10] else None,
                }
                for r in rows
            ]

            return {
                "city": {
                    "code": city_code,
                    "name": city_name,
                },
                "vehicle_count": len(vehicles),
                "vehicles": vehicles,
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# GTFS cause/effect integer → human readable label
ALERT_CAUSE = {
    1: "Unknown cause", 2: "Other cause", 3: "Technical problem",
    4: "Strike", 5: "Demonstration", 6: "Accident", 7: "Holiday",
    8: "Weather", 9: "Maintenance", 10: "Construction",
    11: "Police activity", 12: "Medical emergency",
}

ALERT_EFFECT = {
    1: "No service", 2: "Reduced service", 3: "Significant delays",
    4: "Detour", 5: "Additional service", 6: "Modified service",
    7: "Other effect", 8: "Unknown effect", 9: "Stop moved",
    10: "No effect", 11: "Accessibility issue",
}


@router.get("/cities/{city_code}/alerts")
def get_city_alerts(city_code: str, lang: str = "en"):
    try:
        with get_conn() as conn:
            city_row = conn.execute(
                "SELECT city_id FROM cities WHERE city_code = %s", (city_code,)
            ).fetchone()

            if city_row is None:
                raise HTTPException(status_code=404, detail="City not found")

            city_id = city_row[0]

            rows = conn.execute("""
                SELECT
                    entity_id, cause, effect,
                    url_translations, header_translations,
                    description_translations, active_periods,
                    informed_entities, updated_at
                FROM gtfs_rt_alerts_current
                WHERE city_id = %s
                ORDER BY updated_at DESC
            """, (city_id,)).fetchall()

            def pick_text(translations: dict | None, lang: str) -> str | None:
                if not translations:
                    return None
                return (
                    translations.get(lang)
                    or translations.get("en")
                    or next(iter(translations.values()), None)
                )

            import time as _time
            now = _time.time()

            alerts = []
            for r in rows:
                entity_id, cause, effect = r[0], r[1], r[2]
                url_t = r[3] or {}
                header_t = r[4] or {}
                desc_t = r[5] or {}
                active_periods = r[6] or []
                informed_entities = r[7] or []
                updated_at = r[8]

                # Check if alert is currently active
                is_active = True
                if active_periods:
                    is_active = any(
                        (p.get("start") or 0) <= now <= (p.get("end") or float("inf"))
                        for p in active_periods
                    )

                alerts.append({
                    "entity_id": entity_id,
                    "cause": ALERT_CAUSE.get(cause, "Unknown cause") if cause else None,
                    "effect": ALERT_EFFECT.get(effect, "Unknown effect") if effect else None,
                    "header": pick_text(header_t, lang),
                    "description": pick_text(desc_t, lang),
                    "url": pick_text(url_t, lang),
                    "active_periods": active_periods,
                    "informed_entities": informed_entities,
                    "is_active": is_active,
                    "updated_at": updated_at.isoformat() if updated_at else None,
                })

            return {
                "city_code": city_code,
                "alert_count": len(alerts),
                "alerts": alerts,
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
