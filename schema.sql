BEGIN;

DROP TABLE IF EXISTS gtfs_rt_stop_time_updates_current CASCADE;
DROP TABLE IF EXISTS gtfs_rt_trip_updates_current CASCADE;
DROP TABLE IF EXISTS gtfs_rt_alerts_current CASCADE;
DROP TABLE IF EXISTS gtfs_rt_stop_time_updates CASCADE;
DROP TABLE IF EXISTS gtfs_rt_trip_updates CASCADE;
DROP TABLE IF EXISTS gtfs_rt_vehicle_positions CASCADE;
DROP TABLE IF EXISTS gtfs_rt_alerts CASCADE;
DROP TABLE IF EXISTS gtfs_rt_feed_snapshots CASCADE;
DROP TABLE IF EXISTS gtfs_rt_sources CASCADE;
DROP TABLE IF EXISTS city_runtime_config CASCADE;

DROP TABLE IF EXISTS vehicle_states_simulated_current CASCADE;
DROP TABLE IF EXISTS vehicle_states_current CASCADE;
DROP TABLE IF EXISTS frequencies CASCADE;
DROP TABLE IF EXISTS stop_times CASCADE;
DROP TABLE IF EXISTS trips CASCADE;
DROP TABLE IF EXISTS shape_points CASCADE;
DROP TABLE IF EXISTS shapes CASCADE;
DROP TABLE IF EXISTS calendar_dates CASCADE;
DROP TABLE IF EXISTS calendar CASCADE;
DROP TABLE IF EXISTS services CASCADE;
DROP TABLE IF EXISTS routes CASCADE;
DROP TABLE IF EXISTS stops CASCADE;
DROP TABLE IF EXISTS gtfs_import_runs CASCADE;
DROP TABLE IF EXISTS cities CASCADE;
DROP TABLE IF EXISTS runtime_partitions CASCADE;

-- ============================================
-- Cities
-- ============================================
CREATE TABLE cities (
    city_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_code    TEXT NOT NULL UNIQUE,
    city_name    TEXT NOT NULL,
    timezone     TEXT
);

-- ============================================
-- GTFS import runs
-- ============================================
CREATE TABLE gtfs_import_runs (
    import_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_id        BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at    TIMESTAMPTZ,
    status         TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    feed_hash      TEXT,
    error_message  TEXT
);

CREATE INDEX idx_gtfs_import_runs_city_id ON gtfs_import_runs(city_id);

-- ============================================
-- City runtime config
-- ============================================
CREATE TABLE city_runtime_config (
    city_id                     BIGINT PRIMARY KEY REFERENCES cities(city_id) ON DELETE CASCADE,
    timezone_name               TEXT NOT NULL DEFAULT 'UTC',
    service_day_rollover_hour   INTEGER NOT NULL DEFAULT 4,
    is_enabled                  BOOLEAN NOT NULL DEFAULT TRUE,
    gtfs_import_in_progress     BOOLEAN NOT NULL DEFAULT FALSE,
    sim_enabled                 BOOLEAN NOT NULL DEFAULT TRUE,
    sim_force_date              TEXT,
    sim_force_time              TEXT,
    gtfs_url                    TEXT,
    gtfs_refresh_interval_hours INTEGER NOT NULL DEFAULT 24,
    gtfs_last_refreshed_at      TIMESTAMPTZ,
    gtfs_last_refresh_status    TEXT,
    gtfs_url_encrypted          BOOLEAN NOT NULL DEFAULT FALSE,

    -- Optional per-city runtime overrides. NULL means use .env default.
    sim_interval_seconds                    INTEGER,
    sim_active_trip_lookbehind_seconds      INTEGER,
    sim_active_trip_lookahead_seconds       INTEGER,
    sim_apply_trip_updates                  BOOLEAN,

    fusion_interval_seconds                 INTEGER,
    fusion_rt_max_age_seconds               INTEGER,
    fusion_min_match_score                  DOUBLE PRECISION,
    fusion_max_shape_distance_m             DOUBLE PRECISION,

    stop_arrivals_interval_seconds          INTEGER,
    stop_arrivals_horizon_seconds           INTEGER,
    stop_arrivals_limit_per_stop            INTEGER,
    stop_arrivals_shape_speed_mps           DOUBLE PRECISION,
    stop_arrivals_shape_max_distance_m      DOUBLE PRECISION,
    stop_arrivals_shape_passed_grace_m      DOUBLE PRECISION
);

-- ============================================
-- GTFS-RT source configuration
-- ============================================
CREATE TABLE gtfs_rt_sources (
    rt_source_pk BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_id BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    feed_type TEXT NOT NULL CHECK (feed_type IN ('vehicle_positions', 'trip_updates', 'alerts')),
    source_kind TEXT NOT NULL DEFAULT 'url',
    source_value TEXT NOT NULL,
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    refresh_interval_seconds INTEGER NOT NULL DEFAULT 30,
    headers_json JSONB,
    query_params_json JSONB,
    source_encrypted BOOLEAN NOT NULL DEFAULT FALSE,
    notes TEXT,
    CONSTRAINT ux_gtfs_rt_sources_city_feed_type UNIQUE (city_id, feed_type)
);

CREATE INDEX idx_gtfs_rt_sources_city_id ON gtfs_rt_sources(city_id);
CREATE INDEX idx_gtfs_rt_sources_enabled ON gtfs_rt_sources(is_enabled);

-- ============================================
-- Stops
-- ============================================
CREATE TABLE stops (
    stop_pk             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_id             BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    stop_id             TEXT NOT NULL,
    stop_code           TEXT,
    stop_name           TEXT NOT NULL,
    stop_desc           TEXT,
    stop_lat            DOUBLE PRECISION NOT NULL,
    stop_lon            DOUBLE PRECISION NOT NULL,
    location_type       INTEGER,
    parent_station_id   TEXT,
    parent_stop_pk      BIGINT,
    zone_id             TEXT,
    stop_url            TEXT,
    stop_timezone       TEXT,
    wheelchair_boarding INTEGER,
    CONSTRAINT ux_stops_city_stop_id UNIQUE (city_id, stop_id),
    CONSTRAINT fk_stops_parent_stop_pk
        FOREIGN KEY (parent_stop_pk) REFERENCES stops(stop_pk) ON DELETE SET NULL
);

CREATE INDEX idx_stops_city_id ON stops(city_id);
CREATE INDEX idx_stops_city_stop_name ON stops(city_id, stop_name);

-- ============================================
-- Routes
-- ============================================
CREATE TABLE routes (
    route_pk          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_id           BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    route_id          TEXT NOT NULL,
    agency_id         TEXT,
    route_short_name  TEXT,
    route_long_name   TEXT,
    route_desc        TEXT,
    route_type        INTEGER,
    route_color       TEXT,
    route_text_color  TEXT,
    route_url         TEXT,
    CONSTRAINT ux_routes_city_route_id UNIQUE (city_id, route_id)
);

CREATE INDEX idx_routes_city_id ON routes(city_id);
CREATE INDEX idx_routes_city_route_short_name ON routes(city_id, route_short_name);

-- ============================================
-- Services
-- ============================================
CREATE TABLE services (
    service_pk   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_id      BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    service_id   TEXT NOT NULL,
    CONSTRAINT ux_services_city_service_id UNIQUE (city_id, service_id)
);

CREATE INDEX idx_services_city_id ON services(city_id);

-- ============================================
-- Calendar
-- ============================================
CREATE TABLE calendar (
    service_pk   BIGINT PRIMARY KEY REFERENCES services(service_pk) ON DELETE CASCADE,
    monday       INTEGER NOT NULL CHECK (monday IN (0,1)),
    tuesday      INTEGER NOT NULL CHECK (tuesday IN (0,1)),
    wednesday    INTEGER NOT NULL CHECK (wednesday IN (0,1)),
    thursday     INTEGER NOT NULL CHECK (thursday IN (0,1)),
    friday       INTEGER NOT NULL CHECK (friday IN (0,1)),
    saturday     INTEGER NOT NULL CHECK (saturday IN (0,1)),
    sunday       INTEGER NOT NULL CHECK (sunday IN (0,1)),
    start_date   TEXT NOT NULL,
    end_date     TEXT NOT NULL
);

CREATE INDEX idx_calendar_start_end ON calendar(start_date, end_date);

-- ============================================
-- Calendar dates
-- ============================================
CREATE TABLE calendar_dates (
    calendar_date_pk BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    service_pk       BIGINT NOT NULL REFERENCES services(service_pk) ON DELETE CASCADE,
    date             TEXT NOT NULL,
    exception_type   INTEGER NOT NULL CHECK (exception_type IN (1,2)),
    CONSTRAINT ux_calendar_dates_service_date UNIQUE (service_pk, date)
);

CREATE INDEX idx_calendar_dates_date ON calendar_dates(date);

-- ============================================
-- Shapes
-- ============================================
CREATE TABLE shapes (
    shape_pk   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_id    BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    shape_id   TEXT NOT NULL,
    CONSTRAINT ux_shapes_city_shape_id UNIQUE (city_id, shape_id)
);

CREATE INDEX idx_shapes_city_id ON shapes(city_id);

-- ============================================
-- Shape points
-- ============================================
CREATE TABLE shape_points (
    shape_point_pk        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shape_pk              BIGINT NOT NULL REFERENCES shapes(shape_pk) ON DELETE CASCADE,
    shape_pt_sequence     INTEGER NOT NULL,
    shape_pt_lat          DOUBLE PRECISION NOT NULL,
    shape_pt_lon          DOUBLE PRECISION NOT NULL,
    shape_dist_traveled   DOUBLE PRECISION,
    CONSTRAINT ux_shape_points_shape_seq UNIQUE (shape_pk, shape_pt_sequence)
);

CREATE INDEX idx_shape_points_shape_pk ON shape_points(shape_pk);

-- ============================================
-- Trips
-- ============================================
CREATE TABLE trips (
    trip_pk                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_id                BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    trip_id                TEXT NOT NULL,
    route_pk               BIGINT NOT NULL REFERENCES routes(route_pk) ON DELETE CASCADE,
    service_pk             BIGINT NOT NULL REFERENCES services(service_pk) ON DELETE CASCADE,
    shape_pk               BIGINT REFERENCES shapes(shape_pk) ON DELETE SET NULL,
    trip_headsign          TEXT,
    trip_short_name        TEXT,
    direction_id           INTEGER,
    block_id               TEXT,
    wheelchair_accessible  INTEGER,
    bikes_allowed          INTEGER,
    first_departure_seconds INTEGER,
    last_arrival_seconds    INTEGER,
    CONSTRAINT ux_trips_city_trip_id UNIQUE (city_id, trip_id)
);

CREATE INDEX idx_trips_city_id ON trips(city_id);
CREATE INDEX idx_trips_route_pk ON trips(route_pk);
CREATE INDEX idx_trips_service_pk ON trips(service_pk);
CREATE INDEX idx_trips_shape_pk ON trips(shape_pk);
CREATE INDEX idx_trips_first_departure_seconds ON trips(first_departure_seconds);
CREATE INDEX idx_trips_last_arrival_seconds ON trips(last_arrival_seconds);
CREATE INDEX idx_trips_city_service_time_window
    ON trips(city_id, service_pk, first_departure_seconds, last_arrival_seconds);

-- ============================================
-- Stop times
-- ============================================
CREATE TABLE stop_times (
    stop_time_pk           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trip_pk                BIGINT NOT NULL REFERENCES trips(trip_pk) ON DELETE CASCADE,
    stop_pk                BIGINT NOT NULL REFERENCES stops(stop_pk) ON DELETE CASCADE,
    arrival_time           TEXT,
    departure_time         TEXT,
    arrival_seconds        INTEGER,
    departure_seconds      INTEGER,
    stop_sequence          INTEGER NOT NULL,
    stop_headsign          TEXT,
    pickup_type            INTEGER,
    drop_off_type          INTEGER,
    shape_dist_traveled    DOUBLE PRECISION,
    timepoint              INTEGER,
    CONSTRAINT ux_stop_times_trip_seq UNIQUE (trip_pk, stop_sequence)
);

CREATE INDEX idx_stop_times_trip_pk ON stop_times(trip_pk);
CREATE INDEX idx_stop_times_stop_pk ON stop_times(stop_pk);
CREATE INDEX idx_stop_times_arrival_time ON stop_times(arrival_time);
CREATE INDEX idx_stop_times_departure_time ON stop_times(departure_time);
CREATE INDEX idx_stop_times_arrival_seconds ON stop_times(arrival_seconds);
CREATE INDEX idx_stop_times_departure_seconds ON stop_times(departure_seconds);
CREATE INDEX idx_stop_times_trip_time_window ON stop_times(trip_pk, arrival_seconds, departure_seconds);

-- ============================================
-- Frequencies
-- ============================================
CREATE TABLE frequencies (
    frequency_pk    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trip_pk         BIGINT NOT NULL REFERENCES trips(trip_pk) ON DELETE CASCADE,
    start_time      TEXT NOT NULL,
    end_time        TEXT NOT NULL,
    headway_secs    INTEGER NOT NULL,
    exact_times     INTEGER,
    CONSTRAINT ux_frequencies_trip_window UNIQUE (trip_pk, start_time, end_time, headway_secs)
);

CREATE INDEX idx_frequencies_trip_pk ON frequencies(trip_pk);

-- ============================================
-- Runtime current vehicle state
-- ============================================
CREATE TABLE vehicle_states_current (
    vehicle_state_pk    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_id             BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    trip_instance_id    TEXT NOT NULL,
    trip_pk             BIGINT REFERENCES trips(trip_pk) ON DELETE SET NULL,
    route_pk            BIGINT REFERENCES routes(route_pk) ON DELETE SET NULL,
    service_pk          BIGINT REFERENCES services(service_pk) ON DELETE SET NULL,
    shape_pk            BIGINT REFERENCES shapes(shape_pk) ON DELETE SET NULL,
    trip_id             TEXT,
    route_id            TEXT,
    service_id          TEXT,
    shape_id            TEXT,
    status              TEXT NOT NULL,
    source              TEXT NOT NULL,
    vehicle_id          TEXT,
    vehicle_label       TEXT,
    raw_rt_trip_id      TEXT,
    start_date          TEXT,
    start_time          TEXT,
    position_timestamp  BIGINT,
    match_strategy      TEXT,
    match_confidence    DOUBLE PRECISION,
    lat                 DOUBLE PRECISION NOT NULL,
    lon                 DOUBLE PRECISION NOT NULL,
    trip_progress       DOUBLE PRECISION,
    current_stop_sequence INTEGER,
    next_stop_sequence  INTEGER,
    previous_stop_sequence INTEGER,
    scheduled_delay_seconds INTEGER,
    adjusted_progress   DOUBLE PRECISION,
    updated_at          TIMESTAMPTZ NOT NULL,
    raw_lat             DOUBLE PRECISION,
    raw_lon             DOUBLE PRECISION,
    matched_lat         DOUBLE PRECISION,
    matched_lon         DOUBLE PRECISION,
    distance_from_shape_m DOUBLE PRECISION,
    shape_dist_traveled DOUBLE PRECISION,
    bearing_error_deg   DOUBLE PRECISION,
    fusion_score        DOUBLE PRECISION,
    match_reasons       TEXT,
    is_position_snapped BOOLEAN DEFAULT FALSE,
    is_position_smoothed BOOLEAN DEFAULT FALSE,
    jump_distance_m     DOUBLE PRECISION,
    smoothing_reset_reason TEXT,
    bearing DOUBLE PRECISION,
    CONSTRAINT ux_vehicle_states_city_trip_instance UNIQUE (city_id, trip_instance_id)
);

CREATE INDEX idx_vehicle_states_city_id ON vehicle_states_current(city_id);
CREATE INDEX idx_vehicle_states_trip_pk ON vehicle_states_current(trip_pk);
CREATE INDEX idx_vehicle_states_route_pk ON vehicle_states_current(route_pk);
CREATE INDEX idx_vehicle_states_updated_at ON vehicle_states_current(updated_at);
CREATE INDEX idx_vehicle_states_vehicle_id ON vehicle_states_current(city_id, vehicle_id);
CREATE INDEX idx_vehicle_states_match_strategy ON vehicle_states_current(city_id, match_strategy);
CREATE INDEX idx_vehicle_states_fusion_score ON vehicle_states_current(city_id, fusion_score DESC);
CREATE INDEX idx_vehicle_states_distance_from_shape ON vehicle_states_current(city_id, distance_from_shape_m);
CREATE INDEX idx_vehicle_states_snapped ON vehicle_states_current(city_id, is_position_snapped);
CREATE INDEX idx_vehicle_states_current_stop_sequence ON vehicle_states_current(city_id, current_stop_sequence);
CREATE INDEX idx_vehicle_states_current_delay ON vehicle_states_current(city_id, scheduled_delay_seconds);

-- ============================================
-- ============================================
-- Runtime partitions
-- ============================================
-- The GTFS static tables above remain normal tables. The high-volume runtime
-- tables below are partitioned by timestamp so cleanup can DROP old partitions
-- instead of DELETE millions of rows.

-- ============================================
-- Vehicle position history for smoothing
-- ============================================
CREATE TABLE vehicle_position_history (
    city_id           BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    vehicle_key       TEXT NOT NULL,
    observed_at       TIMESTAMPTZ NOT NULL,
    lat               DOUBLE PRECISION NOT NULL,
    lon               DOUBLE PRECISION NOT NULL,
    raw_lat           DOUBLE PRECISION,
    raw_lon           DOUBLE PRECISION,
    matched_lat       DOUBLE PRECISION,
    matched_lon       DOUBLE PRECISION,
    source            TEXT NOT NULL,
    match_strategy    TEXT,
    fusion_score      DOUBLE PRECISION,
    PRIMARY KEY (city_id, vehicle_key, observed_at)
) PARTITION BY RANGE (observed_at);

CREATE TABLE vehicle_position_history_default
    PARTITION OF vehicle_position_history DEFAULT;

CREATE INDEX idx_vehicle_position_history_latest
    ON vehicle_position_history(city_id, vehicle_key, observed_at DESC);
CREATE INDEX idx_vehicle_position_history_cleanup
    ON vehicle_position_history(observed_at);

-- ============================================
-- GTFS-RT feed snapshots
-- ============================================
-- This table stays unpartitioned because it is small and is referenced by the
-- high-volume RT tables. Its old rows are cleaned after dependent rows are gone.
CREATE TABLE gtfs_rt_feed_snapshots (
    snapshot_pk            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_id                BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    feed_type              TEXT NOT NULL CHECK (feed_type IN ('vehicle_positions', 'trip_updates', 'alerts')),
    feed_timestamp         BIGINT,
    gtfs_realtime_version  TEXT,
    fetched_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    entity_count           INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_gtfs_rt_feed_snapshots_city_type_time
    ON gtfs_rt_feed_snapshots(city_id, feed_type, fetched_at DESC);

-- ============================================
-- GTFS-RT vehicle positions, partitioned by created_at
-- ============================================
CREATE TABLE gtfs_rt_vehicle_positions (
    rt_vehicle_pk          BIGINT GENERATED ALWAYS AS IDENTITY,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    city_id                BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    snapshot_pk            BIGINT NOT NULL REFERENCES gtfs_rt_feed_snapshots(snapshot_pk) ON DELETE CASCADE,
    entity_id              TEXT,
    vehicle_id             TEXT,
    vehicle_label          TEXT,
    trip_id                TEXT,
    route_id               TEXT,
    stop_id                TEXT,
    start_date             TEXT,
    start_time             TEXT,
    schedule_relationship  INTEGER,
    trip_pk                BIGINT REFERENCES trips(trip_pk) ON DELETE SET NULL,
    route_pk               BIGINT REFERENCES routes(route_pk) ON DELETE SET NULL,
    latitude               DOUBLE PRECISION NOT NULL,
    longitude              DOUBLE PRECISION NOT NULL,
    bearing                DOUBLE PRECISION,
    speed                  DOUBLE PRECISION,
    current_stop_sequence  INTEGER,
    current_status         INTEGER,
    congestion_level       INTEGER,
    occupancy_status       INTEGER,
    position_timestamp     BIGINT,
    raw_json               JSONB
) PARTITION BY RANGE (created_at);

CREATE TABLE gtfs_rt_vehicle_positions_default
    PARTITION OF gtfs_rt_vehicle_positions DEFAULT;

CREATE INDEX idx_gtfs_rt_vehicle_positions_city_snapshot
    ON gtfs_rt_vehicle_positions(city_id, snapshot_pk);
CREATE INDEX idx_gtfs_rt_vehicle_positions_trip_id
    ON gtfs_rt_vehicle_positions(city_id, trip_id);
CREATE INDEX idx_gtfs_rt_vehicle_positions_vehicle_id
    ON gtfs_rt_vehicle_positions(city_id, vehicle_id);
CREATE INDEX idx_gtfs_rt_vehicle_positions_created_at
    ON gtfs_rt_vehicle_positions(created_at);

-- ============================================
-- GTFS-RT trip updates, partitioned by created_at
-- ============================================
CREATE TABLE gtfs_rt_trip_updates (
    rt_trip_update_pk      BIGINT GENERATED ALWAYS AS IDENTITY,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    city_id                BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    snapshot_pk            BIGINT NOT NULL REFERENCES gtfs_rt_feed_snapshots(snapshot_pk) ON DELETE CASCADE,
    entity_id              TEXT,
    trip_id                TEXT,
    route_id               TEXT,
    start_date             TEXT,
    start_time             TEXT,
    schedule_relationship  INTEGER,
    vehicle_id             TEXT,
    vehicle_label          TEXT,
    trip_delay_seconds     INTEGER,
    trip_timestamp         BIGINT,
    trip_pk                BIGINT REFERENCES trips(trip_pk) ON DELETE SET NULL,
    route_pk               BIGINT REFERENCES routes(route_pk) ON DELETE SET NULL,
    raw_json               JSONB
) PARTITION BY RANGE (created_at);

CREATE TABLE gtfs_rt_trip_updates_default
    PARTITION OF gtfs_rt_trip_updates DEFAULT;

CREATE INDEX idx_gtfs_rt_trip_updates_city_snapshot
    ON gtfs_rt_trip_updates(city_id, snapshot_pk);
CREATE INDEX idx_gtfs_rt_trip_updates_trip_id
    ON gtfs_rt_trip_updates(city_id, trip_id);
CREATE INDEX idx_gtfs_rt_trip_updates_created_at
    ON gtfs_rt_trip_updates(created_at);
CREATE INDEX idx_gtfs_rt_trip_updates_pk_lookup
    ON gtfs_rt_trip_updates(rt_trip_update_pk);

-- ============================================
-- GTFS-RT stop time updates, partitioned by created_at
-- ============================================
-- city_id/snapshot_pk/created_at are intentionally denormalized here so this
-- child-heavy table can be partitioned and cleaned independently. We do not use
-- an FK to gtfs_rt_trip_updates because PostgreSQL partition uniqueness would
-- require the partition key in the referenced key.
CREATE TABLE gtfs_rt_stop_time_updates (
    rt_stop_time_update_pk    BIGINT GENERATED ALWAYS AS IDENTITY,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    city_id                   BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    snapshot_pk               BIGINT NOT NULL REFERENCES gtfs_rt_feed_snapshots(snapshot_pk) ON DELETE CASCADE,
    rt_trip_update_pk         BIGINT NOT NULL,
    stop_sequence             INTEGER,
    stop_id                   TEXT,
    arrival_delay_seconds     INTEGER,
    arrival_time_epoch        BIGINT,
    departure_delay_seconds   INTEGER,
    departure_time_epoch      BIGINT,
    schedule_relationship     INTEGER
) PARTITION BY RANGE (created_at);

CREATE TABLE gtfs_rt_stop_time_updates_default
    PARTITION OF gtfs_rt_stop_time_updates DEFAULT;

CREATE INDEX idx_gtfs_rt_stop_time_updates_trip_update
    ON gtfs_rt_stop_time_updates(rt_trip_update_pk);
CREATE INDEX idx_gtfs_rt_stop_time_updates_city_snapshot
    ON gtfs_rt_stop_time_updates(city_id, snapshot_pk);
CREATE INDEX idx_gtfs_rt_stop_time_updates_created_at
    ON gtfs_rt_stop_time_updates(created_at);

-- ============================================
-- GTFS-RT current trip updates (small operational cache)
-- ============================================
-- History tables above remain partitioned for debugging/retention. Runtime services
-- should read these current tables instead of repeatedly scanning history partitions.
CREATE TABLE gtfs_rt_trip_updates_current (
    city_id                BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    trip_key               TEXT NOT NULL,
    snapshot_pk            BIGINT REFERENCES gtfs_rt_feed_snapshots(snapshot_pk) ON DELETE SET NULL,
    entity_id              TEXT,
    trip_id                TEXT,
    route_id               TEXT,
    start_date             TEXT,
    start_time             TEXT,
    schedule_relationship  INTEGER,
    vehicle_id             TEXT,
    vehicle_label          TEXT,
    trip_delay_seconds     INTEGER,
    trip_timestamp         BIGINT,
    feed_timestamp         BIGINT,
    trip_pk                BIGINT REFERENCES trips(trip_pk) ON DELETE SET NULL,
    route_pk               BIGINT REFERENCES routes(route_pk) ON DELETE SET NULL,
    fetched_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (city_id, trip_key)
);

CREATE INDEX idx_gtfs_rt_trip_updates_current_trip_pk
    ON gtfs_rt_trip_updates_current(city_id, trip_pk);
CREATE INDEX idx_gtfs_rt_trip_updates_current_trip_id
    ON gtfs_rt_trip_updates_current(city_id, trip_id);
CREATE INDEX idx_gtfs_rt_trip_updates_current_vehicle_id
    ON gtfs_rt_trip_updates_current(city_id, vehicle_id);
CREATE INDEX idx_gtfs_rt_trip_updates_current_snapshot
    ON gtfs_rt_trip_updates_current(city_id, snapshot_pk);
CREATE INDEX idx_gtfs_rt_trip_updates_current_updated_at
    ON gtfs_rt_trip_updates_current(updated_at);

CREATE TABLE gtfs_rt_stop_time_updates_current (
    city_id                   BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    trip_key                  TEXT NOT NULL,
    stop_update_key           TEXT NOT NULL,
    stop_sequence             INTEGER,
    stop_id                   TEXT,
    arrival_delay_seconds     INTEGER,
    arrival_time_epoch        BIGINT,
    departure_delay_seconds   INTEGER,
    departure_time_epoch      BIGINT,
    schedule_relationship     INTEGER,
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (city_id, trip_key, stop_update_key)
);

CREATE INDEX idx_gtfs_rt_stop_time_updates_current_trip_key
    ON gtfs_rt_stop_time_updates_current(city_id, trip_key);
CREATE INDEX idx_gtfs_rt_stop_time_updates_current_stop_sequence
    ON gtfs_rt_stop_time_updates_current(city_id, trip_key, stop_sequence);
CREATE INDEX idx_gtfs_rt_stop_time_updates_current_stop_id
    ON gtfs_rt_stop_time_updates_current(city_id, trip_key, stop_id);
CREATE INDEX idx_gtfs_rt_stop_time_updates_current_updated_at
    ON gtfs_rt_stop_time_updates_current(updated_at);



-- ============================================
-- GTFS-RT alerts, partitioned by created_at
-- ============================================
CREATE TABLE gtfs_rt_alerts (
    rt_alert_pk               BIGINT GENERATED ALWAYS AS IDENTITY,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    city_id                   BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    snapshot_pk               BIGINT NOT NULL REFERENCES gtfs_rt_feed_snapshots(snapshot_pk) ON DELETE CASCADE,
    entity_id                 TEXT,
    cause                     INTEGER,
    effect                    INTEGER,
    url_translations          JSONB,
    header_translations       JSONB,
    description_translations  JSONB,
    active_periods            JSONB,
    informed_entities         JSONB,
    raw_json                  JSONB
) PARTITION BY RANGE (created_at);

CREATE TABLE gtfs_rt_alerts_default
    PARTITION OF gtfs_rt_alerts DEFAULT;

CREATE INDEX idx_gtfs_rt_alerts_city_snapshot
    ON gtfs_rt_alerts(city_id, snapshot_pk);
CREATE INDEX idx_gtfs_rt_alerts_created_at
    ON gtfs_rt_alerts(created_at);

-- ============================================
-- GTFS-RT current alerts (small operational cache)
-- ============================================
CREATE TABLE gtfs_rt_alerts_current (
    city_id                   BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    entity_id                 TEXT NOT NULL,
    cause                     INTEGER,
    effect                    INTEGER,
    url_translations          JSONB,
    header_translations       JSONB,
    description_translations  JSONB,
    active_periods            JSONB,
    informed_entities         JSONB,
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (city_id, entity_id)
);

CREATE INDEX idx_gtfs_rt_alerts_current_city_id
    ON gtfs_rt_alerts_current(city_id);
CREATE INDEX idx_gtfs_rt_alerts_current_updated_at
    ON gtfs_rt_alerts_current(updated_at);

-- Registry used by partition_manager_service.py and cleanup_service.py.
CREATE TABLE IF NOT EXISTS runtime_partitions (
    parent_table TEXT NOT NULL,
    partition_name TEXT NOT NULL PRIMARY KEY,
    range_start TIMESTAMPTZ NOT NULL,
    range_end TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_runtime_partitions_parent_start
    ON runtime_partitions(parent_table, range_start);

-- Simulated vehicle state
-- ============================================
CREATE TABLE vehicle_states_simulated_current (
    vehicle_state_pk    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_id             BIGINT NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    trip_instance_id    TEXT NOT NULL,
    trip_pk             BIGINT REFERENCES trips(trip_pk) ON DELETE SET NULL,
    route_pk            BIGINT REFERENCES routes(route_pk) ON DELETE SET NULL,
    service_pk          BIGINT REFERENCES services(service_pk) ON DELETE SET NULL,
    shape_pk            BIGINT REFERENCES shapes(shape_pk) ON DELETE SET NULL,
    trip_id             TEXT,
    route_id            TEXT,
    service_id          TEXT,
    shape_id            TEXT,
    status              TEXT NOT NULL,
    source              TEXT NOT NULL,
    lat                 DOUBLE PRECISION NOT NULL,
    lon                 DOUBLE PRECISION NOT NULL,
    trip_progress       DOUBLE PRECISION,
    updated_at          TIMESTAMPTZ NOT NULL,
    current_stop_sequence INTEGER,
    next_stop_sequence  INTEGER,
    previous_stop_sequence INTEGER,
    scheduled_delay_seconds INTEGER,
    adjusted_progress   DOUBLE PRECISION,
    CONSTRAINT ux_vehicle_states_sim_city_trip_instance UNIQUE (city_id, trip_instance_id)
);

CREATE INDEX idx_vehicle_states_sim_city_id
    ON vehicle_states_simulated_current(city_id);
CREATE INDEX idx_vehicle_states_sim_trip_pk
    ON vehicle_states_simulated_current(trip_pk);
CREATE INDEX idx_vehicle_states_sim_route_pk
    ON vehicle_states_simulated_current(route_pk);
CREATE INDEX idx_vehicle_states_sim_updated_at
    ON vehicle_states_simulated_current(updated_at);
CREATE INDEX idx_vehicle_states_sim_stop_sequence
    ON vehicle_states_simulated_current(city_id, current_stop_sequence);
CREATE INDEX idx_vehicle_states_sim_delay
    ON vehicle_states_simulated_current(city_id, scheduled_delay_seconds);

COMMIT;

-- Import jobs (admin UI logs)
-- =========================

CREATE TABLE IF NOT EXISTS import_jobs (
  job_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  done BOOLEAN NOT NULL DEFAULT FALSE,
  ok BOOLEAN NOT NULL DEFAULT FALSE,
  exit_code INTEGER,
  logs TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_import_jobs_created_at
  ON import_jobs (created_at DESC);
