"""Hub checks for both use cases: run the hub queries and compare them with SQL on each farm.

Use case 1 (queries/farm_a_01_*.rq) runs on farm A's named graph in the hub and must equal the
same question asked in SQL on farm A's PostgreSQL. Use case 2 (queries/hub_0[123]_*.rq) runs the
same kind of pattern over every farm graph and must equal SQL on both farm databases.

Needs the services running (setup.ps1, or docker compose --profile uc2 up -d).
    uv run --offline python run_hub.py --verify
"""

import argparse
import csv
import io
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

HUB = "http://127.0.0.1:3030/hub/sparql"
NAMES = ("farm_a_01_fault_diagnosis", "hub_01_cross_farm_fixes", "hub_02_alarm_knowledge", "hub_03_governance")
FIXES_A = """SELECT 'kelmarsh' AS farm, f.label AS fault, m.action, COUNT(DISTINCT m.case_id) AS times
FROM synthetic_maintenance m JOIN fault_conditions f USING (fault_code) WHERE m.proposed_component = 'Gearbox' GROUP BY 1, 2, 3"""
FIXES_B = """SELECT 'penmanshiel' AS farm, f.title AS fault, o.job_type AS action, COUNT(DISTINCT o.wo_number) AS times
FROM work_orders o JOIN fault_catalog f USING (fault_ref) WHERE o.part_type = 'Gearbox' GROUP BY 1, 2, 3"""
ALARMS_A = "SELECT r.rule_id, COUNT(*) FROM status_events e JOIN alarm_fault_rules r ON e.message ~* r.message_pattern GROUP BY 1"
ALARMS_B = "SELECT r.rule_ref, COUNT(*) FROM alarm_log a JOIN alarm_rules r ON a.alarm_text ~* r.pattern GROUP BY 1"


def sparql(name):
    query = (Path("queries") / f"{name}.rq").read_text(encoding="utf-8")
    request = urllib.request.Request(HUB, data=urllib.parse.urlencode({"query": query}).encode(),
                                     headers={"Accept": "application/sparql-results+json"})
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=180) as response:
        result = json.load(response)
    columns = result["head"]["vars"]
    rows = [{c: b.get(c, {}).get("value", "") for c in columns} for b in result["results"]["bindings"]]
    return columns, rows, time.perf_counter() - started


def sql(service, user, sql_text):
    out = subprocess.run(["docker", "compose", "--profile", "uc2", "exec", "-T", service, "psql", "-U", user, "-d", user if user == "farmb" else "vkg",
                          "--csv", "-c", sql_text], capture_output=True, text=True, check=True).stdout
    return list(csv.reader(io.StringIO(out)))[1:]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    results = {}
    for name in NAMES:
        columns, rows, elapsed = sparql(name)
        results[name] = rows
        print(f"\n{name}: {len(rows)} rows in {elapsed:.2f}s")
        writer = csv.writer(__import__("sys").stdout)
        writer.writerow(columns)
        writer.writerows([row[c] for c in columns] for row in rows)
    if not args.verify:
        return
    from run_demo import same
    uc1 = results["farm_a_01_fault_diagnosis"]
    expected = list(csv.DictReader(io.StringIO(subprocess.run(
        ["docker", "compose", "exec", "-T", "db", "psql", "-U", "vkg", "-d", "vkg", "--csv", "-f", "/queries/05_uc1_fault_diagnosis.sql"],
        capture_output=True, text=True, check=True).stdout)))
    assert uc1 and len(uc1) == len(expected) and all(same(a[c], e[c]) for a, e in zip(uc1, expected) for c in a), \
        f"farm A graph in the hub differs from SQL on farm A:\n{uc1}\n{expected}"
    assert all(r["recordKind"] in ("synthetic_demo", "") for r in uc1)
    print("farm_a_01: use case 1 on farm A's hub graph matches SQL on farm A's database")
    ontop_farms = ("kelmarsh", "penmanshiel")  # farm C (optional Neo4j export) has no SQL database to compare with
    hub = Counter({(r["farm"], r["fault"], r["action"]): int(r["times"]) for r in results["hub_01_cross_farm_fixes"] if r["farm"] in ontop_farms})
    expected = Counter({(farm, fault, action): int(times) for farm, fault, action, times in sql("db", "vkg", FIXES_A) + sql("db-b", "farmb", FIXES_B)})
    assert hub == expected, f"hub_01 differs from SQL on the farms:\n{hub}\n{expected}"
    assert all(r["recordKind"] == "synthetic_demo" for r in results["hub_01_cross_farm_fixes"])
    print("hub_01: cross-farm counts match SQL on both farm databases")
    hub = {(r["farm"], r["rule"]): int(r["alarmsFired"]) for r in results["hub_02_alarm_knowledge"] if r["farm"] in ontop_farms}
    expected = {("kelmarsh", rule): int(n) for rule, n in sql("db", "vkg", ALARMS_A)}
    expected |= {("penmanshiel", rule): int(n) for rule, n in sql("db-b", "farmb", ALARMS_B)}
    assert hub == expected, f"hub_02 differs from SQL:\n{hub}\n{expected}"
    assert all(re.fullmatch(r"0?\.\d+", r["confidence"]) for r in results["hub_02_alarm_knowledge"])
    print("hub_02: RDF 1.2 edge annotations returned; alarm counts match SQL on both farms")
    techs = {r["farm"]: r["technicians"] for r in results["hub_03_governance"] if r["technicians"] and r["farm"] in ontop_farms}
    assert techs == {"kelmarsh": "10", "penmanshiel": "0"}, techs
    print("hub_03: farm B's withheld technician names are not reachable through the graph")


if __name__ == "__main__":
    main()
