import { create } from "zustand";
import type { City } from "../types/transit";

const STORAGE_KEY = "transit-selected-city";

function loadPersistedCity(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function persistCity(cityCode: string | null) {
  try {
    if (cityCode) localStorage.setItem(STORAGE_KEY, cityCode);
    else localStorage.removeItem(STORAGE_KEY);
  } catch {}
}

type CityState = {
  cities: City[];
  selectedCityCode: string | null;
  loading: boolean;
  error: string | null;

  setCities: (cities: City[]) => void;
  setSelectedCityCode: (cityCode: string | null) => void;
  setLoading: (loading: boolean) => void;
  setError: (error: string | null) => void;
};

export const useCityStore = create<CityState>((set, get) => ({
  cities: [],
  selectedCityCode: loadPersistedCity(),
  loading: false,
  error: null,

  setCities: (cities) => {
    const persisted = get().selectedCityCode;
    // Validate persisted city still exists in the fetched list
    const valid = persisted && cities.some((c) => c.city_code === persisted)
      ? persisted
      : null;
    set({ cities, selectedCityCode: valid });
  },

  setSelectedCityCode: (selectedCityCode) => {
    persistCity(selectedCityCode);
    set({ selectedCityCode });
  },

  setLoading: (loading) => set({ loading }),
  setError: (error) => set({ error }),
}));

export function useSelectedCity() {
  const cities = useCityStore((s) => s.cities);
  const selectedCityCode = useCityStore((s) => s.selectedCityCode);
  return cities.find((city) => city.city_code === selectedCityCode) ?? null;
}
