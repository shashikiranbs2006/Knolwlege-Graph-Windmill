SET client_min_messages = warning;
-- V1.1 schema for one farm node. Re-running drops and recreates ONLY these demo objects;
-- the data is reloaded by db/load.sql from build/import.
DROP VIEW IF EXISTS diagnoses, logical_sensors, measurement_observations CASCADE;
DROP TABLE IF EXISTS synthetic_maintenance, alarm_fault_rules, fault_conditions, components,
  status_events, scada_readings, turbines, farms, datasets CASCADE;

CREATE TABLE datasets (
  dataset_id text PRIMARY KEY,
  record_kind text NOT NULL CHECK (record_kind IN ('observed_source', 'synthetic_demo', 'curated_demo', 'derived_assumption')),
  source text NOT NULL,
  source_date date NOT NULL,
  owner text NOT NULL,
  license text NOT NULL,
  usage_note text NOT NULL
);

CREATE TABLE farms (
  farm_id text PRIMARY KEY,            -- lower-case slug, used in every IRI (e.g. kelmarsh)
  name text NOT NULL,
  country text NOT NULL,
  dataset_id text NOT NULL REFERENCES datasets
);

CREATE TABLE turbines (
  turbine_id text PRIMARY KEY,
  farm_id text NOT NULL REFERENCES farms,
  title text NOT NULL UNIQUE,
  manufacturer text NOT NULL,
  model text NOT NULL,
  rated_power_kw numeric NOT NULL,
  latitude numeric NOT NULL,
  longitude numeric NOT NULL,
  dataset_id text NOT NULL REFERENCES datasets
);

CREATE TABLE scada_readings (
  source_file text NOT NULL,
  row_number integer NOT NULL,
  turbine_id text NOT NULL REFERENCES turbines,
  observed_at timestamptz NOT NULL,
  power_kw double precision,
  energy_export_kwh double precision,
  wind_speed_ms double precision,
  gear_oil_temp_c double precision,
  generator_rpm double precision,
  wind_direction_deg double precision,
  dataset_id text NOT NULL REFERENCES datasets,
  PRIMARY KEY (source_file, row_number)
);
CREATE INDEX scada_turbine_time ON scada_readings (turbine_id, observed_at);

CREATE TABLE status_events (
  source_file text NOT NULL,
  row_number integer NOT NULL,
  turbine_id text NOT NULL REFERENCES turbines,
  started_at timestamptz NOT NULL,
  ended_at timestamptz,
  status text NOT NULL,
  code text NOT NULL,
  message text NOT NULL,
  service_contract_category text,
  iec_category text,
  dataset_id text NOT NULL REFERENCES datasets,
  PRIMARY KEY (source_file, row_number),
  UNIQUE (turbine_id, row_number)
);
CREATE INDEX events_turbine_time ON status_events (turbine_id, started_at, ended_at);

-- Components assumed from the turbine model (see dataset v11-component-register).
CREATE TABLE components (
  turbine_id text NOT NULL REFERENCES turbines,
  component_type text NOT NULL CHECK (component_type IN ('Gearbox', 'Generator', 'RotorBlade')),
  dataset_id text NOT NULL REFERENCES datasets,
  PRIMARY KEY (turbine_id, component_type)
);

-- Shared fault catalogue (same codes on every farm node, so faults can be compared across farms).
CREATE TABLE fault_conditions (
  fault_code text PRIMARY KEY,
  label text NOT NULL,
  component_type text NOT NULL,
  description text NOT NULL,
  dataset_id text NOT NULL REFERENCES datasets
);

-- Curated, illustrative alarm-to-fault rules with a confidence per rule.
CREATE TABLE alarm_fault_rules (
  rule_id text PRIMARY KEY,
  message_pattern text NOT NULL,
  fault_code text NOT NULL REFERENCES fault_conditions,
  confidence numeric NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  rationale text NOT NULL,
  dataset_id text NOT NULL REFERENCES datasets
);

CREATE TABLE synthetic_maintenance (
  case_id text PRIMARY KEY,
  record_kind text NOT NULL CHECK (record_kind = 'synthetic_demo'),
  turbine_id text NOT NULL REFERENCES turbines,
  source_status_file text NOT NULL,
  source_status_row integer NOT NULL,
  fault_code text NOT NULL REFERENCES fault_conditions,
  proposed_fault text NOT NULL,
  proposed_component text NOT NULL,
  maintenance_start timestamptz NOT NULL,
  maintenance_end timestamptz NOT NULL,
  action text NOT NULL,
  replacement_part text,
  technician text NOT NULL,
  maintenance_log text NOT NULL,
  relation_confidence numeric NOT NULL,
  evidence_note text NOT NULL,
  dataset_id text NOT NULL REFERENCES datasets,
  FOREIGN KEY (source_status_file, source_status_row) REFERENCES status_events (source_file, row_number),
  FOREIGN KEY (turbine_id, proposed_component) REFERENCES components (turbine_id, component_type)
);

-- Every view carries farm_id so the mapping can build farm-scoped IRIs.
CREATE VIEW measurement_observations AS
SELECT r.source_file, r.row_number, r.turbine_id, t.farm_id, r.observed_at, 'Power' AS signal, 'KiloW' AS unit_code, r.power_kw AS value, r.dataset_id
FROM scada_readings r JOIN turbines t USING (turbine_id) WHERE r.power_kw IS NOT NULL
UNION ALL
SELECT r.source_file, r.row_number, r.turbine_id, t.farm_id, r.observed_at, 'EnergyExport', 'KiloW-HR', r.energy_export_kwh, r.dataset_id
FROM scada_readings r JOIN turbines t USING (turbine_id) WHERE r.energy_export_kwh IS NOT NULL
UNION ALL
SELECT r.source_file, r.row_number, r.turbine_id, t.farm_id, r.observed_at, 'WindSpeed', 'M-PER-SEC', r.wind_speed_ms, r.dataset_id
FROM scada_readings r JOIN turbines t USING (turbine_id) WHERE r.wind_speed_ms IS NOT NULL
UNION ALL
SELECT r.source_file, r.row_number, r.turbine_id, t.farm_id, r.observed_at, 'GearOilTemperature', 'DEG_C', r.gear_oil_temp_c, r.dataset_id
FROM scada_readings r JOIN turbines t USING (turbine_id) WHERE r.gear_oil_temp_c IS NOT NULL;

CREATE VIEW logical_sensors AS
SELECT turbine_id, farm_id, signal FROM turbines
CROSS JOIN (VALUES ('Power'), ('EnergyExport'), ('WindSpeed'), ('GearOilTemperature')) AS s(signal);

-- A diagnosis = one real status event matched by one curated rule.
CREATE VIEW diagnoses AS
SELECT e.turbine_id, t.farm_id, e.row_number, r.rule_id, r.fault_code, r.confidence, r.rationale, r.dataset_id
FROM status_events e
JOIN turbines t USING (turbine_id)
JOIN alarm_fault_rules r ON e.message ~* r.message_pattern;
