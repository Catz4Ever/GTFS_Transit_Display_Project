import { useEffect } from "react";
import { client } from "./mqttClient";
import { routeVehicleSnapshotTopic, vehicleSnapshotTopic } from "./topics";
import { config } from "../config/runtime";
import { useCityStore } from "../stores/cityStore";
import { useRouteStore } from "../stores/routeStore";
import { useVehicleStore } from "../stores/vehicleStore";
import type { Vehicle } from "../types/transit";
import { parseVehicleSnapshot } from "../utils/validation";

type CachedVehicleSnapshot = {
  vehicles: Vehicle[];
  updatedAt: string | null;
};

const snapshotCache = new Map<string, CachedVehicleSnapshot>();

export function useVehicleSubscription() {
  const cityCode = useCityStore((s) => s.selectedCityCode);
  const selectedRouteId = useRouteStore((s) => s.selectedRouteId);
  const setVehicles = useVehicleStore((s) => s.setVehicles);
  const setStatus = useVehicleStore((s) => s.setStatus);

  useEffect(() => {
    if (!cityCode) return;

    const topic = selectedRouteId
      ? routeVehicleSnapshotTopic(config.topicPrefix, cityCode, selectedRouteId)
      : vehicleSnapshotTopic(config.topicPrefix, cityCode);

    const cached = snapshotCache.get(topic);
    if (cached) {
      setVehicles(cached.vehicles, cached.updatedAt, !selectedRouteId);
      setStatus(`restored cached snapshot: ${topic}`);
    } else {
      // Don't clear vehicles on switch — keep previous city/route visible
      // until the new snapshot arrives and overwrites them naturally
      setStatus(`waiting for data: ${topic}`);
    }

    const subscribe = () => {
      client.subscribe(topic, (err) => {
        setStatus(err ? `subscribe failed: ${err.message}` : `subscribed: ${topic}`);
      });
    };

    const onMessage = (incomingTopic: string, payload: Buffer) => {
      if (incomingTopic !== topic) return;

      try {
        const data = parseVehicleSnapshot(JSON.parse(payload.toString()));
        const allVehicles = data.vehicles ?? [];
        snapshotCache.set(topic, { vehicles: allVehicles, updatedAt: data.updated_at ?? null });
        setVehicles(allVehicles, data.updated_at ?? null, !selectedRouteId);
      } catch (err) {
        setStatus(`vehicle payload validation failed: ${(err as Error).message}`);
      }
    };

    client.on("message", onMessage);

    if (client.connected) subscribe();
    else client.once("connect", subscribe);

    return () => {
      client.unsubscribe(topic);
      client.off("message", onMessage);
      client.off("connect", subscribe);
    };
  }, [cityCode, selectedRouteId, setStatus, setVehicles]);
}
