export function vehicleSnapshotTopic(prefix: string, city: string) {
  return `${prefix}/${city}/vehicles/snapshot`;
}

export function routeVehicleSnapshotTopic(prefix: string, city: string, routeId: string) {
  return `${prefix}/${city}/routes/${routeId}/vehicles/snapshot`;
}

export function stopArrivalsTopic(prefix: string, city: string, stopId: string) {
  return `${prefix}/${city}/stops/${stopId}/arrivals`;
}
