import { useState } from "react";
import type { City } from "../types/transit";
import { useLangStore } from "../hooks/useLanguage";
import type { Lang } from "../i18n/translations";

interface CityPickerModalProps {
  cities: City[];
  onSelect: (cityCode: string) => void;
}

export function CityPickerModal({ cities, onSelect }: CityPickerModalProps) {
  const [query, setQuery] = useState("");
  const { t, lang, setLang } = useLangStore();

  const filtered = cities.filter((c) =>
    c.city_name.toLowerCase().includes(query.toLowerCase()) ||
    c.city_code.toLowerCase().includes(query.toLowerCase())
  );

  const grid = filtered.slice(0, 9);

  return (
    <div className="city-picker-overlay">
      <div className="city-picker-modal">
        <div className="city-picker-header">
          <h2 className="city-picker-title">{t.pickCity}</h2>
          <p className="city-picker-sub">{t.appTagline}</p>
        </div>

        <div className="city-picker-lang">
          {(["bg", "en"] as Lang[]).map((l) => (
            <button
              key={l}
              className={`city-picker-lang-btn${lang === l ? " active" : ""}`}
              onClick={() => setLang(l)}
            >
              {l === "bg" ? "🇧🇬 БГ" : "🇬🇧 EN"}
            </button>
          ))}
        </div>

        <div className="city-picker-search-wrap">
          <input
            className="city-picker-search"
            type="text"
            placeholder={t.searchCity}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            autoFocus
          />
        </div>

        {grid.length === 0 ? (
          <p className="city-picker-empty">{t.noCitiesFound}</p>
        ) : (
          <div className="city-picker-grid">
            {grid.map((city) => (
              <button
                key={city.city_code}
                className="city-picker-card"
                onClick={() => onSelect(city.city_code)}
              >
                <span className="city-picker-card-name">{city.city_name}</span>
                <span className="city-picker-card-meta">
                  {city.route_count ? `${city.route_count} ${t.routes}` : ""}
                  {city.route_count && city.stop_count ? " · " : ""}
                  {city.stop_count ? `${city.stop_count} ${t.stops}` : ""}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
