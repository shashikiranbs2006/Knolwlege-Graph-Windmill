SELECT t.title AS "turbineName", r.observed_at AS "observedAt", r.power_kw AS power,
       r.source_file AS "sourceFile", r.row_number AS "sourceRow",
       e.message AS "eventMessage", e.source_file AS "eventFile", e.row_number AS "eventRow"
FROM scada_readings r JOIN turbines t USING (turbine_id)
JOIN status_events e ON e.turbine_id = r.turbine_id
WHERE r.turbine_id = 'KWF2' AND r.power_kw IS NOT NULL AND e.row_number = 72
  AND r.observed_at >= '2016-01-29 12:00:00+00' AND r.observed_at < '2016-01-29 13:00:00+00'
  AND r.observed_at >= e.started_at AND r.observed_at < e.ended_at
ORDER BY r.observed_at LIMIT 5;
