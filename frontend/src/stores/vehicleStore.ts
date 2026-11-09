import { create } from "zustand";
import type { Vehicle } from "../types/transit";
import { filterVehiclesByDisplayMode } from "../utils/vehicleFiltering";

function normalizeVehicleIdentity(vehicle: Vehicle, index: number): Vehicle {
  const id = String(
    vehicle.id ??
      vehicle.trip_instance_id ??
      vehicle.vehicle_id ??
      vehicle.vehicle_label ??
      `${vehicle.route_id ?? "vehicle"}-${index}`,
  );

  return { ...vehicle, id };
}

export type VehicleDisplayMode = "all" | "rt" | "simulated";

type VehicleState = {
  displayMode: VehicleDisplayMode;
  setDisplayMode: (mode: VehicleDisplayMode) => void;
  selectedRouteType: number | null;
  setSelectedRouteType: (type: number | null) => void;
  allVehicles: Vehicle[];
  vehicles: Vehicle[];
  updatedAt: string | null;
  status: string;
  activeRouteIds: Set<string>;
  setVehicles: (vehicles: Vehicle[], updatedAt?: string | null, isFullSnapshot?: boolean) => void;
  setStatus: (status: string) => void;
};

export const useVehicleStore = create<VehicleState>((set) => ({
  displayMode: "all",
  selectedRouteType: null,
  allVehicles: [],
  vehicles: [],
  updatedAt: null,
  status: "connecting",
  activeRouteIds: new Set<string>(),
  setDisplayMode: (displayMode) =>
    set((state) => ({
      displayMode,
      vehicles: filterVehiclesByDisplayMode(state.allVehicles, displayMode),
    })),
  setSelectedRouteType: (selectedRouteType) => set({ selectedRouteType }),
  setVehicles: (vehicles, updatedAt = null, isFullSnapshot = false) =>
    set((state) => {
      const allVehicles = vehicles.map(normalizeVehicleIdentity);
      const activeRouteIds = isFullSnapshot
        ? new Set<string>(allVehicles.map((v) => v.route_id).filter(Boolean) as string[])
        : state.activeRouteIds;
      return {
        allVehicles,
        vehicles: filterVehiclesByDisplayMode(allVehicles, state.displayMode),
        updatedAt,
        activeRouteIds,
      };
    }),
  setStatus: (status) => set({ status }),
}));
