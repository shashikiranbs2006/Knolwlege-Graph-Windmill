\set ON_ERROR_STOP on
BEGIN;
TRUNCATE synthetic_maintenance, alarm_fault_rules, fault_conditions, components, scada_readings, status_events, turbines, farms, datasets;
COPY datasets FROM '/import/datasets.csv' WITH (FORMAT csv, HEADER true);
COPY farms FROM '/import/farms.csv' WITH (FORMAT csv, HEADER true);
COPY turbines FROM '/import/turbines.csv' WITH (FORMAT csv, HEADER true);
COPY components FROM '/import/components.csv' WITH (FORMAT csv, HEADER true);
COPY scada_readings FROM '/import/scada_readings.csv' WITH (FORMAT csv, HEADER true);
COPY status_events FROM '/import/status_events.csv' WITH (FORMAT csv, HEADER true);
COPY fault_conditions FROM '/import/fault_conditions.csv' WITH (FORMAT csv, HEADER true);
COPY alarm_fault_rules FROM '/import/alarm_fault_rules.csv' WITH (FORMAT csv, HEADER true);
COPY synthetic_maintenance FROM '/import/synthetic_maintenance.csv' WITH (FORMAT csv, HEADER true);
COMMIT;
