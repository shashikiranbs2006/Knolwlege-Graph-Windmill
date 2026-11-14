WITH faults AS (
  SELECT f.fault_code, f.label AS fault, MAX(d.confidence) AS confidence
  FROM status_events e
  JOIN diagnoses d ON d.turbine_id = e.turbine_id AND d.row_number = e.row_number
  JOIN fault_conditions f ON f.fault_code = d.fault_code
  WHERE lower(e.message) LIKE '%high temp. gear bearing%' AND f.component_type = 'Gearbox'
  GROUP BY f.fault_code, f.label
), fixes AS (
  SELECT fault_code, action, MAX(maintenance_start) AS "lastDone", COUNT(DISTINCT case_id) AS times, MIN(record_kind) AS "recordKind"
  FROM synthetic_maintenance GROUP BY fault_code, action
)
SELECT faults.fault, faults.confidence, fixes.action, fixes."lastDone", fixes.times, fixes."recordKind"
FROM faults LEFT JOIN fixes USING (fault_code)
ORDER BY faults.confidence DESC, fixes."lastDone" DESC;
