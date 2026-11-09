export const config = {
  apiUrl: import.meta.env.VITE_API_URL ?? "http://localhost:8000",
  mqttUrl: import.meta.env.VITE_MQTT_URL ?? "ws://127.0.0.1:9001",
  topicPrefix: import.meta.env.VITE_TOPIC_PREFIX ?? "transit",
  mqttUsername: import.meta.env.VITE_MQTT_USERNAME,
  mqttPassword: import.meta.env.VITE_MQTT_PASSWORD,
};
