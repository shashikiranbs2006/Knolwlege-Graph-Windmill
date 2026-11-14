SELECT t.title AS "turbineName", r.observed_at AS "observedAt", r.power_kw AS power,
       r.source_file AS "sourceFile", r.row_number AS "sourceRow",
       e.message AS "eventMessage", e.source_file AS "eventFile", e.row_number AS "eventRow",
       m.proposed_fault AS "faultName", m.action, m.record_kind AS "recordKind",
       m.relation_confidence AS confidence, m.evidence_note AS "evidenceNote",
       real_d.source AS "realSource", demo_d.source AS "demoSource"
FROM scada_readings r JOIN turbines t USING (turbine_id)
JOIN status_events e ON e.turbine_id = r.turbine_id
JOIN synthetic_maintenance m ON m.source_status_file = e.source_file AND m.source_status_row = e.row_number
JOIN datasets real_d ON real_d.dataset_id = r.dataset_id
JOIN datasets demo_d ON demo_d.dataset_id = m.dataset_id
WHERE r.turbine_id = 'KWF2' AND r.power_kw IS NOT NULL AND e.row_number = 72
  AND r.observed_at >= '2016-01-29 12:00:00+00' AND r.observed_at < '2016-01-29 13:00:00+00'
  AND r.observed_at >= e.started_at AND r.observed_at < e.ended_at
ORDER BY r.observed_at LIMIT 5;
