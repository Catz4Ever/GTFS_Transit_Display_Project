import type { City, Route, RouteGeometry, RouteShapePoint, RouteStop, Stop, TripStops, Vehicle, VehicleSnapshot } from "../types/transit";
import type { StopArrival, StopArrivalsPayload } from "../stores/stopStore";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringOrNull(value: unknown): string | null | undefined {
  if (value === null) return null;
  if (value === undefined) return undefined;
  return String(value);
}

function numberOrNull(value: unknown): number | null | undefined {
  if (value === null) return null;
  if (value === undefined || value === "") return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function finiteNumber(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function tuple2(value: unknown): [number, number] | null {
  if (!Array.isArray(value) || value.length < 2) return null;
  const a = finiteNumber(value[0]);
  const b = finiteNumber(value[1]);
  return a === null || b === null ? null : [a, b];
}

function bounds(value: unknown): [[number, number], [number, number]] | null {
  if (!Array.isArray(value) || value.length < 2) return null;
  const a = tuple2(value[0]);
  const b = tuple2(value[1]);
  return a && b ? [a, b] : null;
}

export function parseCity(raw: unknown): City | null {
  if (!isRecord(raw)) return null;
  const city_code = raw.city_code ?? raw.code;
  const city_name = raw.city_name ?? raw.name;
  if (!city_code || !city_name) return null;

  return {
    ...raw,
    city_code: String(city_code),
    city_name: String(city_name),
    stop_count: numberOrNull(raw.stop_count) ?? undefined,
    route_count: numberOrNull(raw.route_count) ?? undefined,
    center: tuple2(raw.center),
    bounds: bounds(raw.bounds),
    timezone: stringOrNull(raw.timezone),
    timezone_name: stringOrNull(raw.timezone_name),
  };
}

export function parseRoute(raw: unknown): Route | null {
  if (!isRecord(raw) || raw.route_id === undefined || raw.route_id === null) return null;
  return {
    ...raw,
    route_id: String(raw.route_id),
    route_short_name: stringOrNull(raw.route_short_name),
    route_long_name: stringOrNull(raw.route_long_name),
    route_type: numberOrNull(raw.route_type),
    route_color: stringOrNull(raw.route_color),
    route_text_color: stringOrNull(raw.route_text_color),
    active_vehicle_count: numberOrNull(raw.active_vehicle_count) ?? undefined,
    simulated_vehicle_count: numberOrNull(raw.simulated_vehicle_count) ?? undefined,
  };
}

export function parseStop(raw: unknown): Stop | null {
  if (!isRecord(raw) || raw.stop_id === undefined || raw.stop_name === undefined) return null;
  const lat = finiteNumber(raw.lat ?? raw.latitude);
  const lon = finiteNumber(raw.lon ?? raw.longitude);
  if (lat === null || lon === null) return null;

  return {
    ...raw,
    stop_id: String(raw.stop_id),
    stop_code: stringOrNull(raw.stop_code),
    stop_name: String(raw.stop_name),
    lat,
    lon,
    location_type: numberOrNull(raw.location_type),
    parent_station_id: stringOrNull(raw.parent_station_id),
  };
}

export function parseRouteStop(raw: unknown): RouteStop | null {
  const stop = parseStop(raw);
  if (!stop || !isRecord(raw)) return null;
  return {
    ...stop,
    stop_sequence: numberOrNull(raw.stop_sequence) ?? undefined,
    arrival_time: stringOrNull(raw.arrival_time) ?? undefined,
    departure_time: stringOrNull(raw.departure_time) ?? undefined,
    eta_seconds: numberOrNull(raw.eta_seconds),
  };
}

export function parseShapePoint(raw: unknown): RouteShapePoint | null {
  if (!isRecord(raw)) return null;
  const lat = finiteNumber(raw.lat ?? raw.latitude);
  const lon = finiteNumber(raw.lon ?? raw.longitude);
  if (lat === null || lon === null) return null;
  return {
    sequence: numberOrNull(raw.sequence) ?? 0,
    lat,
    lon,
    distance_traveled: numberOrNull(raw.distance_traveled),
  };
}

export function parseRouteGeometry(raw: unknown): RouteGeometry {
  if (!isRecord(raw)) throw new Error("Invalid route geometry payload");
  const route_id = raw.route_id;
  if (route_id === undefined || route_id === null) throw new Error("Route geometry is missing route_id");
  const points = Array.isArray(raw.points) ? raw.points.map(parseShapePoint).filter((v): v is RouteShapePoint => v !== null) : [];
  const stops = Array.isArray(raw.stops) ? raw.stops.map(parseRouteStop).filter((v): v is RouteStop => v !== null) : [];
  return {
    ...raw,
    route_id: String(route_id),
    route_short_name: stringOrNull(raw.route_short_name),
    route_long_name: stringOrNull(raw.route_long_name),
    shape_id: stringOrNull(raw.shape_id),
    point_count: numberOrNull(raw.point_count) ?? points.length,
    stop_count: numberOrNull(raw.stop_count) ?? stops.length,
    points,
    stops,
  };
}

export function parseTripStops(raw: unknown): TripStops {
  if (!isRecord(raw)) throw new Error("Invalid trip stops payload");
  const trip_id = raw.trip_id;
  const route_id = raw.route_id;
  if (trip_id === undefined || route_id === undefined) throw new Error("Trip stops payload is missing IDs");
  const stops = Array.isArray(raw.stops) ? raw.stops.map(parseRouteStop).filter((v): v is RouteStop => v !== null) : [];
  return {
    ...raw,
    trip_id: String(trip_id),
    route_id: String(route_id),
    route_short_name: stringOrNull(raw.route_short_name),
    trip_headsign: stringOrNull(raw.trip_headsign),
    stop_count: numberOrNull(raw.stop_count) ?? stops.length,
    stops,
  };
}

export function parseVehicle(raw: unknown, index = 0): Vehicle | null {
  if (!isRecord(raw)) return null;
  const lat = finiteNumber(raw.lat ?? raw.latitude);
  const lon = finiteNumber(raw.lon ?? raw.longitude);
  if (lat === null || lon === null) return null;

  const id = String(raw.id ?? raw.trip_instance_id ?? raw.vehicle_id ?? raw.vehicle_label ?? `${raw.route_id ?? "vehicle"}-${index}`);

  return {
    ...raw,
    id,
    trip_instance_id: stringOrNull(raw.trip_instance_id) ?? undefined,
    vehicle_id: stringOrNull(raw.vehicle_id),
    vehicle_label: stringOrNull(raw.vehicle_label),
    raw_rt_trip_id: stringOrNull(raw.raw_rt_trip_id),
    trip_id: stringOrNull(raw.trip_id),
    route_id: stringOrNull(raw.route_id),
    service_id: stringOrNull(raw.service_id),
    shape_id: stringOrNull(raw.shape_id),
    source: stringOrNull(raw.source) ?? undefined,
    status: stringOrNull(raw.status) ?? undefined,
    start_date: stringOrNull(raw.start_date),
    start_time: stringOrNull(raw.start_time),
    position_timestamp: numberOrNull(raw.position_timestamp),
    match_strategy: stringOrNull(raw.match_strategy),
    match_confidence: numberOrNull(raw.match_confidence),
    trip_progress: numberOrNull(raw.trip_progress),
    lat,
    lon,
    latitude: numberOrNull(raw.latitude) ?? undefined,
    longitude: numberOrNull(raw.longitude) ?? undefined,
    bearing: numberOrNull(raw.bearing),
    route_short_name: stringOrNull(raw.route_short_name),
    route_long_name: stringOrNull(raw.route_long_name),
    route_color: stringOrNull(raw.route_color),
    route_text_color: stringOrNull(raw.route_text_color),
  };
}

export function parseVehicleSnapshot(raw: unknown): VehicleSnapshot {
  if (!isRecord(raw)) throw new Error("Invalid vehicle snapshot payload");
  const vehicles = Array.isArray(raw.vehicles) ? raw.vehicles.map(parseVehicle).filter((v): v is Vehicle => v !== null) : [];
  return {
    city: stringOrNull(raw.city) ?? undefined,
    updated_at: stringOrNull(raw.updated_at) ?? undefined,
    vehicle_count: numberOrNull(raw.vehicle_count) ?? undefined,
    count: numberOrNull(raw.count) ?? undefined,
    vehicles,
  };
}

function parseArrival(raw: unknown): StopArrival | null {
  if (!isRecord(raw)) return null;
  return {
    ...raw,
    route_id: String(raw.route_id ?? ""),
    route_name: stringOrNull(raw.route_name ?? raw.route_short_name ?? raw.route_long_name),
    trip_id: stringOrNull(raw.trip_id),
    trip_instance_id: stringOrNull(raw.trip_instance_id),
    arrival_time: stringOrNull(raw.arrival_time) ?? undefined,
    departure_time: stringOrNull(raw.departure_time) ?? undefined,
    eta_seconds: numberOrNull(raw.eta_seconds),
    source: stringOrNull(raw.source),
  };
}

export function parseStopArrivalsPayload(raw: unknown): StopArrivalsPayload {
  if (!isRecord(raw)) throw new Error("Invalid stop arrivals payload");
  return {
    city: String(raw.city ?? ""),
    stop_id: String(raw.stop_id ?? ""),
    stop_name: stringOrNull(raw.stop_name) ?? undefined,
    updated_at: stringOrNull(raw.updated_at) ?? undefined,
    arrivals: Array.isArray(raw.arrivals) ? raw.arrivals.map(parseArrival).filter((v): v is StopArrival => v !== null) : [],
  };
}
