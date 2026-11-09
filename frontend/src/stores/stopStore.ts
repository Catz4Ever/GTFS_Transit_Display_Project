import { create } from "zustand";
import type { Stop } from "../types/transit";

export type StopArrival = {
  route_id?: string | null;
  route_name?: string | null;
  trip_id?: string | null;
  trip_pk?: number | null;
  trip_instance_id?: string | null;
  vehicle_id?: string | null;
  vehicle_label?: string | null;
  raw_rt_trip_id?: string | null;
  stop_id?: string | null;
  stop_sequence?: number | null;
  scheduled_time?: string | null;
  predicted_time?: string | null;
  eta_seconds?: number | null;
  delay_seconds?: number | null;
  source?: string | null;
  prediction_source?: string | null;
  match_strategy?: string | null;
  match_confidence?: number | null;
  vehicle_lat?: number | null;
  vehicle_lon?: number | null;
  vehicle_updated_at?: string | null;

  // Kept for compatibility with older frontend experiments.
  arrival_time?: string;
  departure_time?: string;
  seconds_until_arrival?: number;
  minutes_until_arrival?: number;
};

export type StopArrivalsPayload = {
  city?: string;
  city_name?: string;
  stop_id?: string;
  stop_name?: string;
  updated_at?: string;
  arrival_count?: number;
  arrivals?: StopArrival[];
  fallback_schedules?: StopArrival[];
  schema_version?: number;
};

type ArrivalSource = "api" | "mqtt";

type StopState = {
  stops: Stop[];
  selectedStopId: string | null;
  arrivals: StopArrival[];
  arrivalsUpdatedAt: string | null;
  arrivalsSource: ArrivalSource | null;
  loading: boolean;
  error: string | null;
  stopStatus: string;

  setStops: (stops: Stop[]) => void;
  setSelectedStopId: (stopId: string | null) => void;
  setArrivals: (payload: StopArrivalsPayload, source?: ArrivalSource) => void;
  clearArrivals: () => void;
  setLoading: (loading: boolean) => void;
  setError: (error: string | null) => void;
  setStopStatus: (status: string) => void;
};


export const useStopStore = create<StopState>((set) => ({
  stops: [],
  selectedStopId: null,
  arrivals: [],
  arrivalsUpdatedAt: null,
  arrivalsSource: null,
  loading: false,
  error: null,
  stopStatus: "not subscribed",

  setStops: (stops) => set({ stops }),
  setSelectedStopId: (selectedStopId) => set({ selectedStopId }),
  setArrivals: (payload, source = "mqtt") =>
    set({
      arrivals: payload.arrivals ?? [],
      arrivalsUpdatedAt: payload.updated_at ?? null,
      arrivalsSource: source,
    }),
  clearArrivals: () =>
    set({
      arrivals: [],
      arrivalsUpdatedAt: null,
      arrivalsSource: null,
    }),
  setLoading: (loading) => set({ loading }),
  setError: (error) => set({ error }),
  setStopStatus: (stopStatus) => set({ stopStatus }),
}));
