import { config } from "../config/runtime";
import type { City, Route, RouteGeometry, Stop, TripStops } from "../types/transit";
import type { StopArrivalsPayload } from "../stores/stopStore";
import {
  parseCity,
  parseRoute,
  parseRouteGeometry,
  parseStop,
  parseStopArrivalsPayload,
  parseTripStops,
} from "../utils/validation";

async function getJson(path: string): Promise<unknown> {
  const response = await fetch(`${config.apiUrl}${path}`);

  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }

  return response.json() as Promise<unknown>;
}

function objectPayload(data: unknown): Record<string, unknown> {
  if (typeof data !== "object" || data === null || Array.isArray(data)) {
    throw new Error("Unexpected API response shape");
  }
  return data as Record<string, unknown>;
}

export async function fetchCities(): Promise<City[]> {
  const data = objectPayload(await getJson("/cities"));
  return Array.isArray(data.cities) ? data.cities.map(parseCity).filter((v): v is City => v !== null) : [];
}

export async function fetchRoutes(cityCode: string): Promise<Route[]> {
  const data = objectPayload(await getJson(`/cities/${encodeURIComponent(cityCode)}/routes`));
  return Array.isArray(data.routes) ? data.routes.map(parseRoute).filter((v): v is Route => v !== null) : [];
}

export async function fetchStops(cityCode: string): Promise<Stop[]> {
  const data = objectPayload(await getJson(`/cities/${encodeURIComponent(cityCode)}/stops`));
  return Array.isArray(data.stops) ? data.stops.map(parseStop).filter((v): v is Stop => v !== null) : [];
}

export async function fetchRouteGeometry(cityCode: string, routeId: string): Promise<RouteGeometry> {
  return parseRouteGeometry(
    await getJson(`/cities/${encodeURIComponent(cityCode)}/routes/${encodeURIComponent(routeId)}/shape`),
  );
}

export async function fetchTripStops(cityCode: string, tripId: string): Promise<TripStops> {
  return parseTripStops(await getJson(`/cities/${encodeURIComponent(cityCode)}/trips/${encodeURIComponent(tripId)}/stops`));
}

export async function fetchStopArrivals(
  cityCode: string,
  stopId: string,
  options: { limit?: number; horizonSeconds?: number } = {},
): Promise<StopArrivalsPayload> {
  const params = new URLSearchParams();
  if (options.limit) params.set("limit", String(options.limit));
  if (options.horizonSeconds) params.set("horizon_seconds", String(options.horizonSeconds));
  const query = params.toString();

  return parseStopArrivalsPayload(
    await getJson(
      `/cities/${encodeURIComponent(cityCode)}/stops/${encodeURIComponent(stopId)}/arrivals${query ? `?${query}` : ""}`,
    ),
  );
}

export type StopRoute = {
  route_id: string;
  route_short_name?: string | null;
  route_long_name?: string | null;
  route_type?: number | null;
  route_color?: string | null;
  route_text_color?: string | null;
};

export async function fetchStopRoutes(cityCode: string, stopId: string): Promise<StopRoute[]> {
  const raw = await getJson(`/cities/${encodeURIComponent(cityCode)}/stops/${encodeURIComponent(stopId)}/routes`);
  const data = raw as Record<string, unknown>;
  if (!data || !Array.isArray(data.routes)) return [];
  return data.routes as StopRoute[];
}
