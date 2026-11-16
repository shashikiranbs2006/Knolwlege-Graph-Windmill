"""farm_metric: live measurement aggregates asked of each farm's own Ontop endpoint.

The hub keeps knowledge (alarms, faults, repairs). Measurements (power, wind speed) stay at the
farm, so this tool asks each farm for ONE aggregate, in parallel, and merges the numbers. The LLM
never writes this SPARQL and never sees an endpoint: it only picks parameters (metric, aggregate,
farms, grouping, window), which are validated here against the farm registry (hub/farms.csv).

    metric     Power | WindSpeed | EnergyExport | GearOilTemperature (must be in the farm's `signals`
               and not in its `withheld_signals`)
    agg        avg | min | max | sum | count
    group_by   farm (default) | turbine | month
    farms      farm ids from the registry; the caller (assistant) enforces the scope rules
    start/end  optional ISO dates, end exclusive

The aggregate is computed inside Ontop (AVG/COUNT/... become SQL), so only a few rows travel.
A farm that is offline, times out or does not publish the signal is reported by name, never
silently dropped. Merging refuses unequal units.
"""

import csv
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import assistant

REGISTRY = Path(os.getenv("FARMS_CSV", "hub/farms.csv"))
ENDPOINT_COLUMN = os.getenv("FARM_ENDPOINT_COLUMN", "endpoint")  # "harvest_endpoint" when run from the host
METRICS = {"Power": "power", "WindSpeed": "wind speed", "EnergyExport": "exported energy", "GearOilTemperature": "gear oil temperature"}
AGGS = {"avg": "AVG", "min": "MIN", "max": "MAX", "sum": "SUM", "count": "COUNT"}
GROUPS = ("farm", "turbine", "month")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UNIT_NAMES = {"KiloW": "kW", "M-PER-SEC": "m/s", "KiloW-HR": "kWh", "DEG_C": "degC"}
PREFIXES = ("PREFIX wind: <https://w3id.org/wind-core/ontology#>\nPREFIX sosa: <http://www.w3.org/ns/sosa/>\n"
            "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n")


def registry():
    """Farm registry rows (hub/farms.csv), keyed by farm id, with the list columns split."""
    with REGISTRY.open(encoding="utf-8", newline="") as stream:
        farms = {}
        for row in csv.DictReader(stream):
            row["signal_list"] = [s for s in (row.get("signals") or "").split("|") if s]
            row["withheld_list"] = [s for s in (row.get("withheld_signals") or "").split("|") if s]
            farms[row["farm_id"]] = row
    return farms


def validate(params, farms):
    """Check the LLM's parameters against the registry. Returns a clean dict or raises ValueError."""
    metric, agg, group_by = params.get("metric"), params.get("agg") or "avg", params.get("group_by") or "farm"
    if metric not in METRICS:
        raise ValueError(f"Unknown metric {metric!r}; use one of {', '.join(METRICS)}.")
    if agg not in AGGS:
        raise ValueError(f"Unknown aggregate {agg!r}; use one of {', '.join(AGGS)}.")
    if group_by not in GROUPS:
        raise ValueError(f"Unknown grouping {group_by!r}; use one of {', '.join(GROUPS)}.")
    chosen = params.get("farms") or list(farms)
    unknown = [f for f in chosen if f not in farms]
    if unknown:
        raise ValueError(f"Farm(s) not in the registry: {', '.join(map(str, unknown))}.")
    window = {}
    for key in ("start", "end"):
        value = params.get(key)
        if value:
            if not DATE.match(str(value)):
                raise ValueError(f"{key} must be an ISO date such as 2016-03-01.")
            window[key] = str(value)
    return {"metric": metric, "agg": agg, "group_by": group_by, "farms": list(dict.fromkeys(chosen)), **window}


def aggregate_query(p):
    """The one aggregate query sent to a farm. group_by only changes the grouping."""
    group_vars = {"farm": "", "turbine": "?turbine", "month": "?month"}[p["group_by"]]
    pattern = f"?o sosa:observedProperty wind:{p['metric']} ; sosa:hasSimpleResult ?v ; wind:unit ?unit ; sosa:resultTime ?t ."
    if p["group_by"] == "turbine":
        pattern += " ?o sosa:hasFeatureOfInterest ?tur . ?tur wind:name ?turbine ."
    bind = " BIND(SUBSTR(STR(?t), 1, 7) AS ?month)" if p["group_by"] == "month" else ""
    filters = ""
    if p.get("start"):
        filters += f' FILTER(?t >= "{p["start"]}T00:00:00Z"^^xsd:dateTime)'
    if p.get("end"):
        filters += f' FILTER(?t < "{p["end"]}T00:00:00Z"^^xsd:dateTime)'
    fn = AGGS[p["agg"]]
    order = group_vars or "?unit"
    return (f"{PREFIXES}SELECT {group_vars} ?unit ({fn}(?v) AS ?value) (COUNT(?v) AS ?readings) "
            f"WHERE {{ {pattern}{bind}{filters} }} GROUP BY {group_vars} ?unit ORDER BY {order} LIMIT 200")


RATED_QUERY = PREFIXES + "SELECT (AVG(?r) AS ?rated) WHERE { ?t a wind:WindTurbine ; wind:ratedPowerKW ?r }"


RANGE_QUERY = PREFIXES + "SELECT (MIN(?t) AS ?first) (MAX(?t) AS ?last) WHERE { ?o sosa:resultTime ?t }"
_ranges = {}  # farm id -> (fetched_at, (first_date, last_date)) so the range is not asked on every question


def data_range(farm):
    """First and last day (YYYY-MM-DD) with readings at this farm, or None if it cannot be read. Cached for 10 minutes."""
    now = time.monotonic()
    cached = _ranges.get(farm["farm_id"])
    if cached and now - cached[0] < 600:
        return cached[1]
    try:
        rows = assistant.run_sparql(RANGE_QUERY, farm[ENDPOINT_COLUMN], timeout=min(15.0, float(farm.get("timeout_s") or 15)))[1]
        found = (rows[0]["first"][:10], rows[0]["last"][:10]) if rows and rows[0]["first"] else None
    except Exception:  # an unreachable farm must not break the question
        return None
    _ranges[farm["farm_id"]] = (now, found)
    return found


def data_ranges(farms=None):
    """{farm id: (first, last)} for every registry farm that answers, read in parallel."""
    farms = farms or registry()
    with ThreadPoolExecutor(max_workers=max(1, len(farms))) as pool:
        found = list(pool.map(data_range, farms.values()))
    return {fid: r for fid, r in zip(farms, found) if r}


def unit_name(iri):
    tail = iri.rsplit("/", 1)[-1]
    return UNIT_NAMES.get(tail, tail)


def one_farm(farm, params):
    """Ask one farm. Returns a dict with a status (ok, no_data, offline, ...); never raises."""
    base = {"farm": farm["farm_id"], "name": farm["name"]}
    if farm.get("kind", "ontop") != "ontop":
        return {**base, "status": "unsupported", "detail": f"farm kind {farm.get('kind')!r} has no farm_metric adapter yet"}
    if params["metric"] in farm["withheld_list"]:
        return {**base, "status": "withheld", "detail": f"{farm['name']} withholds {params['metric']}"}
    if params["metric"] not in farm["signal_list"]:
        return {**base, "status": "not_published", "detail": f"{farm['name']} does not publish {params['metric']}"}
    endpoint = farm[ENDPOINT_COLUMN]
    query = aggregate_query(params)
    timeout = float(farm.get("timeout_s") or 60)
    try:
        rows = assistant.run_sparql(query, endpoint, timeout=timeout)[1]
        rated = None
        if params["metric"] == "Power":
            rated_rows = assistant.run_sparql(RATED_QUERY, endpoint, timeout=timeout)[1]
            rated = float(rated_rows[0]["rated"]) if rated_rows and rated_rows[0]["rated"] else None
    except assistant.Unavailable as error:
        return {**base, "status": "offline", "detail": f"{farm['name']} did not answer ({error})", "sparql": query}
    except ValueError as error:
        return {**base, "status": "error", "detail": f"{farm['name']} rejected the query: {str(error)[:300]}", "sparql": query}
    out = []
    for row in rows[: int(farm.get("max_rows") or 50)]:
        out.append({**base, **{k: v for k, v in row.items() if k in ("turbine", "month")},
                    "unit": unit_name(row["unit"]), "value": round(float(row["value"]), 2), "readings": int(float(row["readings"]))})
    detail = ""
    if not out:
        span = data_range(farm)
        detail = (f"{farm['name']} has no readings in that window. Its data covers {span[0]} to {span[1]}"
                  if span else f"{farm['name']} has no readings in that window")
    return {**base, "status": "ok" if out else "no_data", "detail": detail,
            "rows": out, "rated_kw": rated, "sparql": query, "endpoint": endpoint}


def farm_metric(params, farms=None):
    """Run the aggregate at every requested farm in parallel and merge. Returns the labelled rows,
    the farms that did not answer (with reasons), caveats, and each farm's query."""
    farms = farms or registry()
    p = validate(params, farms)
    with ThreadPoolExecutor(max_workers=max(1, len(p["farms"]))) as pool:
        results = list(pool.map(lambda fid: one_farm(farms[fid], p), p["farms"]))
    answered = [r for r in results if r["status"] == "ok"]
    if p["metric"] == "Power" and p["agg"] == "avg" and p["group_by"] == "farm":
        for r in answered:
            if r.get("rated_kw") and r["rows"]:
                r["rows"][0]["rated_kw"] = round(r["rated_kw"], 1)
                r["rows"][0]["capacity_factor"] = round(r["rows"][0]["value"] / r["rated_kw"], 3)
    rows = [row for r in answered for row in r["rows"]]
    missing = [{"farm": r["farm"], "name": r["name"], "status": r["status"], "detail": r["detail"]}
               for r in results if r["status"] != "ok"]  # includes no_data, with the farm's covered date range
    caveats = []
    units = {row["unit"] for row in rows}
    if len(units) > 1:  # never merge unequal units
        caveats.append(f"Farms report different units ({', '.join(sorted(units))}); the values are not comparable and are not ranked.")
    elif len(answered) > 1 and p["group_by"] == "farm":
        caveats.append("Ranking is indicative only: the farms use the same turbine class, so differences mostly reflect wind resource, availability and the covered period, not turbine quality."
                       + (" Capacity factor (average power / rated power) is shown to normalise." if "capacity_factor" in (rows[0] if rows else {}) else ""))
    if missing:
        caveats.append("No measurement from: " + "; ".join(m["detail"] for m in missing) + ". The result covers only the farms that answered.")
    return {"params": p, "rows": rows, "missing": missing, "caveats": caveats,
            "farms": [{"farm": r["farm"], "name": r["name"], "status": r["status"], "endpoint": r.get("endpoint"), "sparql": r.get("sparql")} for r in results]}
