# Kelmarsh data collected for V1

Source: Cubico Sustainable Investments, [Kelmarsh wind farm data v4](https://zenodo.org/records/16807551), DOI 10.5281/zenodo.16807551. License: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Attribute Cubico and cite the Zenodo record when publishing results or derivatives.

| Local file | Purpose | Published MD5 (verified locally) |
| --- | --- | --- |
| `Kelmarsh_WT_static.csv` | Six turbine IDs, model, rated power, dimensions, coordinates | `af3a038f0f7fddfc1608ad0bfc8cf5ba` |
| `Kelmarsh_WT_dataSignalMapping.csv` | Signal names and stated units | `ca8fe399ab15ae7111d3214c632efd36` |
| `Kelmarsh_SCADA_2016_3082.zip` | 2016 ten-minute SCADA and status events for all six turbines | `ca8efbb6dbcfae3516583937eb5b5402` |

The 2016 archive contains six SCADA files of 52,416 rows each (314,496 total, January 3 to December 31) and six status files with 14,019 records total. Turbine 1's SCADA file has 299 columns, including wind speed, power, temperatures, and RPM; its status file has event start/end, duration, status, code, message, service contract category, and IEC category. The file header states UTC and marks missing/erroneous values `NaN`.

Data checks before import:

- The signal dictionary calls `Power` kWh, while the SCADA column is `Power (kW)`. V1 follows the SCADA column and keeps power distinct from energy; do not convert power to energy by label alone.
- V1 unit decision: map `Power (kW)` to kilowatts, `Energy Export (kWh)` to kilowatt hours, and wind speed to metres per second, as recorded in `v1_unit_decisions.csv`. For turbine 1, the median ratio of interval energy to power over 34,947 paired positive readings is about 0.156 hours, close to a ten-minute interval; the exact values need not equal power divided by six. The raw dictionary remains unchanged.
- Values are sparse for some signals. In turbine 1, wind speed and power are present in 48,485 of 52,416 rows; gear oil temperature and generator RPM in 34,310.
- Status events are not verified faults or maintenance work orders. Any manually curated fault-to-component relation must carry its evidence and be clearly marked as a hypothesis unless independently confirmed.
- Keep raw files unchanged. Convert timestamps to PostgreSQL `timestamptz` using the source's UTC declaration. Preserve source filename and row identity for provenance.

The existing `../T1.csv` is a separate 2018 single-turbine dataset with 50,530 rows and five columns. Its source/license has not been verified here, and it has no event or maintenance records. Use it only as disposable plumbing data until its provenance is established.

Not collected for V1: other Kelmarsh years (the [v4 record](https://zenodo.org/records/16807551) offers 2017–2024), grid/PMU archives, and independent maintenance logs. They are not required to demonstrate the V1 query pipeline on one real year. If the goal is to prove real repairs, replacements, technicians, or fault root causes, obtain operator work orders or a licensed linked maintenance dataset with turbine IDs and timestamps. Do not infer them from status messages.

## Clearly synthetic V1 demonstration data

`v1_synthetic_maintenance.csv` contains ten fictional maintenance cases (V1.1; V1 had two) linked to **real** status-event rows, each naming the shared fault code it addresses (`fault_code`). It supplies illustrative `FaultCondition`, `MaintenanceEvent`, `ReplacementPart` (one case), `Technician`, and `MaintenanceLog` instances for V1 mappings. The event references and their messages are real; all diagnosis, work, parts, people, and log text are invented. `record_kind=synthetic_demo` must remain queryable in PostgreSQL and RDF, and any query or chart combining these records with observed SCADA must display the synthetic label. `relation_confidence=0.0` means there is no real-world evidence for the invented relation; it is not a calibrated probability. The separate rule confidences in `../curated/alarm_fault_rules.csv` are illustrative demo values (`curated_demo`), not validated probabilities either. Do not treat these as operator records.

`v1_dataset_metadata.csv` gives both the observed source and synthetic layer a queryable source, date, owner, licence, and usage note. `v1_unit_decisions.csv` records canonical units without modifying Cubico's source files. The V1 setup loads dataset metadata and synthetic cases into PostgreSQL; unit decisions determine the RDF mappings.
