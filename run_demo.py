"""Run the V1 SPARQL examples, explain their provenance, and compare with SQL."""

import argparse
import csv
import io
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal
from pathlib import Path

QUERIES = Path("queries")
NAMES = ("01_reading", "02_event_join", "03_synthetic_join", "05_uc1_fault_diagnosis")
ENDPOINT = "http://127.0.0.1:8080/sparql"


def sparql(name):
    query = (QUERIES / f"{name}.rq").read_text(encoding="utf-8")
    request = urllib.request.Request(
        ENDPOINT,
        data=urllib.parse.urlencode({"query": query}).encode(),
        headers={"Accept": "application/sparql-results+json"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(error.read().decode(errors="replace")) from error
    elapsed = time.perf_counter() - started
    columns = result["head"]["vars"]
    rows = [{col: row.get(col, {}).get("value", "") for col in columns} for row in result["results"]["bindings"]]
    return columns, rows, elapsed


def sql(name):
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "db", "psql", "-U", "vkg", "-d", "vkg", "--csv", "-f", f"/queries/{name}.sql"],
        capture_output=True, text=True, check=True,
    )
    return list(csv.DictReader(io.StringIO(result.stdout)))


def same(left, right):
    if left == right:
        return True
    try:
        return abs(Decimal(left) - Decimal(right)) < Decimal("0.000001")
    except Exception:
        pass
    try:
        return datetime.fromisoformat(left.replace("Z", "+00:00")) == datetime.fromisoformat(right.replace("Z", "+00:00"))
    except Exception:
        return False


def explain(name, rows):
    if name == "05_uc1_fault_diagnosis":
        if not rows:
            return "No curated diagnosis matched the alarm text."
        top = rows[0]
        return (f"Most likely cause: {top['fault']} (rule confidence {top['confidence']}, illustrative). "
                f"Last recorded fix for it: {top['action'] or 'none recorded'} on {top['lastDone'] or '-'}. "
                f"Fault links come from curated rules over real Kelmarsh alarm events; maintenance records are {top['recordKind'] or 'absent'}.")
    if not rows:
        return "No matching observations were returned."
    row, last = rows[0], rows[-1]
    parts = [
        f"These {len(rows)} readings are from {row['turbineName']} between {row['observedAt']} and {last['observedAt']}.",
        f"Their power values come from {row['sourceFile']}, data rows {row['sourceRow']} through {last['sourceRow']}.",
    ]
    if name == "01_reading":
        parts.append(f"Their unit is {row['unit']}; dataset source: {row['datasetSource']}.")
    else:
        parts.append(f"Each reading time falls within the reported event in {row['eventFile']}, row {row['eventRow']}: {row['eventMessage']}.")
    if name == "03_synthetic_join":
        parts.append(f"The curated diagnosis ({row['faultName']}, illustrative rule confidence {row['confidence']}: {row['evidenceNote']}) comes from a demo rule over the real alarm text.")
        parts.append(f"The maintenance action ({row['action']}) is {row['recordKind']} from {row['demoSource']}.")
        parts.append(f"Observed measurements are from {row['realSource']}.")
    return " ".join(parts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="?", choices=NAMES)
    parser.add_argument("--verify", action="store_true", help="Compare every result value with the paired SQL query")
    parser.add_argument("--benchmark", action="store_true", help="Time a bounded one-day count after one warm-up")
    args = parser.parse_args()
    for name in (args.query,) if args.query else NAMES:
        columns, rows, elapsed = sparql(name)
        print(f"\n{name}: {len(rows)} rows in {elapsed:.3f}s")
        writer = csv.writer(sys.stdout)
        writer.writerow(columns)
        writer.writerows([row[col] for col in columns] for row in rows)
        print("How this result was obtained: " + explain(name, rows))
        if args.verify:
            expected = sql(name)
            if name in ("03_synthetic_join", "05_uc1_fault_diagnosis") and any(row["recordKind"] not in ("synthetic_demo", "") for row in rows):
                raise AssertionError("Synthetic provenance label missing")
            if not rows or len(rows) != len(expected) or any(
                not same(actual[col], reference[col])
                for actual, reference in zip(rows, expected)
                for col in columns
            ):
                raise AssertionError(f"SQL/SPARQL mismatch for {name}: {rows[:1]} vs {expected[:1]}")
            print("SQL/SPARQL values match")
    if args.verify:
        counts = subprocess.run(
            ["docker", "compose", "exec", "-T", "db", "psql", "-U", "vkg", "-d", "vkg", "-At", "-c",
             "SELECT (SELECT count(*) FROM farms), (SELECT count(*) FROM turbines), (SELECT count(*) FROM scada_readings), (SELECT count(*) FROM status_events), (SELECT count(*) FROM synthetic_maintenance), (SELECT count(*) FROM scada_readings WHERE power_kw IS NULL);"],
            capture_output=True, text=True, check=True,
        ).stdout.strip().split("|")
        if list(map(int, counts[:5])) != [1, 6, 314496, 14019, 10] or int(counts[5]) <= 0:
            raise AssertionError(f"Unexpected import counts or missing-value handling: {counts}")
        print(f"Import counts match; {counts[5]} missing power values remain NULL")
    if args.benchmark:
        sparql("04_day_benchmark")
        trials = [sparql("04_day_benchmark") for _ in range(3)]
        counts = [rows[0]["readings"] for _, rows, _ in trials]
        if len(set(counts)) != 1:
            raise AssertionError(f"Benchmark count changed: {counts}")
        if max(elapsed for _, _, elapsed in trials) >= 5:
            raise AssertionError("Warm one-day query exceeded five seconds")
        print(f"One-day KWF2 power count: {counts[0]}; warm timings: " + ", ".join(f"{elapsed:.3f}s" for _, _, elapsed in trials))


if __name__ == "__main__":
    main()
