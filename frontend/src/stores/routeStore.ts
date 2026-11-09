import { create } from "zustand";
import type { Route } from "../types/transit";

type RouteState = {
  routes: Route[];
  selectedRouteId: string | null;
  loading: boolean;
  error: string | null;

  setRoutes: (routes: Route[]) => void;
  setSelectedRouteId: (routeId: string | null) => void;
  setLoading: (loading: boolean) => void;
  setError: (error: string | null) => void;
};

export const useRouteStore = create<RouteState>((set) => ({
  routes: [],
  selectedRouteId: null,
  loading: false,
  error: null,

  setRoutes: (routes) => set({ routes }),
  setSelectedRouteId: (selectedRouteId) => set({ selectedRouteId }),
  setLoading: (loading) => set({ loading }),
  setError: (error) => set({ error }),
}));
