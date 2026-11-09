import { useCallback, useEffect, useState } from "react";

const DEBUG = new URLSearchParams(window.location.search).has("debug");
const MAX_LINES = 40;

export type LogLine = { t: string; msg: string; kind: "info" | "warn" | "error" | "ok" };

let globalLog: (msg: string, kind?: LogLine["kind"]) => void = () => {};

export function useMapDebug() {
  const [lines, setLines] = useState<LogLine[]>([]);

  const log = useCallback((msg: string, kind: LogLine["kind"] = "info") => {
    if (!DEBUG) return;
    const now = new Date();
    const t = `${String(now.getHours()).padStart(2,"0")}:${String(now.getMinutes()).padStart(2,"0")}:${String(now.getSeconds()).padStart(2,"0")}.${String(now.getMilliseconds()).padStart(3,"0")}`;
    setLines((prev) => [...prev.slice(-(MAX_LINES - 1)), { t, msg, kind }]);
  }, []);

  useEffect(() => { globalLog = log; return () => { globalLog = () => {}; }; }, [log]);

  return { log, lines, enabled: DEBUG };
}

export function mapLog(msg: string, kind: LogLine["kind"] = "info") {
  globalLog(msg, kind);
}
