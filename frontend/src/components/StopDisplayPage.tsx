import { useEffect, useRef, useState } from "react";
import mqtt from "mqtt";
import { config } from "../config/runtime";
import { useLangStore } from "../hooks/useLanguage";
import { stopArrivalsTopic } from "../mqtt/topics";
import type { StopArrival, StopArrivalsPayload } from "../stores/stopStore";

// Apply theme from URL param immediately (before first render)
(function applyThemeFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const urlTheme = params.get("theme");
  if (urlTheme === "dark" || urlTheme === "light") {
    localStorage.setItem("transit-theme", urlTheme);
    document.documentElement.setAttribute("data-theme", urlTheme);
  } else {
    const stored = localStorage.getItem("transit-theme") ?? "dark";
    document.documentElement.setAttribute("data-theme", stored);
  }
})();

function parseArrivals(raw: unknown): StopArrivalsPayload | null {
  if (!raw || typeof raw !== "object") return null;
  return raw as StopArrivalsPayload;
}

function etaMinutes(arrival: StopArrival): number | null {
  if (arrival.eta_seconds !== null && arrival.eta_seconds !== undefined && Number.isFinite(Number(arrival.eta_seconds))) {
    return Math.round(Number(arrival.eta_seconds) / 60);
  }
  if ((arrival as any).seconds_until_arrival !== undefined) {
    return Math.round(Number((arrival as any).seconds_until_arrival) / 60);
  }
  if ((arrival as any).minutes_until_arrival !== undefined) {
    return Math.round(Number((arrival as any).minutes_until_arrival));
  }
  return null;
}

function etaLabel(arrival: StopArrival, t: { arriving: string; minAbbr: string }): { text: string; highlight: boolean } {
  const mins = etaMinutes(arrival);
  if (mins === null) return { text: (arrival as any).predicted_time ?? (arrival as any).scheduled_time ?? "—", highlight: false };
  if (mins <= 0) return { text: t.arriving, highlight: true };
  if (mins === 1) return { text: `1 ${t.minAbbr}`, highlight: true };
  return { text: `${mins} ${t.minAbbr}`, highlight: mins <= 2 };
}

function dedupeArrivals(arrivals: StopArrival[]): StopArrival[] {
  const seen = new Set<string>();
  return arrivals.filter((a) => {
    const key = `${(a as any).trip_instance_id ?? (a as any).vehicle_id ?? ""}-${(a as any).route_id ?? ""}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

interface StopDisplayPageProps {
  cityCode: string;
  stopId: string;
}

export function StopDisplayPage({ cityCode, stopId }: StopDisplayPageProps) {
  const [payload, setPayload] = useState<StopArrivalsPayload | null>(null);
  const [connected, setConnected] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [clock, setClock] = useState(() => new Date());
  const { t, setLang } = useLangStore();
  const clientRef = useRef<ReturnType<typeof mqtt.connect> | null>(null);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const urlLang = params.get("lang");
    if (urlLang === "bg" || urlLang === "en") setLang(urlLang);
  }, []);

  useEffect(() => {
    const id = setInterval(() => setClock(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const topic = stopArrivalsTopic(config.topicPrefix, cityCode, stopId);

    const mqttClient = mqtt.connect(config.mqttUrl, {
      username: config.mqttUsername,
      password: config.mqttPassword,
      reconnectPeriod: 3000,
      connectTimeout: 10000,
      clean: true,
      keepalive: 60,
      clientId: `stop-display-${cityCode}-${stopId}-${Math.random().toString(16).slice(2)}`,
    });

    clientRef.current = mqttClient;

    mqttClient.on("connect", () => {
      setConnected(true);
      mqttClient.subscribe(topic);
    });

    mqttClient.on("reconnect", () => setConnected(false));
    mqttClient.on("offline", () => setConnected(false));
    mqttClient.on("error", () => setConnected(false));

    mqttClient.on("message", (incomingTopic: string, buf: Buffer) => {
      if (incomingTopic !== topic) return;
      try {
        const parsed = parseArrivals(JSON.parse(buf.toString()));
        if (parsed) {
          setPayload(parsed);
          setLastUpdated(new Date());
        }
      } catch {}
    });

    return () => {
      mqttClient.end(true);
      clientRef.current = null;
    };
  }, [cityCode, stopId]);

  const arrivals = dedupeArrivals(payload?.arrivals ?? []).slice(0, 8);
  const stopName = (payload as any)?.stop_name ?? stopId;

  const formattedTime = clock.toLocaleTimeString("bg-BG", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });

  return (
    <div className="sd-shell">
      <header className="sd-header">
        <div className="sd-stop-info">
          <div className="sd-stop-name">{stopName}</div>
          <div className="sd-stop-id">{cityCode.toUpperCase()} · {stopId}</div>
        </div>
        <div className="sd-header-right">
          <div className="sd-clock">{formattedTime}</div>
          <div className={`sd-status ${connected ? "sd-status--ok" : "sd-status--err"}`}>
            {connected ? `● ${t.onLive}` : `● ${t.connecting}`}
          </div>
        </div>
      </header>

      <main className="sd-main">
        {arrivals.length === 0 ? (
          <div className="sd-empty">
            {connected ? t.noArrivals : t.connecting}
          </div>
        ) : (
          <table className="sd-table">
            <thead>
              <tr>
                <th className="sd-th">{t.lineLabel}</th>
                <th className="sd-th">{t.colTime}</th>
                <th className="sd-th sd-th--eta">{t.colArrives}</th>
              </tr>
            </thead>
            <tbody>
              {arrivals.map((arrival, i) => {
                const { text, highlight } = etaLabel(arrival, t);
                const a = arrival as any;
                const routeName = a.route_name ?? a.route_id ?? "?";
                const routeColor = a.route_color ? `#${a.route_color.replace(/^#/, "")}` : "var(--accent-primary, #c33149)";
                const time = a.predicted_time ?? a.scheduled_time ?? "—";

                return (
                  <tr key={`${a.trip_instance_id ?? i}-${a.route_id}`} className="sd-row">
                    <td className="sd-td">
                      <span className="sd-badge" style={{ background: routeColor }}>
                        {routeName}
                      </span>
                    </td>
                    <td className="sd-td sd-td--time">{time}</td>
                    <td className={`sd-td sd-td--eta${highlight ? " sd-td--highlight" : ""}`}>
                      {text}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </main>

      <footer className="sd-footer">
        <span>
          {lastUpdated
            ? `${t.lastUpdated}: ${lastUpdated.toLocaleTimeString("bg-BG", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`
            : t.connecting}
        </span>
        <a href="/" className="sd-back">{t.backToMap}</a>
      </footer>
    </div>
  );
}
