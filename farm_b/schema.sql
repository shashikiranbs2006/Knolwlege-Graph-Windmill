SET client_min_messages = warning;
-- Farm B ("Penmanshiel operator") keeps its own, differently shaped database.
-- Nothing here matches farm A's table or column names; only the R2RML mapping
-- (farm_b/mapping.obda) translates it into the shared wind-core vocabulary.
DROP VIEW IF EXISTS alarm_diagnosis, wtg_parts CASCADE;
DROP TABLE IF EXISTS work_orders, alarm_rules, fault_catalog, alarm_log, tenmin, wtg, site, data_sources CASCADE;

CREATE TABLE data_sources (
  source_ref text PRIMARY KEY,
  kind text NOT NULL,                 -- observed_source | synthetic_demo | curated_demo | derived_assumption
  origin text NOT NULL,
  published date NOT NULL,
  owner_org text NOT NULL,
  licence text NOT NULL,
  terms text NOT NULL
);

CREATE TABLE site (
  site_code text PRIMARY KEY,
  site_name text NOT NULL,
  country text NOT NULL,
  source_ref text NOT NULL REFERENCES data_sources
);

CREATE TABLE wtg (
  wtg_code text PRIMARY KEY,          -- e.g. T01
  site_code text NOT NULL REFERENCES site,
  wtg_label text NOT NULL,
  make text NOT NULL,
  type_name text NOT NULL,
  rated_kw numeric NOT NULL,
  lat numeric NOT NULL,
  lon numeric NOT NULL
);

-- Long ("narrow") 10-minute table: one row per turbine, timestamp and signal.
CREATE TABLE tenmin (
  src_file text NOT NULL,
  src_line integer NOT NULL,
  wtg_code text NOT NULL REFERENCES wtg,
  ts timestamptz NOT NULL,
  signal_name text NOT NULL,          -- P_avg, E_exp, WS_avg, T_gear_oil
  val double precision NOT NULL,
  PRIMARY KEY (src_file, src_line, signal_name)
);
CREATE INDEX tenmin_wtg_signal_ts ON tenmin (wtg_code, signal_name, ts);

CREATE TABLE alarm_log (
  src_file text NOT NULL,
  line_no integer NOT NULL,
  wtg_code text NOT NULL REFERENCES wtg,
  t_from timestamptz NOT NULL,
  t_to timestamptz,
  severity text NOT NULL,
  alarm_code text NOT NULL,
  alarm_text text NOT NULL,
  PRIMARY KEY (src_file, line_no),
  UNIQUE (wtg_code, line_no)
);

CREATE TABLE fault_catalog (
  fault_ref text PRIMARY KEY,
  title text NOT NULL,
  part_type text NOT NULL,
  details text NOT NULL
);

CREATE TABLE alarm_rules (
  rule_ref text PRIMARY KEY,
  pattern text NOT NULL,
  fault_ref text NOT NULL REFERENCES fault_catalog,
  weight numeric NOT NULL,
  why text NOT NULL
);

CREATE TABLE work_orders (
  wo_number text PRIMARY KEY,
  wtg_code text NOT NULL REFERENCES wtg,
  alarm_file text NOT NULL,
  alarm_line integer NOT NULL,
  fault_ref text NOT NULL REFERENCES fault_catalog,
  part_type text NOT NULL,
  job_type text NOT NULL,
  opened timestamptz NOT NULL,
  closed timestamptz NOT NULL,
  crew_name text NOT NULL,            -- personal data: deliberately NOT mapped (governance)
  notes text NOT NULL,
  source_ref text NOT NULL REFERENCES data_sources,
  FOREIGN KEY (alarm_file, alarm_line) REFERENCES alarm_log (src_file, line_no)
);

CREATE VIEW alarm_diagnosis AS
SELECT a.wtg_code, w.site_code, a.line_no, r.rule_ref, r.fault_ref, r.weight, r.why
FROM alarm_log a JOIN wtg w USING (wtg_code) JOIN alarm_rules r ON a.alarm_text ~* r.pattern;

-- Farm B keeps no component register; this view states the model assumption (one gearbox, one generator).
CREATE VIEW wtg_parts AS
SELECT wtg_code, site_code, p.part::text AS part FROM wtg CROSS JOIN (VALUES ('Gearbox'), ('Generator')) AS p(part);
