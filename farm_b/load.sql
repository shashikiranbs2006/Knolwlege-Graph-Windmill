\set ON_ERROR_STOP on
BEGIN;
TRUNCATE work_orders, alarm_rules, fault_catalog, alarm_log, tenmin, wtg, site, data_sources;
COPY data_sources FROM '/import/data_sources.csv' WITH (FORMAT csv, HEADER true);
COPY site FROM '/import/site.csv' WITH (FORMAT csv, HEADER true);
COPY wtg FROM '/import/wtg.csv' WITH (FORMAT csv, HEADER true);
COPY tenmin FROM '/import/tenmin.csv' WITH (FORMAT csv, HEADER true);
COPY alarm_log FROM '/import/alarm_log.csv' WITH (FORMAT csv, HEADER true);
COPY fault_catalog FROM '/import/fault_catalog.csv' WITH (FORMAT csv, HEADER true);
COPY alarm_rules FROM '/import/alarm_rules.csv' WITH (FORMAT csv, HEADER true);
COPY work_orders FROM '/import/work_orders.csv' WITH (FORMAT csv, HEADER true);
COMMIT;
ANALYZE;
