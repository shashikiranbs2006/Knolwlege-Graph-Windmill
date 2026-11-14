\set ON_ERROR_STOP on
BEGIN;
TRUNCATE synthetic_maintenance, scada_readings, status_events, turbines, datasets;
COPY datasets FROM '/import/datasets.csv' WITH (FORMAT csv, HEADER true);
COPY turbines FROM '/import/turbines.csv' WITH (FORMAT csv, HEADER true);
COPY scada_readings FROM '/import/scada_readings.csv' WITH (FORMAT csv, HEADER true);
COPY status_events FROM '/import/status_events.csv' WITH (FORMAT csv, HEADER true);
COPY synthetic_maintenance FROM '/import/synthetic_maintenance.csv' WITH (FORMAT csv, HEADER true);
COMMIT;
