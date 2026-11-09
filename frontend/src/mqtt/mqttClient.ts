import mqtt from "mqtt";
import { config } from "../config/runtime";

export const client = mqtt.connect(config.mqttUrl, {
  username: config.mqttUsername,
  password: config.mqttPassword,

  reconnectPeriod: 2000,
  connectTimeout: 10000,
  clean: true,
  keepalive: 60,
});

client.on("connect", () => {
  console.log("MQTT connected");
});

client.on("reconnect", () => {
  console.log("MQTT reconnecting");
});

client.on("error", (err) => {
  console.error("MQTT error", err);
});