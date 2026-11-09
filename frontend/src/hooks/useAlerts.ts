import { useEffect, useState } from "react";
import { useCityStore } from "../stores/cityStore";
import { config } from "../config/runtime";

export type Alert = {
  entity_id: string;
  cause: string | null;
  effect: string | null;
  header: string | null;
  description: string | null;
  url: string | null;
  is_active: boolean;
  informed_entities: Array<{ route_id?: string; stop_id?: string; agency_id?: string }>;
};

export function useAlerts() {
  const selectedCityCode = useCityStore((s) => s.selectedCityCode);
  const [alerts, setAlerts] = useState<Alert[]>([]);

  useEffect(() => {
    if (!selectedCityCode) { setAlerts([]); return; }
    let cancelled = false;

    async function fetch_() {
      try {
        const res = await fetch(`${config.apiUrl}/cities/${encodeURIComponent(selectedCityCode!)}/alerts`);
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setAlerts((data.alerts as Alert[]).filter((a) => a.is_active));
      } catch {}
    }

    fetch_();
    const interval = setInterval(fetch_, 60_000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [selectedCityCode]);

  // Alerts with no specific route/stop — agency or network wide
  const globalAlerts = alerts.filter((a) =>
    a.informed_entities.length === 0 ||
    a.informed_entities.every((e) => !e.route_id && !e.stop_id && (e.agency_id != null))
  );

  // Set of route_ids with alerts
  const alertedRouteIds = new Set<string>(
    alerts.flatMap((a) => a.informed_entities.map((e) => e.route_id).filter(Boolean) as string[])
  );

  // Set of stop_ids with alerts
  const alertedStopIds = new Set<string>(
    alerts.flatMap((a) => a.informed_entities.map((e) => e.stop_id).filter(Boolean) as string[])
  );

  function alertsForRoute(routeId: string): Alert[] {
    return alerts.filter((a) =>
      a.informed_entities.some((e) => e.route_id === routeId)
    );
  }

  function alertsForStop(stopId: string): Alert[] {
    return alerts.filter((a) =>
      a.informed_entities.some((e) => e.stop_id === stopId)
    );
  }

  return { alerts, globalAlerts, alertedRouteIds, alertedStopIds, alertsForRoute, alertsForStop };
}
