import { useEffect, useMemo, useRef, useState } from "react";
import maplibregl, { type GeoJSONSource, type Map as MapLibreMap, type MapLayerMouseEvent } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useQueryClient } from "@tanstack/react-query";
import { fetchTripStops } from "../api/referenceData";
import { config } from "../config/runtime";
import { useVehicleStore } from "../stores/vehicleStore";
import { useCityStore, useSelectedCity } from "../stores/cityStore";
import { useRouteStore } from "../stores/routeStore";
import { useStopStore } from "../stores/stopStore";
import type { RouteStop, Vehicle } from "../types/transit";
import { useRouteGeometry } from "./hooks/useRouteGeometry";
import { notNull, useRouteGeojson, useRouteLabels, useStopGeojson, useVehicleGeojson, vehicleId } from "./hooks/useMapSources";
import { useMapDebug, type LogLine } from "./hooks/useMapDebug";
import { useAlerts } from "../hooks/useAlerts";
import { useTheme } from "../hooks/useTheme";
import { useLangStore } from "../hooks/useLanguage";
import { GpsControl, FavouritesControl, SatelliteControl } from "../components/MapControls";

const VEHICLE_SOURCE_ID = "vehicles";
const VEHICLE_DOT_LAYER_ID = "vehicles-dot";
const VEHICLE_LABEL_LAYER_ID = "vehicles-label";
const STOPS_SOURCE_ID = "stops";
const SELECTED_STOP_FLAG_SOURCE_ID = "selected-stop-flag";
const SELECTED_STOP_FLAG_LAYER_ID = "selected-stop-flag-layer";
const STOPS_LAYER_ID = "stops-circle";
const STOPS_LABEL_LAYER_ID = "stops-label";
const ROUTE_SOURCE_ID = "selected-route";
const ROUTE_HALO_LAYER_ID = "selected-route-halo";
const ROUTE_LAYER_ID = "selected-route-line";
const TYPE_ROUTES_SOURCE_ID = "type-routes";
const TYPE_ROUTES_LAYER_ID = "type-routes-line";

function vehicleLabel(vehicle: Partial<Vehicle>, routeLabel?: string | null) {
  return String(vehicle.route_short_name || routeLabel || vehicle.route_id || vehicle.vehicle_label || vehicle.vehicle_id || "?");
}

function vehicleDisplayName(vehicle: Partial<Vehicle>) {
  return String(vehicle.vehicle_label || vehicle.vehicle_id || "n/a");
}

const IS_DEBUG = new URLSearchParams(window.location.search).has("debug");

function debugTripLabel(vehicle: Partial<Vehicle>) {
  if (!IS_DEBUG) return null;
  return vehicle.trip_id || vehicle.trip_instance_id || vehicle.id || null;
}

function sourceLabel(source?: string | null) {
  if (!source) return "unknown";
  if (source === "gtfs_rt_vehicle_position" || source.includes("gtfs")) return "GTFS-RT";
  if (source === "simulated") return "Scheduled";
  if (source === "fused") return "GTFS-RT";
  return source;
}

function normalizeTime(value?: string | null) {
  if (!value) return "—";
  const [h = "00", m = "00"] = value.split(":");
  return `${String(Number(h) % 24).padStart(2, "0")}:${m}`;
}

function etaTextFromSeconds(value?: number | null) {
  if (value === null || value === undefined) return null;

  const etaSeconds = Number(value);
  if (!Number.isFinite(etaSeconds)) return null;
  if (etaSeconds < -90) return null;
  const tr2 = useLangStore.getState().t;
  if (etaSeconds < 60) return tr2.now;

  return `${Math.round(etaSeconds / 60)} ${tr2.minAbbr}`;
}

function secondsFromGtfsTime(value?: string | null) {
  if (!value) return null;
  const [rawHours, rawMinutes = "0", rawSeconds = "0"] = value.split(":");
  const hours = Number(rawHours);
  const minutes = Number(rawMinutes);
  const seconds = Number(rawSeconds);
  if (![hours, minutes, seconds].every(Number.isFinite)) return null;
  return hours * 3600 + minutes * 60 + seconds;
}

function currentServiceSeconds(timeZone?: string | null) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: timeZone || "Europe/Sofia",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).formatToParts(new Date());

  const value = (type: string) => Number(parts.find((part) => part.type === type)?.value ?? 0);
  return value("hour") * 3600 + value("minute") * 60 + value("second");
}

function etaTextForStop(stop: RouteStop, cityNowSeconds: number) {
  const directEta = etaTextFromSeconds(stop.eta_seconds);
  if (directEta) return directEta;

  const targetSeconds = secondsFromGtfsTime(stop.arrival_time ?? stop.departure_time);
  if (targetSeconds === null) return null;

  // GTFS post-midnight times are stored as 24:xx, 25:xx etc.
  // Normalise to wall-clock seconds within the current day.
  const normalizedTarget = targetSeconds >= 24 * 3600 ? targetSeconds - 24 * 3600 : targetSeconds;

  let etaSeconds = normalizedTarget - cityNowSeconds;
  if (etaSeconds < -6 * 3600) etaSeconds += 24 * 3600;

  return etaTextFromSeconds(etaSeconds);
}

function distanceSquared(a: [number, number], b: [number, number]) {
  const dx = a[0] - b[0];
  const dy = a[1] - b[1];
  return dx * dx + dy * dy;
}

function upcomingFromRouteStops(stops: RouteStop[], vehicle: Vehicle): RouteStop[] {
  if (stops.length === 0) return [];

  // Prefer current_stop_sequence from the vehicle — it's authoritative.
  const seq = vehicle.current_stop_sequence;
  if (seq != null && Number.isFinite(Number(seq))) {
    const seqNum = Number(seq);
    // Find the first stop at or after this sequence number.
    const seqIndex = stops.findIndex((s) => s.stop_sequence != null && Number(s.stop_sequence) >= seqNum);
    if (seqIndex >= 0) return stops.slice(seqIndex, seqIndex + 8);
  }

  // Fallback: closest stop by distance.
  const lon = Number(vehicle.lon ?? vehicle.longitude);
  const lat = Number(vehicle.lat ?? vehicle.latitude);
  if (!Number.isFinite(lon) || !Number.isFinite(lat)) return stops.slice(0, 8);

  let bestIndex = 0;
  let bestDistance = Number.POSITIVE_INFINITY;
  stops.forEach((stop, index) => {
    const d = distanceSquared([lon, lat], [Number(stop.lon), Number(stop.lat)]);
    if (d < bestDistance) {
      bestDistance = d;
      bestIndex = index;
    }
  });
  return stops.slice(bestIndex, bestIndex + 8);
}



function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function escapeAttr(value: unknown): string {
  return escapeHtml(value);
}

// Returns readable black or white depending on background color luminance
function contrastColor(hex: string | null | undefined): string {
  if (!hex) return "#ffffff";
  const h = hex.replace(/^#/, "");
  if (h.length < 6) return "#ffffff";
  const r = parseInt(h.slice(0, 2), 16) / 255;
  const g = parseInt(h.slice(2, 4), 16) / 255;
  const b = parseInt(h.slice(4, 6), 16) / 255;
  // Relative luminance (WCAG)
  const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  return lum > 0.45 ? "#111111" : "#ffffff";
}

function routeTypeLabel(vehicle: Partial<Vehicle>): string {
  const rt = vehicle.route_type;
  const tr = useLangStore.getState().t;
  if (rt === 0) return tr.tram;
  if (rt === 1) return tr.metro;
  if (rt === 11) return tr.trolleybus;
  if (rt === 3) return tr.bus;
  return "";
}

function popupHtml(
  vehicle: Vehicle,
  stops: Array<RouteStop>,
  scheduleKind: string,
  routeLabel?: string | null,
  cityTimeZone?: string | null,
  routeAlerts?: Array<{ effect: string | null; header: string | null }>,
) {
  const tr = useLangStore.getState().t;
  const cityNowSeconds = currentServiceSeconds(cityTimeZone);
  const rows = stops
    .map((stop) => {
      const etaText = etaTextForStop(stop, cityNowSeconds);

      // Skip stops with no ETA and no arrival time — nothing useful to show.
      if (!etaText && !stop.arrival_time && !stop.departure_time) return null;
      // Skip stops whose scheduled time has already passed (etaText null means too far in past).
      if (!etaText && (stop.arrival_time || stop.departure_time)) return null;

      const stopId = String(stop.stop_id ?? "");
      const clickableAttrs = stopId
        ? ` data-popup-stop-id="${escapeAttr(stopId)}" role="button" tabindex="0" title="Select this stop" style="cursor:pointer"`
        : "";

      return `<div class="vehicle-popup__stop-row"${clickableAttrs}><span class="vehicle-popup__stop-name">${escapeHtml(stop.stop_name ?? tr.stopFallback)}</span><span class="vehicle-popup__stop-time">${escapeHtml(normalizeTime(stop.arrival_time))}</span><span class="vehicle-popup__eta">${escapeHtml(etaText)}</span></div>`;
    })
    .filter(notNull)
    .slice(0, 8)
    .join("");

  const alertRows = (routeAlerts ?? []).map((a) =>
    `<div class="vehicle-popup__alert" title="${escapeAttr(a.header ?? "")}"><span class="vehicle-popup__alert-icon">⚠</span><span class="vehicle-popup__alert-effect">${escapeHtml(a.effect ?? tr.alert)}</span>${a.header ? `<span class="vehicle-popup__alert-header">${escapeHtml(a.header.length > 80 ? a.header.slice(0, 80) + "…" : a.header)}</span>` : ""}</div>`
  ).join("");

  return `
    <div class="vehicle-popup">
      <div class="vehicle-popup__header">
        <span class="vehicle-popup__badge" style="background:${vehicle.route_color ? `#${vehicle.route_color.replace(/^#/,"")}` : "#c33149"};color:${contrastColor(vehicle.route_color ? `#${vehicle.route_color.replace(/^#/,"")}` : null)}">${escapeHtml(vehicleLabel(vehicle, routeLabel))}</span>
        <span class="vehicle-popup__source">${IS_DEBUG ? escapeHtml(sourceLabel(vehicle.source)) + (routeTypeLabel(vehicle) ? ` &mdash; ${escapeHtml(routeTypeLabel(vehicle))}` : "") : escapeHtml(routeTypeLabel(vehicle))}</span>
        <span class="vehicle-popup__controls">
          <button class="vehicle-popup__minimize" title="Minimize" aria-label="Minimize">&#8722;</button>
          <button class="vehicle-popup__close" title="Close" aria-label="Close">&#x2715;</button>
        </span>
      </div>
      <div class="vehicle-popup__meta">
        ${IS_DEBUG ? `${tr.vehicleLabel} ${vehicleDisplayName(vehicle)}<br/>` : ""}
        ${debugTripLabel(vehicle) ? `<span class="vehicle-popup__debug">${tr.tripLabel} ${debugTripLabel(vehicle)}</span><br/>` : ""}
        ${scheduleKind}
      </div>
      ${alertRows}
      ${rows || `<div class="vehicle-popup__empty">${scheduleKind.includes("No schedule") || scheduleKind.includes("Няма налична") ? tr.notReportingTrip : tr.noUpcomingStopsFound}</div>`}
    </div>`;
}


function DebugOverlay({ lines }: { lines: LogLine[] }) {
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const userScrolledRef = useRef(false);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (!userScrolledRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [lines]);

  return (
    <div
      ref={scrollRef}
      onScroll={(e) => {
        const el = e.currentTarget;
        userScrolledRef.current = el.scrollTop + el.clientHeight < el.scrollHeight - 20;
      }}
      style={{
        position: "fixed", bottom: 8, left: 8, zIndex: 9999,
        background: "rgba(0,0,0,0.88)", color: "#e0e0e0",
        fontFamily: "monospace", fontSize: 11, lineHeight: 1.5,
        padding: "6px 10px", borderRadius: 6, maxWidth: 560,
        maxHeight: 320, overflowY: "auto",
        border: "1px solid #444", userSelect: "text", cursor: "text",
      }}
    >
      {lines.map((l, i) => (
        <div key={i} style={{ color: l.kind === "error" ? "#ff6b6b" : l.kind === "warn" ? "#ffd93d" : l.kind === "ok" ? "#6bcb77" : "#e0e0e0", whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
          <span style={{ opacity: 0.5 }}>{l.t} </span>{l.msg}
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  );
}

export default function TransitMap() {
  const { log, lines, enabled: debugEnabled } = useMapDebug();
  const { alertsForRoute } = useAlerts();
  const { theme } = useTheme();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const popupRef = useRef<maplibregl.Popup | null>(null);
  const popupVehicleIdRef = useRef<string | null>(null);
  const suppressPopupCloseRef = useRef(false);
  const vehiclesRef = useRef<Vehicle[]>([]);
  const selectedRouteIdRef = useRef<string | null>(null);
  const routeChangedByVehicleClickRef = useRef(false);
  const queryClient = useQueryClient();

  const selectedCity = useSelectedCity();
  const cityTimeZone = selectedCity?.timezone_name ?? selectedCity?.timezone ?? "Europe/Sofia";
  const currentLang = useLangStore((s) => s.lang); // triggers re-render on language change
  void currentLang; // consumed to satisfy TS noUnusedLocals
  const vehicles = useVehicleStore((s) => s.vehicles);
  const setVehicles = useVehicleStore((s) => s.setVehicles);
  const displayMode = useVehicleStore((s) => s.displayMode);
  const selectedRouteType = useVehicleStore((s) => s.selectedRouteType);
  const setSelectedRouteType = useVehicleStore((s) => s.setSelectedRouteType);
  const selectedCityCode = useCityStore((s) => s.selectedCityCode);
  const selectedRouteId = useRouteStore((s) => s.selectedRouteId);
  const modeFiltered = displayMode === "all"
    ? vehicles
    : displayMode === "simulated"
    ? vehicles.filter((v) => v.source === "simulated")
    : vehicles.filter((v) => v.source !== "simulated");
  const typeFiltered = selectedRouteType !== null
    ? modeFiltered.filter((v) => v.route_type === selectedRouteType)
    : modeFiltered;
  const visibleVehicles = selectedRouteId
    ? typeFiltered.filter((v) => v.route_id === selectedRouteId || v.route_short_name === selectedRouteId)
    : typeFiltered;
  vehiclesRef.current = visibleVehicles;
  selectedRouteIdRef.current = selectedRouteId;
  const setSelectedRouteId = useRouteStore((s) => s.setSelectedRouteId);
  const routes = useRouteStore((s) => s.routes);
  const stops = useStopStore((s) => s.stops);
  const selectedStopId = useStopStore((s) => s.selectedStopId);
  const setSelectedStopId = useStopStore((s) => s.setSelectedStopId);

  const selectedRouteColor = useMemo(() => {
    if (!selectedRouteId) return null;
    const route = routes.find((r) => r.route_id === selectedRouteId || r.route_short_name === selectedRouteId);
    return route?.route_color ? `#${route.route_color.replace(/^#/, "")}` : null;
  }, [routes, selectedRouteId]);

  const routeGeometryQuery = useRouteGeometry(selectedCityCode, selectedRouteId);
  const routeGeometry = routeGeometryQuery.data ?? null;

  // Debug logging
  useEffect(() => {
    if (routeGeometryQuery.isFetching) log(`[geometry] fetching: ${selectedCityCode}/${selectedRouteId}`);
  }, [routeGeometryQuery.isFetching]);
  useEffect(() => {
    if (routeGeometry) log(`[geometry] loaded: ${routeGeometry.point_count} pts, ${routeGeometry.stop_count} stops`, "ok");
    else if (selectedRouteId) log(`[geometry] null (loading or failed) for ${selectedRouteId}`, "warn");
  }, [routeGeometry, selectedRouteId]);
  useEffect(() => {
    if (routeGeometryQuery.isError) log(`[geometry] ERROR: ${String(routeGeometryQuery.error)}`, "error");
  }, [routeGeometryQuery.isError]);

  const [selectedVehicleId, setSelectedVehicleId] = useState<string | null>(null);
  const [followVehicle, setFollowVehicle] = useState(false);
  const [popupStops, setPopupStops] = useState<Array<RouteStop>>([]);
  const [mapLoaded, setMapLoaded] = useState(false);
  const [popupScheduleKind, setPopupScheduleKind] = useState<"scheduledStopTimes"|"scheduledStopTimesTrip"|"noScheduleData"|"routeStopOrder">("scheduledStopTimes");

  const selectedVehicle = useMemo(
    () => visibleVehicles.find((v, index) => vehicleId(v, index) === selectedVehicleId) ?? null,
    [selectedVehicleId, visibleVehicles],
  );

  const labelForVehicleRoute = useRouteLabels(routes);
  const routeGeojson = useRouteGeojson(routeGeometry, selectedRouteId);
  const vehicleGeojson = useVehicleGeojson(visibleVehicles, selectedVehicleId, labelForVehicleRoute);
  const stopGeojson = useStopGeojson(stops, routeGeometry, selectedRouteId);

  function selectStopFromVehiclePopup(stopId: string) {
    setSelectedStopId(stopId);

    const stop = stops.find((candidate) => candidate.stop_id === stopId);
    const lon = Number(stop?.lon);
    const lat = Number(stop?.lat);
    const map = mapRef.current;

    if (map && Number.isFinite(lon) && Number.isFinite(lat)) {
      map.easeTo({
        center: [lon, lat],
        zoom: Math.max(map.getZoom(), 15),
        duration: 350,
        essential: true,
      });
    }
  }

  function bindPopupStopClicks(popup: maplibregl.Popup) {
    const popupElement = popup.getElement();
    if (!popupElement) return;

    // Minimize button — just close the popup
    const minimizeBtn = popupElement.querySelector<HTMLElement>(".vehicle-popup__minimize");
    if (minimizeBtn) {
      minimizeBtn.onclick = () => popup.remove();
    }

    // Close button — close popup and return to all lines
    const closeBtn = popupElement.querySelector<HTMLElement>(".vehicle-popup__close");
    if (closeBtn) {
      closeBtn.onclick = () => {
        setSelectedRouteId(null);
        popup.remove();
      };
    }

    popupElement.querySelectorAll<HTMLElement>("[data-popup-stop-id]").forEach((element) => {
      const stopId = element.dataset.popupStopId;
      if (!stopId) return;

      element.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        selectStopFromVehiclePopup(stopId);
      });

      element.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        event.stopPropagation();
        selectStopFromVehiclePopup(stopId);
      });
    });
  }

  function removePopupSilently() {
    suppressPopupCloseRef.current = true;
    popupRef.current?.remove();
    if (mapRef.current) {
      (mapRef.current as any).__selectedVehicleId = null;
      if ((mapRef.current as any).__idleRaf) {
        cancelAnimationFrame((mapRef.current as any).__idleRaf);
        (mapRef.current as any).__idleRaf = undefined;
      }
    }
    window.setTimeout(() => {
      suppressPopupCloseRef.current = false;
    }, 0);
  }

  async function showVehiclePopup(vehicle: Vehicle) {
    const map = mapRef.current;
    if (!map || !selectedCityCode) return;

    const lon = Number(vehicle.lon ?? vehicle.longitude);
    const lat = Number(vehicle.lat ?? vehicle.latitude);
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) return;

    const id = vehicleId(vehicle);
    popupVehicleIdRef.current = id;

    let upcomingStops: Array<RouteStop> = [];
    let scheduleKind: "scheduledStopTimes"|"scheduledStopTimesTrip"|"noScheduleData"|"routeStopOrder" = "scheduledStopTimes";

    const hasNoTripData = !vehicle.trip_id && !vehicle.trip_pk && !vehicle.route_id;

    if (hasNoTripData) {
      scheduleKind = "noScheduleData";
    } else if (vehicle.trip_id) {
      try {
        const tripStops = await queryClient.fetchQuery({
          queryKey: ["tripStops", selectedCityCode, vehicle.trip_id],
          queryFn: () => fetchTripStops(selectedCityCode, vehicle.trip_id!),
          staleTime: 30 * 1000,
        });
        upcomingStops = upcomingFromRouteStops(tripStops.stops ?? [], vehicle);
        scheduleKind = "scheduledStopTimesTrip";
      } catch (error) {
        console.warn("Could not load trip stops; falling back to route stops", error);
      }
    }

    if (popupVehicleIdRef.current !== id) return;

    if (!hasNoTripData && upcomingStops.length === 0) {
      upcomingStops = upcomingFromRouteStops(routeGeometry?.stops ?? [], vehicle);
      scheduleKind = "routeStopOrder";
    }

    setPopupStops(upcomingStops);
    setPopupScheduleKind(scheduleKind);

    removePopupSilently();

    const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, maxWidth: "360px", offset: {"top": [0, 20], "top-left": [0, 20], "top-right": [0, 20],"bottom": [0, -20], "bottom-left": [0, -20], "bottom-right": [0, -20],"left": [20, 0], "right": [-20, 0], "center": [0, 0]} })
      .setLngLat([lon, lat])
      .setHTML(popupHtml(vehicle, upcomingStops, useLangStore.getState().t[scheduleKind], labelForVehicleRoute(vehicle), cityTimeZone, alertsForRoute(vehicle.route_id ?? "")))
      .addTo(map);

    bindPopupStopClicks(popup);

    popup.on("close", () => {
      if (suppressPopupCloseRef.current) return;
      popupVehicleIdRef.current = null;
      setSelectedVehicleId(null);
      setFollowVehicle(false);
      setPopupStops([]);
    });

    popupRef.current = popup;
    if (mapRef.current) {
      (mapRef.current as any).__popupRef = popup;
      (mapRef.current as any).__selectedVehicleId = id;
      (mapRef.current as any).__popupAnimPos = [lon, lat];
    }
  }

  useEffect(() => {
    if (!popupRef.current || !selectedVehicle) return;

    // Refetch trip stops in background so eta_seconds stays fresh.
    // Only refetch if cache is stale (>30s old) to avoid re-render loops.
    if (selectedVehicle.trip_id && selectedCityCode) {
      const cached = queryClient.getQueryState(["tripStops", selectedCityCode, selectedVehicle.trip_id]);
      const isStale = !cached?.dataUpdatedAt || (Date.now() - cached.dataUpdatedAt) > 30 * 1000;
      if (isStale) {
        queryClient.fetchQuery({
          queryKey: ["tripStops", selectedCityCode, selectedVehicle.trip_id],
          queryFn: () => fetchTripStops(selectedCityCode, selectedVehicle.trip_id!),
          staleTime: 30 * 1000,
        }).then((tripStops) => {
          const fresh = upcomingFromRouteStops(tripStops.stops ?? [], selectedVehicle);
          if (fresh.length > 0) setPopupStops(fresh);
        }).catch(() => {});
      }
    }

    const tr = useLangStore.getState().t;
    popupRef.current.setHTML(
      popupHtml(selectedVehicle, popupStops, tr[popupScheduleKind], labelForVehicleRoute(selectedVehicle), cityTimeZone, alertsForRoute(selectedVehicle.route_id ?? "")),
    );
    requestAnimationFrame(() => {
      if (popupRef.current) bindPopupStopClicks(popupRef.current);
    });
  }, [selectedVehicle, popupStops, popupScheduleKind, stops, setSelectedStopId, currentLang]);

  useEffect(() => {
    if (!selectedVehicleId || selectedVehicle) return;
    setFollowVehicle(false);
  }, [selectedVehicle, selectedVehicleId]);

  // Handle vehicle selection triggered from the side panel
  useEffect(() => {
    function onSelectVehicleFromPanel(e: Event) {
      const { vehicleId: id } = (e as CustomEvent).detail;
      const vehicle = vehiclesRef.current.find((v, index) => vehicleId(v, index) === id || v.id === id);
      if (!vehicle) return;
      routeChangedByVehicleClickRef.current = true;
      setSelectedVehicleId(id);
    if (mapRef.current) (mapRef.current as any).__selectedVehicleId = id;
      setFollowVehicle(true);
      const lon = Number(vehicle.lon ?? vehicle.longitude);
      const lat = Number(vehicle.lat ?? vehicle.latitude);
      if (mapRef.current && Number.isFinite(lon) && Number.isFinite(lat)) {
        mapRef.current.flyTo({ center: [lon, lat], zoom: Math.max(mapRef.current.getZoom(), 15), duration: 700, essential: true });
      }
      showVehiclePopup(vehicle);
    }
    window.addEventListener("selectVehicleFromPanel", onSelectVehicleFromPanel);
    return () => window.removeEventListener("selectVehicleFromPanel", onSelectVehicleFromPanel);
  }, []);

  // Fly to stop when side panel stop name is clicked
  useEffect(() => {
    const onFlyToStop = (e: Event) => {
      const { stopId } = (e as CustomEvent).detail;
      const stop = stops.find((s) => s.stop_id === stopId);
      if (stop && mapRef.current) {
        mapRef.current.flyTo({ center: [stop.lon, stop.lat], zoom: Math.max(mapRef.current.getZoom(), 16), duration: 600 });
      }
    };
    window.addEventListener("flyToStop", onFlyToStop);
    return () => window.removeEventListener("flyToStop", onFlyToStop);
  }, [stops]);

  // When returning to the tab, snap popup to current rendered position
  // (RAF is paused in background tabs so popup drifts from the pill)
  useEffect(() => {
    const onVisibility = () => {
      if (document.hidden) return;
      const map = mapRef.current;
      if (!map) return;
      const pos = (map as any).__popupAnimPos as [number, number] | undefined;
      const selId = (map as any).__selectedVehicleId as string | undefined;
      if (pos && selId) {
        (map as any).__popupRef?.setLngLat(pos);
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  useEffect(() => {
    log(`[route] selectedRouteId changed → ${selectedRouteId ?? "null"}`);
    if (routeChangedByVehicleClickRef.current) {
      routeChangedByVehicleClickRef.current = false;
      return;
    }
    setSelectedVehicleId(null);
    setFollowVehicle(false);
    popupVehicleIdRef.current = null;
    removePopupSilently();
    setPopupStops([]);
  }, [selectedRouteId]);

  useEffect(() => {
    setSelectedVehicleId(null);
    setFollowVehicle(false);
    popupVehicleIdRef.current = null;
    removePopupSilently();
    setPopupStops([]);
  }, [displayMode]);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const initialCenter = selectedCity?.center ?? [0, 0];

    // Resolve CSS custom properties once per map init. Both the style object
    // (raster base layer) and the load handler (vector layers) need these.
    const cssStyle = getComputedStyle(document.documentElement);
    const cssVar = (name: string, fallback: string) =>
      cssStyle.getPropertyValue(name).trim() || fallback;

    const map = new maplibregl.Map({
      container: containerRef.current,
      center: initialCenter,
      zoom: selectedCity?.center ? 12 : 2,
      attributionControl: { compact: true },
      style: {
        version: 8,
        sources: {
          base: {
            type: "raster",
            tiles: [theme === "light"
              ? "https://basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}.png"
              : "https://basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}.png"],
            tileSize: 256,
            maxzoom: 19,
            attribution: "© OpenStreetMap contributors © CARTO",
          },
        },
        layers: [
          {
            id: "base",
            type: "raster",
            source: "base",
            paint: {
              "raster-opacity": parseFloat(cssVar("--map-raster-opacity", "0.78")),
              "raster-saturation": parseFloat(cssVar("--map-raster-saturation", "-0.45")),
              "raster-contrast": parseFloat(cssVar("--map-raster-contrast", "-0.08")),
            },
          },
        ],
      },
    });

    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");

    map.on("load", () => {
      // CSS variables were resolved above (cssVar helper). Build the color
      // palette used across all vector layer paint definitions.
      const C = {
        routeHalo:          cssVar("--map-route-halo",            "#100f0e"),
        routeLine:          cssVar("--map-route-line",            "#c33149"),
        routeStopFill:      cssVar("--map-route-stop-fill",       "#fff8ee"),
        normalStopFill:     cssVar("--map-normal-stop-fill",      "#1b1614"),
        routeStopStroke:    cssVar("--map-route-stop-stroke",     "#c33149"),
        normalStopStroke:   cssVar("--map-normal-stop-stroke",    "#c29979"),
        stopLabel:          cssVar("--map-stop-label",            "#f3d9b1"),
        textHalo:           cssVar("--map-text-halo",             "#100f0e"),
        vehicleFill:        cssVar("--map-vehicle-fill",          "#c33149"),
        vehicleSelStroke:   cssVar("--map-vehicle-selected-stroke","#a8c256"),
        vehicleStroke:      cssVar("--map-vehicle-stroke",        "#f3d9b1"),
        vehicleLabel:       cssVar("--map-vehicle-label",         "#fff8ee"),
        vehicleLabelHalo:   cssVar("--map-vehicle-label-halo",    "#c33149"),
      } as const;

      map.addSource(TYPE_ROUTES_SOURCE_ID, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({
        id: TYPE_ROUTES_LAYER_ID,
        type: "line",
        source: TYPE_ROUTES_SOURCE_ID,
        layout: { "line-join": "round", "line-cap": "round" },
        paint: { "line-color": ["get", "route_color"], "line-width": ["interpolate", ["linear"], ["zoom"], 9, 1.5, 15, 3], "line-opacity": 0.6 },
      });
      map.addSource(ROUTE_SOURCE_ID, { type: "geojson", data: { type: "FeatureCollection", features: [] } });

      map.addLayer({
        id: ROUTE_HALO_LAYER_ID,
        type: "line",
        source: ROUTE_SOURCE_ID,
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": C.routeHalo, "line-width": ["interpolate", ["linear"], ["zoom"], 9, 6, 15, 11], "line-opacity": 0.8 },
      });

      map.addLayer({
        id: ROUTE_LAYER_ID,
        type: "line",
        source: ROUTE_SOURCE_ID,
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": C.routeLine, "line-width": ["interpolate", ["linear"], ["zoom"], 9, 3.5, 15, 6.5], "line-opacity": 0.95 },
      });

      map.addSource(STOPS_SOURCE_ID, { type: "geojson", data: { type: "FeatureCollection", features: [] } });

      map.addLayer({
        id: STOPS_LAYER_ID,
        type: "circle",
        source: STOPS_SOURCE_ID,
        minzoom: 13.25,
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 13, 3.2, 16, 7],
          "circle-color": ["case", ["boolean", ["get", "route_stop"], false], C.routeStopFill, C.normalStopFill],
          "circle-stroke-width": ["case", ["boolean", ["get", "route_stop"], false], 2.5, 1.2],
          "circle-stroke-color": ["case", ["boolean", ["get", "route_stop"], false], C.routeStopStroke, C.normalStopStroke],
          "circle-opacity": ["interpolate", ["linear"], ["zoom"], 13, 0, 14, 0.92],
        },
      });

      map.addLayer({
        id: STOPS_LABEL_LAYER_ID,
        type: "symbol",
        source: STOPS_SOURCE_ID,
        minzoom: 15,
        layout: {
          "text-field": IS_DEBUG
            ? ["concat", ["get", "stop_name"], ["case", ["!=", ["get", "stop_code"], ""], ["concat", " (", ["get", "stop_code"], ")"], ""]]
            : ["get", "stop_name"],
          "text-size": 11,
          "text-offset": [0, 1.1],
          "text-anchor": "top",
          "text-optional": true,
        },
        paint: { "text-color": C.stopLabel, "text-halo-color": C.textHalo, "text-halo-width": 1.5 },
      });

      map.addSource(SELECTED_STOP_FLAG_SOURCE_ID, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      // Draw a red flag pin icon onto a canvas and register it as a map image
      {
        const SZ = 32;
        const canvas = document.createElement("canvas");
        canvas.width = SZ; canvas.height = SZ;
        const ctx2d = canvas.getContext("2d")!;
        // Stem: thin vertical line from bottom-center up
        ctx2d.strokeStyle = "#c62828";
        ctx2d.lineWidth = 2.5;
        ctx2d.beginPath();
        ctx2d.moveTo(SZ / 2, SZ - 2);
        ctx2d.lineTo(SZ / 2, 4);
        ctx2d.stroke();
        // Flag: filled red triangle to the right of the stem top
        ctx2d.fillStyle = "#e53935";
        ctx2d.beginPath();
        ctx2d.moveTo(SZ / 2, 4);
        ctx2d.lineTo(SZ - 4, 10);
        ctx2d.lineTo(SZ / 2, 16);
        ctx2d.closePath();
        ctx2d.fill();
        // White outline on flag for contrast
        ctx2d.strokeStyle = "#fff";
        ctx2d.lineWidth = 0.8;
        ctx2d.stroke();
        const imgData = ctx2d.getImageData(0, 0, SZ, SZ);
        map.addImage("selected-stop-flag-icon", { width: SZ, height: SZ, data: new Uint8Array(imgData.data) });
      }
      map.addLayer({
        id: SELECTED_STOP_FLAG_LAYER_ID,
        type: "symbol",
        source: SELECTED_STOP_FLAG_SOURCE_ID,
        layout: {
          "icon-image": "selected-stop-flag-icon",
          "icon-size": 1,
          "icon-anchor": "bottom",
          "icon-offset": [1, -7],
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
        },
      });

      map.addSource(VEHICLE_SOURCE_ID, { type: "geojson", data: { type: "FeatureCollection", features: [] } });

      // Low zoom: small circle dots
      map.addLayer({
        id: VEHICLE_LABEL_LAYER_ID,
        type: "circle",
        source: VEHICLE_SOURCE_ID,
        minzoom: 7,
        maxzoom: 13,
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 7, 3, 10, 5],
          "circle-color": ["coalesce", ["get", "route_color"], C.vehicleFill],
          "circle-stroke-width": 1.5,
          "circle-stroke-color": "#ffffff",
          "circle-opacity": 0.95,
        },
      });

      // High zoom: canvas-drawn badge icons (generated per unique color+label in useMapSources)
      map.addLayer({
        id: VEHICLE_DOT_LAYER_ID,
        type: "symbol",
        source: VEHICLE_SOURCE_ID,
        minzoom: 13,
        layout: {
          "icon-image": ["get", "iconKey"],
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-size": ["interpolate", ["linear"], ["zoom"], 13, 0.55, 15, 0.7, 17, 0.85, 19, 1.0],
        },
        paint: {
          "icon-opacity": 1,
        },
      });


      map.on("click", STOPS_LAYER_ID, (event) => {
        const feature = event.features?.[0];
        const stopId = feature?.properties?.stop_id;
        if (stopId) setSelectedStopId(String(stopId));
      });

      const onVehicleClick = async (event: MapLayerMouseEvent) => {
        const clickPoint = event.point;
        const bbox: [maplibregl.PointLike, maplibregl.PointLike] = [
          [clickPoint.x - 12, clickPoint.y - 12],
          [clickPoint.x + 12, clickPoint.y + 12],
        ];
        const features = map.queryRenderedFeatures(bbox, {
          layers: [VEHICLE_DOT_LAYER_ID, VEHICLE_LABEL_LAYER_ID],
        });
        if (!features.length) return;
        let best = features[0];
        let bestDist = Infinity;
        for (const f of features) {
          if (f.geometry.type !== "Point") continue;
          const [fx, fy] = f.geometry.coordinates;
          const projected = map.project([fx, fy]);
          const dist = Math.hypot(projected.x - clickPoint.x, projected.y - clickPoint.y);
          if (dist < bestDist) { bestDist = dist; best = f; }
        }
        const props = best.properties ?? {};
        const id = String(props.id ?? "");
        const vehicle = vehiclesRef.current.find((v, index) => vehicleId(v, index) === id);
        if (!vehicle) return;

        setSelectedVehicleId(id);
    if (mapRef.current) (mapRef.current as any).__selectedVehicleId = id;
        setFollowVehicle(true);
        if (vehicle.route_id && vehicle.route_id !== selectedRouteIdRef.current) {
          routeChangedByVehicleClickRef.current = true;
          setVehicles([vehicle], null);
          setSelectedRouteId(vehicle.route_id);
        }

        const lon = Number(vehicle.lon ?? vehicle.longitude);
        const lat = Number(vehicle.lat ?? vehicle.latitude);
        if (Number.isFinite(lon) && Number.isFinite(lat)) {
          map.flyTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), 15), duration: 700, essential: true });
        }

        await showVehiclePopup(vehicle);
      };

      map.on("click", VEHICLE_DOT_LAYER_ID, onVehicleClick);
      map.on("click", VEHICLE_LABEL_LAYER_ID, onVehicleClick);

      for (const layerId of [STOPS_LAYER_ID, VEHICLE_DOT_LAYER_ID, VEHICLE_LABEL_LAYER_ID]) {
        map.on("mouseenter", layerId, () => {
          map.getCanvas().style.cursor = "pointer";
        });
        map.on("mouseleave", layerId, () => {
          map.getCanvas().style.cursor = "";
        });
      }

      map.on("dragstart", () => setFollowVehicle(false));
      setMapLoaded(true);
    });

    mapRef.current = map;

    return () => {
      removePopupSilently();
      setMapLoaded(false);
      setSelectedRouteType(null);
      map.remove();
      mapRef.current = null;
    };
  }, [selectedCity?.center, setSelectedRouteId, setSelectedStopId]);

  // Swap tile layer when theme changes
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;
    const tileUrl = theme === "light"
      ? "https://basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}.png"
      : "https://basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}.png";
    try {
      const source = map.getSource("base") as maplibregl.RasterTileSource | undefined;
      if (source) source.setTiles([tileUrl]);
      const cssStyle = getComputedStyle(document.documentElement);
      const cssVar = (name: string, fallback: string) => cssStyle.getPropertyValue(name).trim() || fallback;
      map.setPaintProperty("base", "raster-opacity", parseFloat(cssVar("--map-raster-opacity", "0.78")));
      map.setPaintProperty("base", "raster-saturation", parseFloat(cssVar("--map-raster-saturation", "-0.45")));
      map.setPaintProperty("base", "raster-contrast", parseFloat(cssVar("--map-raster-contrast", "-0.08")));
    } catch {}
  }, [theme]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !selectedCity || selectedVehicleId || selectedRouteId) return;

    const bounds = selectedCity.bounds;
    if (bounds) map.fitBounds(bounds, { padding: 40, maxZoom: 12, duration: 600 });
    else if (selectedCity.center) map.flyTo({ center: selectedCity.center, zoom: 12, duration: 600 });
  }, [selectedCity, selectedRouteId, selectedVehicleId]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !selectedStopId) return;

    const selectedStop = stops.find((stop) => stop.stop_id === selectedStopId);
    if (!selectedStop) return;

    map.flyTo({ center: [selectedStop.lon, selectedStop.lat], zoom: Math.max(map.getZoom(), 15), duration: 600 });
  }, [selectedStopId, stops]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapLoaded) return;
    const source = map.getSource(SELECTED_STOP_FLAG_SOURCE_ID) as GeoJSONSource | undefined;
    if (!source) return;
    if (!selectedStopId) {
      source.setData({ type: "FeatureCollection", features: [] });
      return;
    }
    const stop = stops.find((s) => s.stop_id === selectedStopId);
    if (!stop || !Number.isFinite(stop.lon) || !Number.isFinite(stop.lat)) {
      source.setData({ type: "FeatureCollection", features: [] });
      return;
    }
    source.setData({
      type: "FeatureCollection",
      features: [{ type: "Feature", geometry: { type: "Point", coordinates: [stop.lon, stop.lat] }, properties: {} }],
    });
  }, [selectedStopId, stops, mapLoaded]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !selectedVehicle || !followVehicle) return;

    const lon = Number(selectedVehicle.lon ?? selectedVehicle.longitude);
    const lat = Number(selectedVehicle.lat ?? selectedVehicle.latitude);
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) return;

    map.easeTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), 15), duration: 450, essential: true });
  }, [followVehicle, selectedVehicle]);

  useEffect(() => {
    const map = mapRef.current;
    const coordinates = routeGeojson.features[0]?.geometry.coordinates;
    if (!map || !selectedRouteId || !coordinates?.length || selectedVehicleId) return;

    const bounds = coordinates.reduce(
      (b, coord) => b.extend(coord as [number, number]),
      new maplibregl.LngLatBounds(coordinates[0] as [number, number], coordinates[0] as [number, number]),
    );

    map.fitBounds(bounds, { padding: 80, maxZoom: 14.5, duration: 700 });
  }, [routeGeojson, selectedVehicleId]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapLoaded) return;
    const styleReady = map.isStyleLoaded();
    log(`[route src] routeGeojson changed: ${routeGeojson.features.length} features, styleLoaded=${styleReady}`);
    const source = map.getSource(ROUTE_SOURCE_ID) as GeoJSONSource | undefined;
    if (source) { source.setData(routeGeojson); log(`[route src] setData ok (${routeGeojson.features.length} features)`, "ok"); }
    else log(`[route src] source not found!`, "error");
  }, [routeGeojson, mapLoaded]);

  // Fetch and draw all route shapes when a vehicle type is selected
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapLoaded) return;
    const source = map.getSource(TYPE_ROUTES_SOURCE_ID) as GeoJSONSource | undefined;
    if (!source) return;

    if (selectedRouteType === null) {
      source.setData({ type: "FeatureCollection", features: [] });
      return;
    }

    const typeRoutes = routes.filter((r) => r.route_type === selectedRouteType);
    if (typeRoutes.length === 0) return;

    const apiBase = selectedCityCode ? `/cities/${encodeURIComponent(selectedCityCode)}` : null;
    if (!apiBase) return;

    let cancelled = false;
    Promise.all(
      typeRoutes.map((r) =>
        fetch(`${config.apiUrl}${apiBase}/routes/${encodeURIComponent(r.route_id)}/shape`)
          .then((res) => res.ok ? res.json() : null)
          .catch(() => null)
      )
    ).then((results) => {
      if (cancelled) return;
      const features = results.flatMap((data, i) => {
        if (!data || !data.points || data.points.length < 2) return [];
        const coords = data.points.map((p: { lon: number; lat: number }) => [p.lon, p.lat]);
        const routeColor = typeRoutes[i].route_color
          ? `#${typeRoutes[i].route_color!.replace(/^#/, "")}`
          : "#888";
        return [{ type: "Feature" as const, geometry: { type: "LineString" as const, coordinates: coords }, properties: { route_color: routeColor, route_id: typeRoutes[i].route_id } }];
      });
      const src = map.getSource(TYPE_ROUTES_SOURCE_ID) as GeoJSONSource | undefined;
      if (src) src.setData({ type: "FeatureCollection", features });
    });

    return () => { cancelled = true; };
  }, [selectedRouteType, routes, selectedCityCode, mapLoaded]);

  // Update route line color when selected route changes
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapLoaded) return;
    const routeLineDefault = getComputedStyle(document.documentElement)
      .getPropertyValue("--map-route-line").trim() || "#c33149";
    const routeHaloDefault = getComputedStyle(document.documentElement)
      .getPropertyValue("--map-route-halo").trim() || "#100f0e";
    const routeStopStrokeDefault = getComputedStyle(document.documentElement)
      .getPropertyValue("--map-route-stop-stroke").trim() || "#c33149";
    const color = selectedRouteColor ?? routeLineDefault;
    if (map.getLayer(ROUTE_LAYER_ID)) {
      map.setPaintProperty(ROUTE_LAYER_ID, "line-color", color);
      map.setPaintProperty(ROUTE_HALO_LAYER_ID, "line-color",
        selectedRouteColor ? "rgba(0,0,0,0.4)" : routeHaloDefault);
    }
    // Match route stop dot stroke to route color
    if (map.getLayer(STOPS_LAYER_ID)) {
      map.setPaintProperty(STOPS_LAYER_ID, "circle-stroke-color", [
        "case", ["boolean", ["get", "route_stop"], false],
        selectedRouteColor ?? routeStopStrokeDefault,
        getComputedStyle(document.documentElement).getPropertyValue("--map-normal-stop-stroke").trim() || "#3a3330"
      ]);
    }
  }, [selectedRouteColor, mapLoaded, routeGeojson]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapLoaded) return;
    const source = map.getSource(VEHICLE_SOURCE_ID) as GeoJSONSource | undefined;
    if (!source) { log(`[vehicle src] source not found!`, "error"); return; }

    const DURATION = 1500; // slightly under the ~15s update interval
    const newFeatures = vehicleGeojson.features;

    // Capture current rendered positions as the animation start points.
    const prevRendered = (map as any).__renderedVehiclePos as Map<string, [number, number]> | undefined;

    // If only selectedVehicleId changed (no coordinate changes), skip the lerp entirely
    // to avoid restarting the animation with identical positions.
    if (prevRendered && prevRendered.size > 0) {
      const allSame = newFeatures.every(f => {
        const id = f.properties?.id as string;
        const prev = prevRendered.get(id);
        if (!prev) return false;
        const [nx, ny] = f.geometry.coordinates as [number, number];
        return Math.abs(prev[0] - nx) < 1e-7 && Math.abs(prev[1] - ny) < 1e-7;
      });
      if (allSame) {
        source.setData(vehicleGeojson);
        return;
      }
    }

    // Seed prevPositions from rendered positions, keeping entries for vehicles
    // not in this frame so they don't jump if they re-enter.
    const prevPositions = new Map<string, [number, number]>(prevRendered ?? []);

    const startTime = Date.now();
    let raf = (map as any).__vehicleRaf as number | undefined;
    if (raf) cancelAnimationFrame(raf);

    function animate() {
      const t = Math.min(1, (Date.now() - startTime) / DURATION);
      const lerp = (a: number, b: number) => a + (b - a) * t;
      const rendered = new Map<string, [number, number]>();
      const interpolated = {
        type: "FeatureCollection" as const,
        features: newFeatures.map(f => {
          const id = f.properties?.id as string;
          const prev = prevPositions.get(id);
          const [nx, ny] = f.geometry.coordinates as [number, number];
          if (!prev || t >= 1) {
            rendered.set(id, [nx, ny]);
            return f;
          }
          const cx = lerp(prev[0], nx);
          const cy = lerp(prev[1], ny);
          rendered.set(id, [cx, cy]);
          return { ...f, geometry: { type: "Point" as const, coordinates: [cx, cy] } };
        }),
      };
      (map as any).__renderedVehiclePos = rendered;
      source?.setData(interpolated);
      // Move popup with the animated vehicle position
      const selId = (map as any).__selectedVehicleId as string | undefined;
      if (selId) {
        const selFeature = interpolated.features.find(f => f.properties?.id === selId);
        if (selFeature) {
          const [sx, sy] = selFeature.geometry.coordinates as [number, number];
          (map as any).__popupAnimPos = [sx, sy];
          (map as any).__popupRef?.setLngLat([sx, sy]);
        }
      }
      if (t < 1) {
        (map as any).__vehicleRaf = requestAnimationFrame(animate);
      } else {
        (map as any).__vehicleRaf = undefined;

        const sid = (map as any).__selectedVehicleId as string | undefined;
        const pos = sid ? rendered.get(sid) : undefined;

        if (pos) {
          (map as any).__popupAnimPos = pos;
          (map as any).__popupRef?.setLngLat(pos);
        }
      }
    }

    animate();

    return () => {
      if ((map as any).__vehicleRaf) {
        cancelAnimationFrame((map as any).__vehicleRaf);
        (map as any).__vehicleRaf = undefined;
      }
      if ((map as any).__idleRaf) {
        cancelAnimationFrame((map as any).__idleRaf);
        (map as any).__idleRaf = undefined;
      }
    };
  }, [vehicleGeojson, mapLoaded]);

  // Generate canvas badge images for each unique iconKey
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapLoaded) return;

    const registered: Set<string> = (map as any).__vehicleBadgeKeys ?? new Set<string>();
    (map as any).__vehicleBadgeKeys = registered;

    vehicleGeojson.features.forEach((f) => {
      const key = f.properties?.iconKey as string | undefined;
      if (!key || registered.has(key)) return;

      const color = `#${(f.properties?.route_color as string ?? "c33149").replace(/^#/, "")}`;
      const label = (f.properties?.pillLabel as string ?? f.properties?.label as string ?? "?").replace(/^[↑↗→↘↓↙←↖]\s*/, "");

      const scale = 2;
      const hasArrow = key.includes("_arr_");
      const arrowDir = hasArrow ? parseInt(key.split("_arr_")[1]) : -1;
      const H = Math.round(18 * scale);
      const R = Math.round(5 * scale);
      const B = Math.round(1.5 * scale);
      const fontSize = 10 * scale;
      // Measure text to set dynamic width
      const measureCanvas = document.createElement("canvas");
      const mCtx = measureCanvas.getContext("2d")!;
      mCtx.font = `700 ${fontSize}px "Open Sans", Arial, sans-serif`;
      const textW = mCtx.measureText(label).width;
      const arrowSpace = hasArrow ? 10 * scale : 0;
      const W = Math.round(textW + arrowSpace + 14 * scale);
      const canvas = document.createElement("canvas");
      canvas.width = W; canvas.height = H;
      const ctx = canvas.getContext("2d")!;

      function rrect(x: number, y: number, w: number, h: number, r: number) {
        ctx.beginPath();
        ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y);
        ctx.quadraticCurveTo(x + w, y, x + w, y + r);
        ctx.lineTo(x + w, y + h - r);
        ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
        ctx.lineTo(x + r, y + h);
        ctx.quadraticCurveTo(x, y + h, x, y + h - r);
        ctx.lineTo(x, y + r);
        ctx.quadraticCurveTo(x, y, x + r, y);
        ctx.closePath();
      }

      rrect(0, 0, W, H, R); ctx.fillStyle = "#ffffff"; ctx.fill();
      rrect(B, B, W - B * 2, H - B * 2, Math.max(2, R - B)); ctx.fillStyle = color; ctx.fill();

      if (hasArrow && arrowDir >= 0) {
        // Draw small direction arrow on left side
        const ax = Math.round(9 * scale), ay = H / 2, as_ = 5.5 * scale;
        const rad = (arrowDir * Math.PI) / 180;
        ctx.save();
        ctx.translate(ax, ay);
        ctx.rotate(rad);
        ctx.beginPath();
        ctx.moveTo(0, -as_);
        ctx.lineTo(as_ * 0.6, as_ * 0.5);
        ctx.lineTo(-as_ * 0.6, as_ * 0.5);
        ctx.closePath();
        ctx.fillStyle = "rgba(255,255,255,0.9)";
        ctx.fill();
        ctx.restore();
      }

      ctx.font = `700 ${fontSize}px "Open Sans", Arial, sans-serif`;
      ctx.fillStyle = "#ffffff"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      const textX = hasArrow ? (W / 2) + Math.round(5 * scale) : W / 2;
      ctx.fillText(label, textX, H / 2);

      try {
        map.addImage(key, { width: W, height: H, data: new Uint8Array(ctx.getImageData(0, 0, W, H).data) });
        registered.add(key);
      } catch {}
    });
  }, [vehicleGeojson, mapLoaded]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapLoaded) return;
    const source = map.getSource(STOPS_SOURCE_ID) as GeoJSONSource | undefined;
    if (source) source.setData(stopGeojson);
    else log(`[stops src] source not found!`, "error");
  }, [stopGeojson, mapLoaded]);

  function handleLocate(lat: number, lon: number) {
    if (mapRef.current) mapRef.current.flyTo({ center: [lon, lat], zoom: 15, duration: 800 });
  }
  function handleSatellite(satellite: boolean) {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;
    try {
      const source = map.getSource("base") as maplibregl.RasterTileSource | undefined;
      if (source) {
        if (satellite) {
          source.setTiles(["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"]);
          map.setPaintProperty("base", "raster-opacity", 1);
          map.setPaintProperty("base", "raster-saturation", 0);
          map.setPaintProperty("base", "raster-contrast", 0);
        } else {
          const cssStyle = getComputedStyle(document.documentElement);
          const cssVar = (n: string, f: string) => cssStyle.getPropertyValue(n).trim() || f;
          source.setTiles([theme === "light" ? "https://basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}.png" : "https://basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}.png"]);
          map.setPaintProperty("base", "raster-opacity", parseFloat(cssVar("--map-raster-opacity", "0.78")));
          map.setPaintProperty("base", "raster-saturation", parseFloat(cssVar("--map-raster-saturation", "-0.45")));
          map.setPaintProperty("base", "raster-contrast", parseFloat(cssVar("--map-raster-contrast", "-0.08")));
        }
      }
    } catch {}
  }
  function handleFavStop(_cityCode: string, stopId: string) {
    useStopStore.getState().setSelectedStopId(stopId);
  }

  return (
    <div className="map" style={{ position: "relative" }}>
      <div className="map-controls-overlay">
        <SatelliteControl onToggle={handleSatellite} />
        <GpsControl onLocate={handleLocate} />
        <FavouritesControl onSelectStop={handleFavStop} />
      </div>
      <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />
      {debugEnabled && (
        <DebugOverlay lines={lines} />
      )}
    </div>
  );
}
