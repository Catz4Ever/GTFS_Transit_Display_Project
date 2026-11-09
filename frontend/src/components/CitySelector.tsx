import { useCityStore } from "../stores/cityStore";
import { useLangStore } from "../hooks/useLanguage";

export function CitySelector() {
  const { t } = useLangStore();
  const cities = useCityStore((s) => s.cities);
  const selectedCityCode = useCityStore((s) => s.selectedCityCode);
  const setSelectedCityCode = useCityStore((s) => s.setSelectedCityCode);
  const loading = useCityStore((s) => s.loading);

  return (
    <label className="control">
      <span>{t.cityLabel}</span>
      <select
        value={selectedCityCode ?? ""}
        disabled={loading || cities.length === 0}
        onChange={(event) => setSelectedCityCode(event.target.value || null)}
      >
        {cities.length === 0 && <option value="">{t.noCitiesFound}</option>}
        {cities.map((city) => (
          <option key={city.city_code} value={city.city_code}>
            {city.city_name}
          </option>
        ))}
      </select>
    </label>
  );
}
