import type { Vehicle } from "../types/transit";
import type { VehicleDisplayMode } from "../stores/vehicleStore";

export function vehicleMatchesDisplayMode(vehicle: Pick<Vehicle, "source">, displayMode: VehicleDisplayMode): boolean {
  if (displayMode === "all") return true;
  const source = (vehicle.source ?? "").toLowerCase();
  if (displayMode === "simulated") return source.includes("simulated");
  return source.includes("gtfs") || source.includes("rt") || source === "fused";
}

export function filterVehiclesByDisplayMode(vehicles: Vehicle[], displayMode: VehicleDisplayMode): Vehicle[] {
  return vehicles.filter((vehicle) => vehicleMatchesDisplayMode(vehicle, displayMode));
}
