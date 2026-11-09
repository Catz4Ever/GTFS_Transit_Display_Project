import { useMemo, useRef, useState } from "react";
import { useStopStore } from "../stores/stopStore";
import { useLangStore } from "../hooks/useLanguage";

function isBoardableStop(stop: { location_type?: number | null }) {
  return stop.location_type === null || stop.location_type === undefined || Number(stop.location_type) === 0;
}

function stopLabel(stopName: string, stopCode?: string | null, stopId?: string) {
  const code = stopCode || stopId || "";
  return code ? `${stopName} (${code})` : stopName;
}

export function StopSelector() {
  const { t } = useLangStore();
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const closeTimerRef = useRef<number | null>(null);

  const stops = useStopStore((s) => s.stops);
  const selectedStopId = useStopStore((s) => s.selectedStopId);
  const setSelectedStopId = useStopStore((s) => s.setSelectedStopId);
  const loading = useStopStore((s) => s.loading);
  const error = useStopStore((s) => s.error);

  const boardableStops = useMemo(() => stops.filter(isBoardableStop), [stops]);

  const filteredStops = useMemo(() => {
    const normalized = query.trim().toLowerCase();

    if (!normalized) {
      return boardableStops.slice(0, 25);
    }

    return boardableStops
      .filter((stop) => {
        return (
          stop.stop_id.toLowerCase().includes(normalized) ||
          (stop.stop_code ?? "").toLowerCase().includes(normalized) ||
          stop.stop_name.toLowerCase().includes(normalized)
        );
      })
      .slice(0, 30);
  }, [query, boardableStops]);

  const selectedStop = stops.find((stop) => stop.stop_id === selectedStopId);

  function scheduleClose() {
    closeTimerRef.current = window.setTimeout(() => {
      setOpen(false);
    }, 120);
  }

  function cancelClose() {
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
  }

  return (
    <div className="stop-selector" onBlur={scheduleClose} onFocus={cancelClose}>
      <label className="control">
        <span>{t.station}</span>
        <input
          value={query}
          disabled={loading}
          placeholder={
            loading
              ? t.loadingStops
              : selectedStop
                ? stopLabel(selectedStop.stop_name, selectedStop.stop_code, selectedStop.stop_id)
                : t.searchStopPlaceholder
          }
          onFocus={() => setOpen(true)}
          onChange={(event) => {
            setQuery(event.target.value);
            setOpen(true);
          }}
        />
      </label>

      {open && !loading && query.trim().length > 0 && (
        <div className="stop-results">
          {error && <div className="stop-empty">{t.couldNotLoadStops}: {error}</div>}

          {!error && filteredStops.length === 0 && <div className="stop-empty">{t.noStopsFound}</div>}

          {!error &&
            filteredStops.map((stop) => (
              <button
                key={stop.stop_id}
                className={stop.stop_id === selectedStopId ? "selected" : ""}
                title={`${stop.stop_name} (${stop.stop_id})`}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  setSelectedStopId(stop.stop_id);
                  setQuery("");
                  setOpen(false);
                }}
              >
                <strong>{stop.stop_code || stop.stop_id}</strong>
                <span>{stop.stop_name}</span>
              </button>
            ))}
        </div>
      )}
    </div>
  );
}
