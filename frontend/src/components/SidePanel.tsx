import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useStopStore, type StopArrival } from "../stores/stopStore";
import { useAlerts } from "../hooks/useAlerts";
import { useLangStore } from "../hooks/useLanguage";
import { useTheme } from "../hooks/useTheme";
import { toggleFavourite, isFavourite } from "./MapControls";
import { useVehicleStore } from "../stores/vehicleStore";
import { useRouteStore } from "../stores/routeStore";
import { useCityStore } from "../stores/cityStore";
import { fetchStopRoutes, type StopRoute } from "../api/referenceData";

function arrivalMinutes(arrival: StopArrival) {
  if (arrival.minutes_until_arrival !== undefined) {
    const n = Number(arrival.minutes_until_arrival);
    return Number.isFinite(n) ? n : null;
  }

  if (arrival.eta_seconds !== undefined && arrival.eta_seconds !== null) {
    const n = Number(arrival.eta_seconds);
    return Number.isFinite(n) ? n / 60 : null;
  }

  if (arrival.seconds_until_arrival !== undefined) {
    const n = Number(arrival.seconds_until_arrival);
    return Number.isFinite(n) ? n / 60 : null;
  }

  return null;
}

function formatArrivalMinutes(arrival: StopArrival, t: { now: string; minAbbr: string }) {
  const minutes = arrivalMinutes(arrival);
  if (minutes === null) return arrival.predicted_time ?? "n/a";
  if (minutes <= 0.5) return t.now;
  return `${Math.round(minutes)} ${t.minAbbr}`;
}

function routeDisplay(arrival: StopArrival) {
  return arrival.route_name || arrival.route_id || "?";
}

function timeDisplay(arrival: StopArrival) {
  return arrival.predicted_time || arrival.scheduled_time || arrival.arrival_time || "";
}


function dedupeArrivals(arrivals: StopArrival[]) {
  const seen = new Set<string>();
  const result: StopArrival[] = [];

  for (const arrival of arrivals) {
    const key = [
      arrival.trip_instance_id ?? arrival.vehicle_id ?? "",
      arrival.route_id ?? "",
      arrival.stop_sequence ?? "",
      arrival.predicted_time ?? arrival.scheduled_time ?? "",
    ].join("|");

    if (seen.has(key)) continue;
    seen.add(key);
    result.push(arrival);
  }

  return result;
}


function getRouteTypeLabels(t: { tram: string; metro: string; bus: string; trolleybus: string }): Record<number, string> {
  return {
    0: t.tram,
    1: t.metro,
    2: "Rail",
    3: t.bus,
    4: "Ferry",
    5: "Cable Car",
    6: "Gondola",
    7: "Funicular",
    11: t.trolleybus,
    12: "Monorail",
  };
}

function LegendPanel() {
  const { t } = useLangStore();
  const routes = useRouteStore((s) => s.routes);
  const selectedRouteType = useVehicleStore((s) => s.selectedRouteType);
  const setSelectedRouteType = useVehicleStore((s) => s.setSelectedRouteType);

  const typeColors = useMemo(() => {
    // Count color occurrences per route type
    const typeColorCounts = new Map<number, Map<string, number>>();
    for (const route of routes) {
      const rt = route.route_type;
      if (rt === undefined || rt === null) continue;
      const color = route.route_color
        ? `#${route.route_color.replace(/^#/, "")}`
        : "#888888";
      if (!typeColorCounts.has(rt)) typeColorCounts.set(rt, new Map());
      const counts = typeColorCounts.get(rt)!;
      counts.set(color, (counts.get(color) ?? 0) + 1);
    }
    // Pick most common color per type
    return Array.from(typeColorCounts.entries())
      .sort(([a], [b]) => a - b)
      .map(([type, counts]) => {
        const color = Array.from(counts.entries())
          .sort(([, a], [, b]) => b - a)[0][0];
        const label = getRouteTypeLabels(t)[type] ?? `Type ${type}`;
        return { color, label, type };
      });
  }, [routes, t]);

  // Only show when city has routes loaded
  if (typeColors.length === 0) return null;

  return (
    <div className="legend">
      <div className="legend-title">{t.vehicleTypes}</div>
      {typeColors.map(({ color, label, type }) => {
        const active = selectedRouteType === type;
        return (
          <div
            className={`legend-row${active ? " legend-row--active" : ""}`}
            key={label}
            style={{ cursor: "pointer", opacity: selectedRouteType !== null && !active ? 0.4 : 1 }}
            onClick={() => setSelectedRouteType(active ? null : type)}
            title={active ? "Click to show all types" : `Click to show only ${label}`}
          >
            <span className="legend-dot" style={{ background: color, outline: active ? "2px solid white" : "none", outlineOffset: "1px" }} />
            {label}
          </div>
        );
      })}
    </div>
  );
}

function routeColor(route: StopRoute): string {
  return route.route_color ? `#${route.route_color.replace(/^#/, "")}` : "#888888";
}

function routeTextColor(route: StopRoute): string {
  return route.route_text_color ? `#${route.route_text_color.replace(/^#/, "")}` : "#ffffff";
}

function StopRoutesPanel({ cityCode, stopId }: { cityCode: string; stopId: string }) {
  const { t } = useLangStore();
  const setSelectedRouteId = useRouteStore((s) => s.setSelectedRouteId);
  const selectedRouteId = useRouteStore((s) => s.selectedRouteId);

  const { data: routes, isLoading } = useQuery({
    queryKey: ["stop-routes", cityCode, stopId],
    queryFn: () => fetchStopRoutes(cityCode, stopId),
    staleTime: 5 * 60 * 1000,
    enabled: !!cityCode && !!stopId,
  });

  if (isLoading) return <div className="stop-routes-loading">Loading routes…</div>;
  if (!routes || routes.length === 0) return null;

  return (
    <div className="stop-routes">
      <div className="stop-routes__title">{t.routesAtStop}</div>
      <div className="stop-routes__list">
        {routes.map((route) => {
          const active = selectedRouteId === route.route_id;
          return (
            <button
              key={route.route_id}
              className={`stop-routes__badge${active ? " stop-routes__badge--active" : ""}`}
              style={{
                background: routeColor(route),
                color: routeTextColor(route),
                outline: active ? "2px solid white" : "none",
                outlineOffset: "2px",
              }}
              title={route.route_long_name ?? route.route_short_name ?? route.route_id}
              onClick={() => setSelectedRouteId(active ? null : route.route_id)}
            >
              {route.route_short_name ?? route.route_id}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function FavStopButton({ cityCode, stopId, stopName }: { cityCode: string; stopId: string; stopName: string }) {
  const [faved, setFaved] = useState(() => isFavourite(`stop-${cityCode}-${stopId}`));

  // Reset when stop changes
  useEffect(() => {
    setFaved(isFavourite(`stop-${cityCode}-${stopId}`));
  }, [cityCode, stopId]);
  return (
    <button
      className={`fav-stop-btn${faved ? " faved" : ""}`}
      title={faved ? "Remove from favourites" : "Add to favourites"}
      onClick={() => {
        const next = toggleFavourite({ id: `stop-${cityCode}-${stopId}`, type: "stop", cityCode, label: stopName, stopId });
        setFaved(next);
      }}
    >
      {faved ? "★" : "☆"}
    </button>
  );
}


export function SidePanel() {
  const { t, lang } = useLangStore();
  const { theme } = useTheme();
  const selectedStopId = useStopStore((s) => s.selectedStopId);
  const setSelectedStopId = useStopStore((s) => s.setSelectedStopId);
  const selectedStop = useStopStore((s) => s.stops.find((stop) => stop.stop_id === s.selectedStopId));
  const arrivalsRaw = useStopStore((s) => s.arrivals);
  const arrivalsUpdatedAt = useStopStore((s) => s.arrivalsUpdatedAt);
  const vehicles = useVehicleStore((s) => s.vehicles);
  const setSelectedRouteId = useRouteStore((s) => s.setSelectedRouteId);
  const selectedRouteId = useRouteStore((s) => s.selectedRouteId);
  const { alertsForStop, alertsForRoute } = useAlerts();
  const selectedCityCode = useCityStore((s) => s.selectedCityCode);

  function handleArrivalClick(arrival: StopArrival) {
    const routeId = arrival.route_id ?? null;
    const tripInstanceId = arrival.trip_instance_id ?? arrival.vehicle_id ?? null;

    // Find the matching vehicle in the store
    const vehicle = vehicles.find(
      (v) => v.trip_instance_id === tripInstanceId || v.vehicle_id === tripInstanceId || v.id === tripInstanceId
    );

    if (routeId && routeId !== selectedRouteId) {
      setSelectedRouteId(routeId);
    }

    if (vehicle) {
      // Dispatch a custom event that TransitMap listens for to select and fly to a vehicle
      window.dispatchEvent(new CustomEvent("selectVehicleFromPanel", { detail: { vehicleId: vehicle.id } }));
    }
  }

  const arrivals = dedupeArrivals(arrivalsRaw);

  return (
    <aside className="side-panel">
      <div className="side-panel-header">
        <div>
          <strong
            style={selectedStop ? { cursor: "pointer", textDecoration: "underline dotted" } : undefined}
            title={selectedStop ? "Click to locate on map" : undefined}
            onClick={() => {
              if (selectedStop) window.dispatchEvent(new CustomEvent("flyToStop", { detail: { stopId: selectedStop.stop_id } }));
            }}
          >{selectedStop ? selectedStop.stop_name : selectedStopId ? `Stop ${selectedStopId}` : t.noStopSelected}</strong>
          <span>{!selectedStop && (selectedStopId ? selectedStopId : t.selectAStop)}</span>
          <span>{arrivalsUpdatedAt ? `${t.updated} ${new Date(arrivalsUpdatedAt).toLocaleTimeString()}` : t.notSubscribed}</span>
          {selectedStopId && selectedCityCode && (
            <a
              href={`/?display=stop&city=${encodeURIComponent(selectedCityCode)}&stop=${encodeURIComponent(selectedStopId)}&lang=${lang}&theme=${theme}`}
              target="_blank"
              rel="noopener noreferrer"
              title="Open stop display board in new tab"
              style={{ fontSize: 11, color: "#60a5fa", textDecoration: "none", marginTop: 2 }}
            >
              {t.board} ↗
            </a>
          )}
          {selectedStopId && selectedCityCode && <FavStopButton cityCode={selectedCityCode} stopId={selectedStopId} stopName={selectedStop?.stop_name ?? selectedStopId} />}
        </div>

        {selectedStopId && <button onClick={() => setSelectedStopId(null)}>{t.clear}</button>}
      </div>

      {selectedStopId && alertsForStop(selectedStopId).map((alert) => (
        <div key={alert.entity_id} className="stop-alert">
          <span className="stop-alert__icon">⚠</span>
          <span className="stop-alert__effect">{alert.effect ?? t.alert}</span>
          {alert.header && <span className="stop-alert__header">{alert.header}</span>}
        </div>
      ))}

      {!selectedStopId && <p className="empty">{t.searchStopHint}</p>}

      {selectedStopId && arrivals.length === 0 && (
        <p className="empty">
          {t.noLiveVehicles}
        </p>
      )}

      <div className="arrival-list">
        {arrivals.slice(0, 20).map((arrival, index) => (
          <div className="arrival-row" key={`${arrival.trip_instance_id ?? "vehicle"}-${arrival.route_id ?? "route"}-${index}`}>
            <strong
              style={{ cursor: "pointer" }}
              title="Click to find this vehicle on the map"
              onClick={() => handleArrivalClick(arrival)}
            >{routeDisplay(arrival)}{(() => { const ra = alertsForRoute(arrival.route_id ?? ""); return ra.length > 0 ? <span title={ra.map(a => a.header ?? a.effect ?? t.alert).join(" | ")} style={{color:"#ff9444",marginLeft:3}}>⚠</span> : null; })()}</strong>
            <span>
              {timeDisplay(arrival)}
            </span>
            <b>{formatArrivalMinutes(arrival, t)}</b>
          </div>
        ))}
      </div>

      {selectedStopId && selectedCityCode && (
        <StopRoutesPanel cityCode={selectedCityCode} stopId={selectedStopId} />
      )}

      <LegendPanel />
    </aside>
  );
}
