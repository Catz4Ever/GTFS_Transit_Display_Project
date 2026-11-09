import { useEffect, useState } from "react";
import { useRouteStore } from "../stores/routeStore";
import { useAlerts } from "../hooks/useAlerts";
import { useVehicleStore } from "../stores/vehicleStore";
import { useLangStore } from "../hooks/useLanguage";

function routeLabel(route: { route_id: string; route_short_name?: string | null; route_long_name?: string | null }) {
  const shortName = route.route_short_name || route.route_id;
  return route.route_long_name ? `${shortName} — ${route.route_long_name}` : shortName;
}

export function RouteSelector() {
  const { t } = useLangStore();
  const routes = useRouteStore((s) => s.routes);
  const selectedRouteId = useRouteStore((s) => s.selectedRouteId);
  const setSelectedRouteId = useRouteStore((s) => s.setSelectedRouteId);
  const loading = useRouteStore((s) => s.loading);
  const [activeOnly, setActiveOnly] = useState(false);
  const { alertedRouteIds } = useAlerts();
  const displayMode = useVehicleStore((s) => s.displayMode);
  const isSimOnly = displayMode === "simulated";

  // Derive active route IDs from live vehicle store — always up to date
  const activeRouteIds = useVehicleStore((s) => s.activeRouteIds);

  // Reset to all lines and clear live filter when mode changes
  useEffect(() => {
    setSelectedRouteId(null);
    setActiveOnly(false);
  }, [displayMode]);

  const visibleRoutes = activeOnly
    ? routes.filter((r) => activeRouteIds.has(r.route_id))
    : routes;

  return (
    <label className="control">
      <span>{t.lineLabel}</span>
      <select
        value={selectedRouteId ?? ""}
        disabled={loading}
        onChange={(event) => setSelectedRouteId(event.target.value || null)}
      >
        <option value="">{t.allLinesOption}</option>
        {visibleRoutes.map((route) => (
          <option key={route.route_id} value={route.route_id}>
            {alertedRouteIds.has(route.route_id) ? "⚠ " : ""}{routeLabel(route)}
          </option>
        ))}
      </select>
      <button
        title={isSimOnly ? t.liveFilterTitleSim : activeOnly ? t.showAllLines : t.liveFilterTitle}
        onClick={() => {
          if (isSimOnly) return;
          const next = !activeOnly;
          setActiveOnly(next);
          // If switching to live filter and the selected route has no active vehicles, clear it
          if (next && selectedRouteId && !activeRouteIds.has(selectedRouteId)) {
            setSelectedRouteId(null);
          }
        }}
        disabled={isSimOnly}
        style={{
          marginLeft: 4,
          background: activeOnly && !isSimOnly ? "var(--accent-primary)" : "transparent",
          border: "1px solid var(--border-subtle)",
          borderRadius: 4,
          color: isSimOnly ? "var(--text-muted)" : activeOnly ? "#fff" : "var(--text-secondary)",
          cursor: isSimOnly ? "not-allowed" : "pointer",
          fontSize: 12,
          padding: "2px 7px",
          lineHeight: 1.5,
          opacity: isSimOnly ? 0.5 : 1,
        }}
      >
        live
      </button>
    </label>
  );
}
