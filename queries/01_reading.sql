SELECT t.title AS "turbineName", r.observed_at AS "observedAt", r.power_kw AS power,
       'http://qudt.org/vocab/unit/KiloW' AS unit, r.source_file AS "sourceFile",
       r.row_number AS "sourceRow", d.source AS "datasetSource"
FROM scada_readings r JOIN turbines t USING (turbine_id)
JOIN datasets d ON d.dataset_id = r.dataset_id
WHERE r.turbine_id = 'KWF2' AND r.power_kw IS NOT NULL
  AND r.observed_at >= '2016-01-29 12:00:00+00' AND r.observed_at < '2016-01-29 13:00:00+00'
ORDER BY r.observed_at LIMIT 5;
