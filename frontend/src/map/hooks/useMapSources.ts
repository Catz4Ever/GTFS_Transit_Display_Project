import { useCallback, useMemo, useRef } from "react";
import type { RouteGeometry, Route, Stop, Vehicle } from "../../types/transit";

export function isBoardableStop(stop: { location_type?: number | null }) {
  return stop.location_type === null || stop.location_type === undefined || Number(stop.location_type) === 0;
}

export function notNull<T>(value: T | null): value is T {
  return value !== null;
}

export function vehicleId(vehicle: Vehicle, index = 0) {
  return String(
    vehicle.id ?? vehicle.trip_instance_id ?? vehicle.vehicle_id ?? vehicle.vehicle_label ?? `${vehicle.route_id ?? "vehicle"}-${index}`,
  );
}

function bearingToArrow(bearing: number): string {
  const b = ((bearing % 360) + 360) % 360;
  if (b < 22.5 || b >= 337.5) return "↑";
  if (b < 67.5) return "↗";
  if (b < 112.5) return "→";
  if (b < 157.5) return "↘";
  if (b < 202.5) return "↓";
  if (b < 247.5) return "↙";
  if (b < 292.5) return "←";
  return "↖";
}

function bearingBetween(a: [number, number], b: [number, number]) {
  const lon1 = (a[0] * Math.PI) / 180;
  const lat1 = (a[1] * Math.PI) / 180;
  const lon2 = (b[0] * Math.PI) / 180;
  const lat2 = (b[1] * Math.PI) / 180;
  const y = Math.sin(lon2 - lon1) * Math.cos(lat2);
  const x = Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(lon2 - lon1);
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
}

export function useRouteLabels(routes: Route[]) {
  const routeLabelById = useMemo(() => {
    const labels = new Map<string, string>();
    for (const route of routes) {
      labels.set(route.route_id, route.route_short_name || route.route_long_name || route.route_id);
    }
    return labels;
  }, [routes]);

  return useCallback(
    (vehicle: Partial<Vehicle>) => (vehicle.route_id ? routeLabelById.get(vehicle.route_id) ?? vehicle.route_id : null),
    [routeLabelById],
  );
}

export function useRouteGeojson(routeGeometry: RouteGeometry | null | undefined, selectedRouteId: string | null) {
  return useMemo(() => {
    if (!selectedRouteId) {
      return { type: "FeatureCollection" as const, features: [] };
    }
    // If a route is selected but geometry is loading or empty, always return
    // empty features — never keep the previous route's shape on screen.
    if (!routeGeometry || routeGeometry.point_count === 0) {
      return { type: "FeatureCollection" as const, features: [] };
    }

    const coordinates = (routeGeometry?.points ?? [])
      .map((point) => [Number(point.lon), Number(point.lat)] as [number, number])
      .filter(([lon, lat]) => Number.isFinite(lon) && Number.isFinite(lat));

    return {
      type: "FeatureCollection" as const,
      features:
        coordinates.length >= 2
          ? [
              {
                type: "Feature" as const,
                geometry: { type: "LineString" as const, coordinates },
                properties: { route_id: selectedRouteId ?? "" },
              },
            ]
          : [],
    };
  }, [routeGeometry, selectedRouteId]);
}

export function useVehicleGeojson(
  visibleVehicles: Vehicle[],
  _selectedVehicleId: string | null,
  labelForVehicleRoute: (vehicle: Partial<Vehicle>) => string | null,
) {
  const positionHistoryRef = useRef<globalThis.Map<string, [number, number][]>>(new globalThis.Map());
  const lastBearingRef = useRef<globalThis.Map<string, number>>(new globalThis.Map());

  return useMemo(() => {
    const nextHistory = new globalThis.Map<string, [number, number][]>();

    const features = visibleVehicles
      .filter((v) => !(!v.trip_id && !v.trip_pk && !v.route_id))
      .map((v, index) => {
        const lon = Number(v.lon ?? v.longitude);
        const lat = Number(v.lat ?? v.latitude);
        if (!Number.isFinite(lon) || !Number.isFinite(lat)) return null;

        const id = vehicleId(v, index);
        const history = positionHistoryRef.current.get(id) ?? [];
        const newHistory = [...history, [lon, lat] as [number, number]].slice(-5);
        nextHistory.set(id, newHistory);

        const providedBearing = Number(v.bearing);
        let bearing = 0;
        if (Number.isFinite(providedBearing) && providedBearing > 5) {
          bearing = providedBearing;
        } else if (newHistory.length >= 2) {
          const oldest = newHistory[0];
          const newest = newHistory[newHistory.length - 1];
          if (oldest[0] !== newest[0] || oldest[1] !== newest[1]) {
            bearing = bearingBetween(oldest, newest);
          }
        }
        // If still 0, use last known bearing to prevent arrow disappearing on click/snapshot
        if (bearing <= 5) {
          bearing = lastBearingRef.current.get(id) ?? 0;
        } else {
          lastBearingRef.current.set(id, bearing);
        }

        const routeLabel = v.route_short_name ?? labelForVehicleRoute(v) ?? v.route_id ?? "?";
        const arrow = bearing > 5 ? bearingToArrow(bearing) : "";
        const pillLabel = arrow ? `${arrow} ${routeLabel}` : routeLabel;
        // iconKey: per color+label+bearing-octant so arrow direction is baked into canvas image
        const routeColorHex = (v.route_color ?? "c33149").replace(/^#/, "");
        const safeName = routeLabel.replace(/[^a-zA-Z0-9]/g, "_");
        // Snap bearing to 8 directions (0,45,90,135,180,225,270,315) for cache efficiency
        const bearingOctant = bearing > 5 ? Math.round(bearing / 45) * 45 % 360 : -1;
        const iconKey = bearingOctant >= 0
          ? `vbadge_${routeColorHex}_${safeName}_arr_${bearingOctant}`
          : `vbadge_${routeColorHex}_${safeName}`;


        return {
          type: "Feature" as const,
          geometry: { type: "Point" as const, coordinates: [lon, lat] },
          properties: {
            id,
            route_id: v.route_id ?? "",
            source: v.source ?? "",
            vehicle_label: v.vehicle_label ?? "",
            trip_id: v.trip_id ?? "",
            label: routeLabel,
            pillLabel,
            iconKey,
            bearing,
            unknown: !v.trip_id && !v.trip_pk && !v.route_id,
            route_color: v.route_color ? `#${v.route_color.replace(/^#/, "")}` : null,
            route_text_color: v.route_text_color ? `#${v.route_text_color.replace(/^#/, "")}` : null,
          },
        };
      })
      .filter(notNull);

    positionHistoryRef.current = nextHistory;
    return { type: "FeatureCollection" as const, features };
  }, [labelForVehicleRoute, visibleVehicles]);
}

export function useStopGeojson(stops: Stop[], routeGeometry: RouteGeometry | null | undefined, selectedRouteId: string | null) {
  const routeStopIds = useMemo(() => new Set((routeGeometry?.stops ?? []).map((stop) => stop.stop_id)), [routeGeometry]);
  const visibleStops = useMemo(() => stops.filter(isBoardableStop), [stops]);

  return useMemo(() => {
    return {
      type: "FeatureCollection" as const,
      features: visibleStops
        .map((stop) => {
          if (!Number.isFinite(stop.lon) || !Number.isFinite(stop.lat)) return null;

          return {
            type: "Feature" as const,
            geometry: { type: "Point" as const, coordinates: [stop.lon, stop.lat] },
            properties: {
              stop_id: stop.stop_id,
              stop_code: stop.stop_code ?? "",
              stop_name: stop.stop_name,
              route_stop: Boolean(selectedRouteId && routeStopIds.has(stop.stop_id)),
            },
          };
        })
        .filter(notNull),
    };
  }, [routeStopIds, selectedRouteId, visibleStops]);
}