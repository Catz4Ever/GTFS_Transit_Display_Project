import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchCities, fetchRoutes, fetchStops } from "../api/referenceData";
import { useCityStore } from "../stores/cityStore";
import { useRouteStore } from "../stores/routeStore";
import { useStopStore } from "../stores/stopStore";

export function useCitiesReferenceData() {
  const setCities = useCityStore((s) => s.setCities);
  const setLoading = useCityStore((s) => s.setLoading);
  const setError = useCityStore((s) => s.setError);

  const query = useQuery({
    queryKey: ["cities"],
    queryFn: fetchCities,
    staleTime: 10 * 60 * 1000,
  });

  useEffect(() => {
    setLoading(query.isLoading);
    setError(query.error ? query.error.message : null);
    if (query.data) setCities(query.data);
  }, [query.data, query.error, query.isLoading, setCities, setError, setLoading]);
}

export function useCityReferenceData(cityCode: string | null) {
  const setRoutes = useRouteStore((s) => s.setRoutes);
  const setRouteLoading = useRouteStore((s) => s.setLoading);
  const setRouteError = useRouteStore((s) => s.setError);
  const setSelectedRouteId = useRouteStore((s) => s.setSelectedRouteId);

  const setStops = useStopStore((s) => s.setStops);
  const setStopLoading = useStopStore((s) => s.setLoading);
  const setStopError = useStopStore((s) => s.setError);
  const setSelectedStopId = useStopStore((s) => s.setSelectedStopId);
  const clearArrivals = useStopStore((s) => s.clearArrivals);

  const routesQuery = useQuery({
    queryKey: ["routes", cityCode],
    queryFn: () => fetchRoutes(cityCode!),
    enabled: Boolean(cityCode),
    staleTime: 5 * 60 * 1000,
  });

  const stopsQuery = useQuery({
    queryKey: ["stops", cityCode],
    queryFn: () => fetchStops(cityCode!),
    enabled: Boolean(cityCode),
    staleTime: 5 * 60 * 1000,
  });

  useEffect(() => {
    setSelectedRouteId(null);
    setSelectedStopId(null);
    clearArrivals();
    // Only clear routes/stops when cityCode is null (logged out / no city)
    // On city switch, keep previous data visible until new city data arrives
    if (!cityCode) {
      setRoutes([]);
      setStops([]);
    }
  }, [cityCode, clearArrivals, setRoutes, setSelectedRouteId, setSelectedStopId, setStops]);

  useEffect(() => {
    setRouteLoading(routesQuery.isLoading || routesQuery.isFetching);
    setRouteError(routesQuery.error ? routesQuery.error.message : null);
    if (routesQuery.data) setRoutes(routesQuery.data);
  }, [routesQuery.data, routesQuery.error, routesQuery.isFetching, routesQuery.isLoading, setRouteError, setRouteLoading, setRoutes]);

  useEffect(() => {
    setStopLoading(stopsQuery.isLoading || stopsQuery.isFetching);
    setStopError(stopsQuery.error ? stopsQuery.error.message : null);
    if (stopsQuery.data) setStops(stopsQuery.data);
  }, [stopsQuery.data, stopsQuery.error, stopsQuery.isFetching, stopsQuery.isLoading, setStopError, setStopLoading, setStops]);
}
