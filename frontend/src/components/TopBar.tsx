import { CitySelector } from "./CitySelector";
import { RouteSelector } from "./RouteSelector";
import { StopSelector } from "./StopSelector";
import { useSelectedCity } from "../stores/cityStore";
import { useRouteStore } from "../stores/routeStore";
import { useVehicleStore } from "../stores/vehicleStore";
import { useTheme } from "../hooks/useTheme";
import { useLangStore } from "../hooks/useLanguage";
import type { Lang } from "../i18n/translations";

export function TopBar() {
  const displayMode = useVehicleStore((s) => s.displayMode);
  const setDisplayMode = useVehicleStore((s) => s.setDisplayMode);
  const selectedCity = useSelectedCity();
  const selectedRouteId = useRouteStore((s) => s.selectedRouteId);
  const vehicles = useVehicleStore((s) => s.vehicles);
  const updatedAt = useVehicleStore((s) => s.updatedAt);
  const status = useVehicleStore((s) => s.status);
  const isDebug = new URLSearchParams(window.location.search).has("debug");
  const { theme, toggle } = useTheme();
  const { t, lang, setLang } = useLangStore();

  return (
    <header className="topbar">
      <div className="brand">
        <strong>{selectedCity?.city_name ?? "Transit map"}</strong>
        {isDebug && <span>{vehicles.length} {t.vehicles}</span>}
        {isDebug && <span>{selectedRouteId ? `${t.lineLabel} ${selectedRouteId}` : t.allLines}</span>}
        <span>{updatedAt ? `${t.updated} ${new Date(updatedAt).toLocaleTimeString()}` : t.waitingForData}</span>
      
      <label className="topbar-label topbar-mode">
        {t.mode}
        <select
          value={displayMode}
          onChange={(event) => setDisplayMode(event.target.value as "all" | "rt" | "simulated")}
          className="topbar-select"
        >
          <option value="all">{t.modeAll}</option>
          <option value="rt">{t.modeRt}</option>
          <option value="simulated">{t.modeSim}</option>
        </select>
      </label>

    </div>

      <div className="controls">
        <CitySelector />
        <RouteSelector />
        <StopSelector />
      </div>

      <div className="topbar-right">
        {isDebug && <small title={status}>{status}</small>}
        <div className="lang-toggle">
          {(["bg", "en"] as Lang[]).map((l) => (
            <button
              key={l}
              className={`lang-btn${lang === l ? " active" : ""}`}
              onClick={() => setLang(l)}
              title={l === "bg" ? "Български" : "English"}
            >
              {l === "bg" ? "БГ" : "EN"}
            </button>
          ))}
        </div>
        <button
          className="theme-toggle"
          onClick={toggle}
          title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
          aria-label="Toggle theme"
        >
          {theme === "dark" ? "☀️" : "🌙"}
        </button>
      </div>
    </header>
  );
}
