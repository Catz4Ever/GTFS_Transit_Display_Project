export type City = {
  city_code: string;
  city_name: string;
  stop_count?: number;
  route_count?: number;
  center?: [number, number] | null;
  bounds?: [[number, number], [number, number]] | null;
  timezone?: string | null;
  timezone_name?: string | null;
};

export type Route = {
  route_id: string;
  route_short_name?: string | null;
  route_long_name?: string | null;
  route_type?: number | null;
  route_color?: string | null;
  route_text_color?: string | null;
  active_vehicle_count?: number;
  simulated_vehicle_count?: number;
};

export type Stop = {
  stop_id: string;
  stop_code?: string | null;
  stop_name: string;
  lat: number;
  lon: number;
  location_type?: number | null;
  parent_station_id?: string | null;
};

export type RouteShapePoint = {
  sequence: number;
  lat: number;
  lon: number;
  distance_traveled?: number | null;
};

export type RouteStop = Stop & {
  stop_sequence?: number;
  arrival_time?: string | null;
  departure_time?: string | null;
  eta_seconds?: number | null;
};

export type RouteGeometry = {
  city?: { code: string; name: string };
  route_id: string;
  route_short_name?: string | null;
  route_long_name?: string | null;
  shape_id?: string | null;
  point_count: number;
  stop_count: number;
  points: RouteShapePoint[];
  stops: RouteStop[];
};

export type TripStops = {
  city?: { code: string; name: string };
  trip_id: string;
  route_id: string;
  route_short_name?: string | null;
  trip_headsign?: string | null;
  stop_count: number;
  stops: RouteStop[];
};

export type Vehicle = {
  id: string;
  trip_instance_id?: string;
  vehicle_id?: string | null;
  vehicle_label?: string | null;
  raw_rt_trip_id?: string | null;
  trip_pk?: number | null;
  trip_id?: string | null;
  route_id?: string | null;
  service_id?: string | null;
  shape_id?: string | null;
  source?: string;
  status?: string;
  start_date?: string | null;
  start_time?: string | null;
  position_timestamp?: number | null;
  match_strategy?: string | null;
  match_confidence?: number | null;
  trip_progress?: number | null;
  lat: number;
  lon: number;
  latitude?: number;
  longitude?: number;
  bearing?: number | null;
  current_stop_sequence?: number | null;
  route_short_name?: string | null;
  route_long_name?: string | null;
  route_color?: string | null;
  route_text_color?: string | null;
  route_type?: number | null;
};

export type VehicleSnapshot = {
  city?: string;
  updated_at?: string;
  vehicle_count?: number;
  count?: number;
  vehicles?: Vehicle[];
};
