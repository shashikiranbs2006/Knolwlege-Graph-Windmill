"""Prepare the immutable Kelmarsh archive for PostgreSQL COPY."""

import csv
import hashlib
import re
import zipfile
from pathlib import Path

SOURCE = Path("data/kelmarsh")
CURATED = Path("data/curated")
OUT = Path("build/import")
FARM_ID = "kelmarsh"
HASHES = {
    "Kelmarsh_SCADA_2016_3082.zip": "ca8efbb6dbcfae3516583937eb5b5402",
    "Kelmarsh_WT_static.csv": "af3a038f0f7fddfc1608ad0bfc8cf5ba",
    "Kelmarsh_WT_dataSignalMapping.csv": "ca8fe399ab15ae7111d3214c632efd36",
}


def rows(path):
    with path.open(newline="", encoding="utf-8-sig") as stream:
        yield from csv.DictReader(stream)


def write_csv(name, columns, data):
    with (OUT / name).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(data)


def value(text):
    return "" if text in ("NaN", "") else text


def utc(text):
    return "" if text in ("", "-") else text + "+00:00"


def main():
    for name, expected in HASHES.items():
        actual = hashlib.md5((SOURCE / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"Source hash mismatch: {name}")
    OUT.mkdir(parents=True, exist_ok=True)
    metadata = list(rows(SOURCE / "v1_dataset_metadata.csv"))
    write_csv("datasets.csv", list(metadata[0]), (list(row.values()) for row in metadata))
    turbines = list(rows(SOURCE / "Kelmarsh_WT_static.csv"))
    ids = {row["Title"]: row["Alternative Title"] for row in turbines}
    farm = turbines[0]
    write_csv("farms.csv", ["farm_id", "name", "country", "dataset_id"], [[FARM_ID, farm["Wind Farm"], farm["Country"], "kelmarsh-2016"]])
    write_csv(
        "turbines.csv",
        ["turbine_id", "farm_id", "title", "manufacturer", "model", "rated_power_kw", "latitude", "longitude", "dataset_id"],
        ((ids[r["Title"]], FARM_ID, r["Title"], r["Manufacturer"], r["Model"], r["Rated power (kW)"], r["Latitude"], r["Longitude"], "kelmarsh-2016") for r in turbines),
    )
    write_csv(
        "components.csv",
        ["turbine_id", "component_type", "dataset_id"],
        ([ids[r["Title"]], kind, "v11-component-register"] for r in turbines for kind in ("Gearbox", "Generator")),
    )
    faults = list(rows(CURATED / "fault_conditions.csv"))
    write_csv("fault_conditions.csv", [*faults[0], "dataset_id"], ([*r.values(), "v11-curated-fault-rules"] for r in faults))
    rules = list(rows(CURATED / "alarm_fault_rules.csv"))
    codes = {r["fault_code"] for r in faults}
    if any(r["fault_code"] not in codes for r in rules):
        raise ValueError("A rule names an unknown fault_code")
    write_csv("alarm_fault_rules.csv", [*rules[0], "dataset_id"], ([*r.values(), "v11-curated-fault-rules"] for r in rules))
    scada_count = event_count = 0
    event_keys = set()
    archive = SOURCE / "Kelmarsh_SCADA_2016_3082.zip"
    with zipfile.ZipFile(archive) as z, \
        (OUT / "scada_readings.csv").open("w", newline="", encoding="utf-8") as scada_file, \
        (OUT / "status_events.csv").open("w", newline="", encoding="utf-8") as event_file:
        scada = csv.writer(scada_file)
        events = csv.writer(event_file)
        scada.writerow(["source_file", "row_number", "turbine_id", "observed_at", "power_kw", "energy_export_kwh", "wind_speed_ms", "gear_oil_temp_c", "generator_rpm", "wind_direction_deg", "dataset_id"])
        events.writerow(["source_file", "row_number", "turbine_id", "started_at", "ended_at", "status", "code", "message", "service_contract_category", "iec_category", "dataset_id"])
        for name in z.namelist():
            match = re.match(r"(?:Turbine_Data|Status)_Kelmarsh_(\d)_2016", name)
            if not match:
                raise ValueError(f"Unexpected archive member: {name}")
            turbine_id = ids[f"Kelmarsh {match.group(1)}"]
            with z.open(name) as raw:
                text = (line.decode("utf-8-sig").rstrip("\r\n") for line in raw)
                if name.startswith("Turbine_Data"):
                    header = next(line[2:] for line in text if line.startswith("# Date and time,"))
                    columns = next(csv.reader([header]))
                    indices = [columns.index(label) for label in (
                        "Power (kW)", "Energy Export (kWh)", "Wind speed (m/s)",
                        "Gear oil temperature (°C)", "Generator RPM (RPM)", "Wind direction (°)",
                    )]
                    for number, row in enumerate(csv.reader(text), 1):
                        scada.writerow([name, number, turbine_id, utc(row[0]), *(value(row[i]) for i in indices), "kelmarsh-2016"])
                        scada_count += 1
                else:
                    reader = csv.DictReader(line for line in text if not line.startswith("#"))
                    for number, row in enumerate(reader, 1):
                        events.writerow([name, number, turbine_id, utc(row["Timestamp start"]), utc(row["Timestamp end"]), row["Status"], row["Code"], row["Message"], row["Service contract category"], row["IEC category"], "kelmarsh-2016"])
                        event_keys.add((name, number, row["Timestamp start"], row["Code"]))
                        event_count += 1
    if (len(turbines), scada_count, event_count) != (6, 314496, 14019):
        raise ValueError(f"Unexpected counts: {len(turbines)}, {scada_count}, {event_count}")
    cases = list(rows(SOURCE / "v1_synthetic_maintenance.csv"))
    for row in cases:
        key = (row["source_status_file"], int(row["source_status_row"]), row["source_event_start_utc"], row["source_event_code"])
        if key not in event_keys or row["record_kind"] != "synthetic_demo" or row["fault_code"] not in codes:
            raise ValueError(f"Invalid synthetic event link: {row['case_id']}")
    write_csv(
        "synthetic_maintenance.csv",
        ["case_id", "record_kind", "turbine_id", "source_status_file", "source_status_row", "fault_code", "proposed_fault", "proposed_component", "maintenance_start", "maintenance_end", "action", "replacement_part", "technician", "maintenance_log", "relation_confidence", "evidence_note", "dataset_id"],
        ((r["case_id"], r["record_kind"], ids[r["turbine_title"]], r["source_status_file"], r["source_status_row"], r["fault_code"], r["proposed_fault"], r["proposed_component"], utc(r["maintenance_start_utc"]), utc(r["maintenance_end_utc"]), r["action"], "" if r["replacement_part"] == "None" else r["replacement_part"], r["technician"], r["maintenance_log"], r["relation_confidence"], r["evidence_note"], "v1-synthetic-maintenance") for r in cases),
    )
    print(f"Prepared 1 farm, {len(turbines)} turbines, {len(faults)} fault types, {len(rules)} rules, {scada_count} SCADA rows, {event_count} events, {len(cases)} synthetic cases")


if __name__ == "__main__":
    main()
