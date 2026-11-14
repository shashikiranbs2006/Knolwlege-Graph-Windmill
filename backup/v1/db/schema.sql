CREATE TABLE IF NOT EXISTS datasets (
  dataset_id text PRIMARY KEY,
  record_kind text NOT NULL CHECK (record_kind IN ('observed_source', 'synthetic_demo')),
  source text NOT NULL,
  source_date date NOT NULL,
  owner text NOT NULL,
  license text NOT NULL,
  usage_note text NOT NULL
);

CREATE TABLE IF NOT EXISTS turbines (
  turbine_id text PRIMARY KEY,
  title text NOT NULL UNIQUE,
  manufacturer text NOT NULL,
  model text NOT NULL,
  rated_power_kw numeric NOT NULL,
  latitude numeric NOT NULL,
  longitude numeric NOT NULL,
  dataset_id text NOT NULL REFERENCES datasets
);

CREATE TABLE IF NOT EXISTS scada_readings (
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
CREATE INDEX IF NOT EXISTS scada_turbine_time ON scada_readings (turbine_id, observed_at);

CREATE TABLE IF NOT EXISTS status_events (
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
  PRIMARY KEY (source_file, row_number)
);
ALTER TABLE status_events ALTER COLUMN ended_at DROP NOT NULL;
CREATE INDEX IF NOT EXISTS events_turbine_time ON status_events (turbine_id, started_at, ended_at);

CREATE TABLE IF NOT EXISTS synthetic_maintenance (
  case_id text PRIMARY KEY,
  record_kind text NOT NULL CHECK (record_kind = 'synthetic_demo'),
  turbine_id text NOT NULL REFERENCES turbines,
  source_status_file text NOT NULL,
  source_status_row integer NOT NULL,
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
  FOREIGN KEY (source_status_file, source_status_row) REFERENCES status_events (source_file, row_number)
);

CREATE OR REPLACE VIEW measurement_observations AS
SELECT source_file, row_number, turbine_id, observed_at, 'Power' AS signal, 'KiloW' AS unit_code, power_kw AS value, dataset_id
FROM scada_readings WHERE power_kw IS NOT NULL
UNION ALL
SELECT source_file, row_number, turbine_id, observed_at, 'EnergyExport', 'KiloW-HR', energy_export_kwh, dataset_id
FROM scada_readings WHERE energy_export_kwh IS NOT NULL
UNION ALL
SELECT source_file, row_number, turbine_id, observed_at, 'WindSpeed', 'M-PER-SEC', wind_speed_ms, dataset_id
FROM scada_readings WHERE wind_speed_ms IS NOT NULL;

CREATE OR REPLACE VIEW logical_sensors AS
SELECT turbine_id, signal FROM turbines CROSS JOIN (VALUES ('Power'), ('EnergyExport'), ('WindSpeed')) AS s(signal);
