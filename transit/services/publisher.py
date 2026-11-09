import json
import logging
from collections import defaultdict

import paho.mqtt.client as mqtt

from transit.db import connect_db


class MqttPublisher:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.client = None
        self._routes_by_id: dict[str, dict] = {}
        self._route_metadata_loaded = False

    def start(self):
        if not self.settings.mqtt_enabled:
            logging.info("MQTT disabled for city=%s", self.settings.city_code)
            return

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

        mqtt_username = getattr(self.settings, "mqtt_username", "") or ""
        mqtt_password = getattr(self.settings, "mqtt_password", "") or ""
        if mqtt_username:
            client.username_pw_set(mqtt_username, mqtt_password)

        def on_connect(client, userdata, flags, reason_code, properties):
            logging.info("MQTT connected city=%s reason_code=%s", self.settings.city_code, reason_code)

        def on_disconnect(client, userdata, flags, reason_code, properties):
            logging.warning("MQTT disconnected city=%s reason_code=%s", self.settings.city_code, reason_code)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.connect(self.settings.mqtt_host, self.settings.mqtt_port, self.settings.mqtt_keepalive)
        client.loop_start()
        self.client = client

    def stop(self):
        if self.client:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass

    def _load_route_metadata(self) -> None:
        if self._route_metadata_loaded:
            return

        self._route_metadata_loaded = True

        try:
            with connect_db(self.settings.db_config) as conn:
                rows = conn.execute(
                    """
                    SELECT r.route_id, r.route_short_name, r.route_long_name, r.route_color, r.route_text_color, r.route_type
                    FROM routes r
                    JOIN cities c ON c.city_id = r.city_id
                    WHERE c.city_code = %s
                    """,
                    (self.settings.city_code,),
                ).fetchall()
        except Exception:
            logging.exception("Failed to load route metadata for MQTT vehicle payloads city=%s", self.settings.city_code)
            rows = []

        self._routes_by_id = {
            str(row[0]): {
                "route_short_name": str(row[1]) if row[1] is not None else None,
                "route_long_name": str(row[2]) if row[2] is not None else None,
                "route_color": str(row[3]) if row[3] is not None else None,
                "route_text_color": str(row[4]) if row[4] is not None else None,
                "route_type": int(row[5]) if row[5] is not None else None,
            }
            for row in rows
            if row[0] is not None
        }

    def _vehicle_payload(self, vehicle: dict) -> dict:
        route_id = vehicle.get("route_id")
        route_meta = self._routes_by_id.get(str(route_id), {}) if route_id is not None else {}

        return {
            "trip_instance_id": vehicle.get("trip_instance_id"),
            "trip_pk": vehicle.get("trip_pk"),
            "trip_id": vehicle.get("trip_id"),
            "route_id": route_id,
            "route_short_name": route_meta.get("route_short_name"),
            "route_long_name": route_meta.get("route_long_name"),
            "route_color": route_meta.get("route_color"),
            "route_text_color": route_meta.get("route_text_color"),
            "route_type": route_meta.get("route_type"),
            "service_id": vehicle.get("service_id"),
            "shape_id": vehicle.get("shape_id"),
            "status": vehicle.get("status"),
            "source": vehicle.get("source"),
            "vehicle_id": vehicle.get("vehicle_id"),
            "vehicle_label": vehicle.get("vehicle_label"),
            "raw_rt_trip_id": vehicle.get("raw_rt_trip_id"),
            "start_date": vehicle.get("start_date"),
            "start_time": vehicle.get("start_time"),
            "position_timestamp": vehicle.get("position_timestamp"),
            "match_strategy": vehicle.get("match_strategy"),
            "match_confidence": vehicle.get("match_confidence"),
            "lat": vehicle.get("lat"),
            "lon": vehicle.get("lon"),
            "trip_progress": vehicle.get("trip_progress"),
            "current_stop_sequence": vehicle.get("current_stop_sequence"),
            "next_stop_sequence": vehicle.get("next_stop_sequence"),
            "previous_stop_sequence": vehicle.get("previous_stop_sequence"),
            "scheduled_delay_seconds": vehicle.get("scheduled_delay_seconds"),
            "adjusted_progress": vehicle.get("adjusted_progress"),
            "raw_lat": vehicle.get("raw_lat"),
            "raw_lon": vehicle.get("raw_lon"),
            "matched_lat": vehicle.get("matched_lat"),
            "matched_lon": vehicle.get("matched_lon"),
            "distance_from_shape_m": vehicle.get("distance_from_shape_m"),
            "shape_dist_traveled": vehicle.get("shape_dist_traveled"),
            "bearing_error_deg": vehicle.get("bearing_error_deg"),
            "bearing": vehicle.get("bearing"),
            "fusion_score": vehicle.get("fusion_score"),
            "match_reasons": vehicle.get("match_reasons"),
            "is_position_snapped": vehicle.get("is_position_snapped"),
            "is_position_smoothed": vehicle.get("is_position_smoothed"),
            "jump_distance_m": vehicle.get("jump_distance_m"),
            "smoothing_reset_reason": vehicle.get("smoothing_reset_reason"),
        }

    def _publish_json(self, topic: str, payload: dict, retain: bool = False) -> bool:
        payload_json = json.dumps(payload, ensure_ascii=False)
        info = self.client.publish(topic, payload_json, qos=0, retain=retain)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def publish_vehicle_states(self, vehicles: list[dict], updated_at):
        if not self.settings.mqtt_enabled or not self.client:
            return

        prefix = self.settings.mqtt_topic_prefix.rstrip("/")
        city = self.settings.city_code
        updated_at_iso = updated_at.isoformat()

        self._load_route_metadata()

        route_groups = defaultdict(list)
        normalized_vehicles = []

        for vehicle in vehicles:
            payload = self._vehicle_payload(vehicle)
            normalized_vehicles.append(payload)

            route_id = payload.get("route_id") or "_unknown"
            route_groups[str(route_id)].append(payload)

        city_meta_topic = f"{prefix}/{city}/meta"
        city_snapshot_topic = f"{prefix}/{city}/vehicles/snapshot"
        legacy_city_snapshot_topic = f"{prefix}/{city}/vehicles_snapshot"

        city_meta_payload = {
            "city": city,
            "updated_at": updated_at_iso,
            "vehicle_count": len(normalized_vehicles),
            "route_count": len(route_groups),
            "schema_version": 5,
        }

        city_snapshot_payload = {
            "city": city,
            "updated_at": updated_at_iso,
            "vehicle_count": len(normalized_vehicles),
            "vehicles": normalized_vehicles,
        }

        published_city_meta = self._publish_json(city_meta_topic, city_meta_payload, retain=True)
        published_city_snapshot = self._publish_json(city_snapshot_topic, city_snapshot_payload, retain=True)

        # Transition topic so existing consumers do not break immediately.
        self._publish_json(legacy_city_snapshot_topic, city_snapshot_payload, retain=True)

        published_route_topics = 0
        published_route_meta_topics = 0

        for route_id, route_vehicles in route_groups.items():
            route_meta_topic = f"{prefix}/{city}/routes/{route_id}/meta"
            route_snapshot_topic = f"{prefix}/{city}/routes/{route_id}/vehicles/snapshot"

            route_meta_payload = {
                "city": city,
                "route_id": route_id,
                "updated_at": updated_at_iso,
                "vehicle_count": len(route_vehicles),
                "schema_version": 5,
            }

            route_snapshot_payload = {
                "city": city,
                "route_id": route_id,
                "updated_at": updated_at_iso,
                "vehicle_count": len(route_vehicles),
                "vehicles": route_vehicles,
            }

            if self._publish_json(route_meta_topic, route_meta_payload, retain=True):
                published_route_meta_topics += 1

            if self._publish_json(route_snapshot_topic, route_snapshot_payload, retain=True):
                published_route_topics += 1

        logging.info(
            "MQTT published city=%s city_meta=%s city_snapshot=%s route_meta_topics=%s route_snapshot_topics=%s vehicles=%s routes=%s",
            city,
            int(bool(published_city_meta)),
            int(bool(published_city_snapshot)),
            published_route_meta_topics,
            published_route_topics,
            len(normalized_vehicles),
            len(route_groups),
        )
