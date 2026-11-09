import { useEffect } from "react";
import { client } from "./mqttClient";
import { stopArrivalsTopic } from "./topics";
import { config } from "../config/runtime";
import { fetchStopArrivals } from "../api/referenceData";
import { parseStopArrivalsPayload } from "../utils/validation";
import { useCityStore } from "../stores/cityStore";
import { useStopStore } from "../stores/stopStore";

export function useStopArrivalsSubscription() {
  const cityCode = useCityStore((s) => s.selectedCityCode);
  const selectedStopId = useStopStore((s) => s.selectedStopId);
  const setArrivals = useStopStore((s) => s.setArrivals);
  const clearArrivals = useStopStore((s) => s.clearArrivals);
  const setStopStatus = useStopStore((s) => s.setStopStatus);

  useEffect(() => {
    if (!cityCode || !selectedStopId) {
      clearArrivals();
      return;
    }

    let cancelled = false;
    let mqttArrived = false;

    const topic = stopArrivalsTopic(config.topicPrefix, cityCode, selectedStopId);

    const fallbackTimer = window.setTimeout(() => {
      if (cancelled || mqttArrived) return;

      fetchStopArrivals(cityCode, selectedStopId)
        .then((payload) => {
          if (!cancelled && !mqttArrived) {
            setArrivals(payload, "api");
            setStopStatus(`loaded live arrivals from API fallback`);
          }
        })
        .catch((err) => {
          if (!cancelled && !mqttArrived) setStopStatus(`API arrivals load failed: ${(err as Error).message}`);
        });
    }, 900);

    const subscribe = () => {
      client.subscribe(topic, (err) => {
        if (err) {
          setStopStatus(`subscribe failed: \${err.message}`);
        } else if (new URLSearchParams(window.location.search).has("debug")) {
          setStopStatus(`subscribed: \${topic}`);
        } else {
          setStopStatus("");
        }
      });
    };

    const onMessage = (incomingTopic: string, payload: Buffer) => {
      if (incomingTopic !== topic) return;

      try {
        mqttArrived = true;
        window.clearTimeout(fallbackTimer);
        setArrivals(parseStopArrivalsPayload(JSON.parse(payload.toString())), "mqtt");
      } catch (err) {
        setStopStatus(`stop arrivals validation failed: ${(err as Error).message}`);
      }
    };

    client.on("message", onMessage);

    if (client.connected) subscribe();
    else client.once("connect", subscribe);

    return () => {
      cancelled = true;
      window.clearTimeout(fallbackTimer);
      client.unsubscribe(topic);
      client.off("message", onMessage);
      client.off("connect", subscribe);
    };
  }, [cityCode, selectedStopId, setArrivals, clearArrivals, setStopStatus]);
}
