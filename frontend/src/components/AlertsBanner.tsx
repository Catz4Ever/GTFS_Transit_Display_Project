import { useState } from "react";
import { useAlerts } from "../hooks/useAlerts";

export function AlertsBanner() {
  const { globalAlerts } = useAlerts();
  const [expanded, setExpanded] = useState<string | null>(null);

  if (globalAlerts.length === 0) return null;

  return (
    <div className="alerts-banner">
      <div className="alerts-banner__header">
        <span className="alerts-banner__icon">⚠</span>
        <span className="alerts-banner__count">
          {globalAlerts.length} network alert{globalAlerts.length !== 1 ? "s" : ""}
        </span>
      </div>
      <div className="alerts-banner__list">
        {globalAlerts.map((alert) => (
          <div
            key={alert.entity_id}
            className="alerts-banner__item"
            onClick={() => setExpanded(expanded === alert.entity_id ? null : alert.entity_id)}
          >
            <div className="alerts-banner__item-header">
              <span className="alerts-banner__effect">{alert.effect ?? "Alert"}</span>
              <span className="alerts-banner__title">{alert.header ?? "Service alert"}</span>
              <span className="alerts-banner__chevron">{expanded === alert.entity_id ? "▲" : "▼"}</span>
            </div>
            {expanded === alert.entity_id && (
              <div className="alerts-banner__detail">
                {alert.cause && <div className="alerts-banner__cause">Cause: {alert.cause}</div>}
                {alert.description && <p className="alerts-banner__desc">{alert.description}</p>}
                {alert.url && (
                  <a href={alert.url} target="_blank" rel="noopener noreferrer" className="alerts-banner__link">
                    More info →
                  </a>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
