import { useQuery } from "@tanstack/react-query";
import { fetchRouteGeometry } from "../../api/referenceData";

export function useRouteGeometry(cityCode: string | null, routeId: string | null) {
  return useQuery({
    queryKey: ["routeGeometry", cityCode, routeId],
    queryFn: () => fetchRouteGeometry(cityCode!, routeId!),
    enabled: Boolean(cityCode && routeId),
    staleTime: 10 * 60 * 1000,
    retry: 2,

    refetchInterval: (query) =>
      (query.state.data?.point_count ?? 0) === 0 && (query.state.dataUpdateCount ?? 0) < 3 ? 2000 : false,
  });
}
