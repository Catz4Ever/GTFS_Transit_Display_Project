import { useEffect, useRef, useState } from "react";
import { useLangStore } from "../hooks/useLanguage";


// ── Satellite Control ─────────────────────────────────────────────────────────

interface SatelliteControlProps {
  onToggle: (satellite: boolean) => void;
}

export function SatelliteControl({ onToggle }: SatelliteControlProps) {
  const [active, setActive] = useState(false);

  function handleClick() {
    const next = !active;
    setActive(next);
    onToggle(next);
  }

  return (
    <button
      className={`map-control-btn map-control-satellite${active ? " active" : ""}`}
      onClick={handleClick}
      title={active ? "Switch to map view" : "Switch to satellite view"}
      aria-label="Satellite view"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="3"/>
        <path d="M6.3 6.3a8 8 0 0 0 0 11.4"/>
        <path d="M17.7 6.3a8 8 0 0 1 0 11.4"/>
        <path d="M3.5 3.5a14 14 0 0 0 0 17"/>
        <path d="M20.5 3.5a14 14 0 0 1 0 17"/>
      </svg>
    </button>
  );
}

// ── GPS Control ──────────────────────────────────────────────────────────────

interface GpsControlProps {
  onLocate: (lat: number, lon: number) => void;
}

export function GpsControl({ onLocate }: GpsControlProps) {
  const [state, setState] = useState<"idle" | "locating" | "active" | "error">("idle");

  function handleClick() {
    if (!navigator.geolocation) {
      setState("error");
      return;
    }
    setState("locating");
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setState("active");
        onLocate(pos.coords.latitude, pos.coords.longitude);
      },
      () => setState("error"),
      { timeout: 8000, enableHighAccuracy: true }
    );
  }

  return (
    <button
      className={`map-control-btn map-control-gps map-control-gps--${state}`}
      onClick={handleClick}
      title="My location"
      aria-label="My location"
    >
      {state === "locating" ? (
        <span className="map-control-spinner" />
      ) : (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="3" />
          <line x1="12" y1="2" x2="12" y2="6" />
          <line x1="12" y1="18" x2="12" y2="22" />
          <line x1="2" y1="12" x2="6" y2="12" />
          <line x1="18" y1="12" x2="22" y2="12" />
        </svg>
      )}
    </button>
  );
}

// ── Favourites ────────────────────────────────────────────────────────────────

const FAV_KEY = "transit-favourites";

export type Favourite = {
  id: string;
  type: "stop" | "vehicle";
  cityCode: string;
  label: string;
  stopId?: string;
  routeId?: string;
};

export function loadFavourites(): Favourite[] {
  try {
    return JSON.parse(localStorage.getItem(FAV_KEY) ?? "[]");
  } catch { return []; }
}

export function saveFavourites(favs: Favourite[]) {
  try { localStorage.setItem(FAV_KEY, JSON.stringify(favs)); } catch {}
}

export function isFavourite(id: string): boolean {
  return loadFavourites().some((f) => f.id === id);
}

export function toggleFavourite(fav: Favourite): boolean {
  const favs = loadFavourites();
  const idx = favs.findIndex((f) => f.id === fav.id);
  if (idx >= 0) {
    favs.splice(idx, 1);
    saveFavourites(favs);
    return false;
  } else {
    favs.push(fav);
    saveFavourites(favs);
    return true;
  }
}

interface FavouritesControlProps {
  onSelectStop: (cityCode: string, stopId: string) => void;
}

export function FavouritesControl({ onSelectStop }: FavouritesControlProps) {
  const [open, setOpen] = useState(false);
  const [favs, setFavs] = useState<Favourite[]>(loadFavourites);
  const panelRef = useRef<HTMLDivElement>(null);
  const { t } = useLangStore();

  // Refresh when opened
  useEffect(() => {
    if (open) setFavs(loadFavourites());
  }, [open]);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    function handler(e: MouseEvent) {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  function remove(id: string) {
    const next = favs.filter((f) => f.id !== id);
    saveFavourites(next);
    setFavs(next);
  }

  return (
    <div className="map-control-favs-wrap" ref={panelRef}>
      <button
        className={`map-control-btn map-control-favs-btn${open ? " active" : ""}`}
        onClick={() => setOpen((v) => !v)}
        title="Favourites"
        aria-label="Favourites"
      >
        <svg viewBox="0 0 24 24" fill={open ? "currentColor" : "none"} stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
        </svg>
      </button>

      {open && (
        <div className="map-favs-panel">
          <div className="map-favs-title">{t.board}</div>
          {favs.length === 0 ? (
            <p className="map-favs-empty">{t.noStopSelected}</p>
          ) : (
            <ul className="map-favs-list">
              {favs.map((fav) => (
                <li key={fav.id} className="map-favs-item">
                  <button
                    className="map-favs-item__label"
                    onClick={() => {
                      if (fav.type === "stop" && fav.stopId) {
                        onSelectStop(fav.cityCode, fav.stopId);
                        // Dispatch flyToStop event so map centers on the stop
                        window.dispatchEvent(new CustomEvent("flyToStop", { detail: { stopId: fav.stopId } }));
                        setOpen(false);
                      }
                    }}
                  >
                    <span className="map-favs-item__type">{fav.type === "stop" ? "🚏" : "🚌"}</span>
                    <span>{fav.label}</span>
                    <span className="map-favs-item__city">{fav.cityCode}</span>
                  </button>
                  <button className="map-favs-item__remove" onClick={() => remove(fav.id)} title="Remove">✕</button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
