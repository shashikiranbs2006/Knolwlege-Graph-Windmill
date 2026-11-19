"""farm_metric: live measurement aggregates asked of each farm's own Ontop endpoint.

The hub keeps knowledge (alarms, faults, repairs). Measurements (power, wind speed) stay at the
farm, so this tool asks each farm for ONE aggregate, in parallel, and merges the numbers. The LLM
never writes this SPARQL and never sees an endpoint: it only picks parameters (metric, aggregate,
farms, grouping, window), which are validated here against the farm registry (hub/farms.csv).

    metric     Power | WindSpeed | EnergyExport | GearOilTemperature (must be in the farm's `signals`
               and not in its `withheld_signals`)
    agg        avg | min | max | sum | count
    group_by   farm (default) | turbine | month | wind_speed
               wind_speed = average Power per 1 m/s of wind speed (needs Power and WindSpeed): the power
               curve, answered with a second column for running readings only (power above 50 kW)
               underperformance = per-turbine shortfall in high wind (wind speed at or above min_wind, default
               10 m/s): each turbine's mean power against the farm's expected power for the same 1 m/s bins
               (the fleet mean of that bin), plus the share of high-wind readings under 50 kW
    min_wind   optional high-wind threshold in m/s for underperformance (default 10)
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
GROUPS = ("farm", "turbine", "month", "wind_speed", "underperformance")
HIGH_WIND_MS = 10.0  # default "high wind" threshold (m/s), near where this turbine class reaches rated power
RUNNING_KW = 50  # below this the turbine is stopped, faulted or curtailed
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
    if group_by == "wind_speed":  # the power curve: always average Power against WindSpeed
        metric, agg = "Power", "avg"
    if group_by == "underperformance":
        metric, agg = "Power", "avg"
    try:
        min_wind = float(params.get("min_wind") or HIGH_WIND_MS)
    except (TypeError, ValueError):
        raise ValueError("min_wind must be a number of m/s, such as 10.")
    if not 0 <= min_wind <= 40:
        raise ValueError("min_wind must be between 0 and 40 m/s.")
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
    extra = {"min_wind": min_wind} if group_by == "underperformance" else {}
    return {"metric": metric, "agg": agg, "group_by": group_by, **extra, "farms": list(dict.fromkeys(chosen)), **window}


def power_curve_query(running_only=False):
    """Average Power per 1 m/s wind-speed bin. Power and WindSpeed are separate observations of the same
    source row, so they are joined on file + row (fast: a join on time + turbine times out on a full year)."""
    running = f" FILTER(?v > {RUNNING_KW})" if running_only else ""
    return (f"{PREFIXES}SELECT ?bin (AVG(?v) AS ?value) (COUNT(?v) AS ?readings) WHERE {{ "
            "?o sosa:observedProperty wind:Power ; sosa:hasSimpleResult ?v ; wind:sourceFile ?f ; wind:sourceRow ?r . "
            "?w sosa:observedProperty wind:WindSpeed ; sosa:hasSimpleResult ?ws ; wind:sourceFile ?f ; wind:sourceRow ?r . "
            f"BIND(FLOOR(?ws) AS ?bin){running} }} GROUP BY ?bin ORDER BY ?bin LIMIT 200")


def underperformance_query(min_wind):
    """Per turbine and 1 m/s wind bin, at or above min_wind: mean Power, readings, and readings under RUNNING_KW.
    Power and WindSpeed are joined on file + row, as in the power curve. The expected power is derived from these rows."""
    return (f"{PREFIXES}SELECT ?turbine ?bin (AVG(?v) AS ?value) (COUNT(?v) AS ?readings) (SUM(IF(?v < {RUNNING_KW}, 1, 0)) AS ?low) WHERE {{ "
            "?o sosa:observedProperty wind:Power ; sosa:hasSimpleResult ?v ; wind:sourceFile ?f ; wind:sourceRow ?r ; sosa:hasFeatureOfInterest ?tur . ?tur wind:name ?turbine . "
            "?w sosa:observedProperty wind:WindSpeed ; sosa:hasSimpleResult ?ws ; wind:sourceFile ?f ; wind:sourceRow ?r . "
            f"FILTER(?ws >= {min_wind:g}) BIND(FLOOR(?ws) AS ?bin) }} GROUP BY ?turbine ?bin ORDER BY ?turbine ?bin LIMIT 5000")


def underperformance_rows(rows, base, min_wind):
    """Turn per-(turbine, bin) aggregates into one ranked row per turbine. Expected power = the reading-weighted
    fleet mean of each bin, so a turbine is compared with its own farm at the same wind speed."""
    fleet = {}
    for r in rows:
        n, b = int(float(r["readings"])), float(r["bin"])
        total, count = fleet.get(b, (0.0, 0))
        fleet[b] = (total + float(r["value"]) * n, count + n)
    turbines = {}
    for r in rows:
        n, b = int(float(r["readings"])), float(r["bin"])
        t = turbines.setdefault(r["turbine"], {"n": 0, "power": 0.0, "expected": 0.0, "low": 0})
        t["n"] += n
        t["power"] += float(r["value"]) * n
        t["expected"] += fleet[b][0] / fleet[b][1] * n
        t["low"] += int(float(r["low"]))
    out = [{**base, "turbine": name, "unit": "kW", "value": round(t["power"] / t["n"], 1), "expected_kw": round(t["expected"] / t["n"], 1),
            "shortfall_kw": round((t["expected"] - t["power"]) / t["n"], 1),
            "shortfall_pct": round(100 * (t["expected"] - t["power"]) / t["expected"], 1) if t["expected"] else None,
            "low_share_pct": round(100 * t["low"] / t["n"], 1), "readings": t["n"], "min_wind": min_wind}
           for name, t in turbines.items() if t["n"]]
    return sorted(out, key=lambda x: -x["shortfall_kw"])


def aggregate_query(p):
    """The one aggregate query sent to a farm. group_by only changes the grouping."""
    if p["group_by"] == "underperformance":
        return underperformance_query(p["min_wind"])
    if p["group_by"] == "wind_speed":
        return power_curve_query()
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


CATALOG_QUERIES = {
    "signals": PREFIXES + "SELECT DISTINCT ?p ?u WHERE { ?o sosa:observedProperty ?p ; wind:unit ?u } LIMIT 50",
    "turbines": PREFIXES + "SELECT ?n WHERE { ?t a wind:WindTurbine ; wind:name ?n } ORDER BY ?n LIMIT 200",
}
_catalog = {}  # farm id -> (fetched_at, entry)


def farm_catalog(farm):
    """What this farm really holds, read live from its own endpoint (signals with units, turbines, covered dates),
    cross-checked with the registry's published/withheld lists. Cached for 10 minutes. `live` is False when the
    endpoint could not be read; the registry lists are then the only evidence."""
    now = time.monotonic()
    cached = _catalog.get(farm["farm_id"])
    if cached and now - cached[0] < 600:
        return cached[1]
    entry = {"farm": farm["farm_id"], "name": farm["name"], "live": False, "signals": {s: None for s in farm["signal_list"]},
             "withheld": farm["withheld_list"], "turbines": [], "range": None}
    endpoint, timeout = farm[ENDPOINT_COLUMN], min(20.0, float(farm.get("timeout_s") or 20))
    try:
        for row in assistant.run_sparql(CATALOG_QUERIES["signals"], endpoint, timeout=timeout)[1]:
            entry["signals"][row["p"].rsplit("#", 1)[-1]] = unit_name(row["u"])
        entry["turbines"] = [r["n"] for r in assistant.run_sparql(CATALOG_QUERIES["turbines"], endpoint, timeout=timeout)[1]]
        entry["range"] = data_range(farm)
        entry["live"] = True
    except Exception:  # an unreachable farm must not break the question; the catalog says it could not be read
        pass
    _catalog[farm["farm_id"]] = (now, entry)
    return entry


def catalog(farms=None):
    """{farm id: catalog entry} for every registry farm, read in parallel."""
    farms = farms or registry()
    with ThreadPoolExecutor(max_workers=max(1, len(farms))) as pool:
        found = list(pool.map(farm_catalog, farms.values()))
    return {entry["farm"]: entry for entry in found}


def catalog_text(cat):
    """The catalog as plain text for the router and answer prompts."""
    lines = []
    for e in cat.values():
        sig = ", ".join(f"{k} ({v or 'unit unknown'})" for k, v in e["signals"].items()) or "none"
        lines.append(f"- {e['farm']}: {e['name']}. Measured signals: {sig}. "
                     + (f"Withheld by the farm: {', '.join(e['withheld'])}. " if e["withheld"] else "")
                     + (f"{len(e['turbines'])} turbines ({', '.join(e['turbines'][:3])} ...). " if e["turbines"] else "")
                     + (f"Readings from {e['range'][0]} to {e['range'][1]}. " if e["range"] else "")
                     + ("" if e["live"] else "(endpoint could not be read just now; registry list only.)"))
    return "\n".join(lines)


def signal_status(cat, signal, farm=None):
    """'present' | 'withheld' | 'absent' | 'unknown' for a signal, over one farm or all (present if any has it)."""
    entries = [cat[farm]] if farm in cat else list(cat.values())
    states = {("present" if signal in e["signals"] else "withheld" if signal in e["withheld"] else "absent" if e["live"] else "unknown") for e in entries}
    for state in ("present", "withheld", "unknown", "absent"):
        if state in states:
            return state
    return "unknown"


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
    needed = ["Power", "WindSpeed"] if params["group_by"] in ("wind_speed", "underperformance") else [params["metric"]]
    for signal in needed:
        if signal in farm["withheld_list"]:
            return {**base, "status": "withheld", "detail": f"{farm['name']} withholds {signal}"}
        if signal not in farm["signal_list"]:
            return {**base, "status": "not_published", "detail": f"{farm['name']} does not publish {signal}"}
    endpoint = farm[ENDPOINT_COLUMN]
    query = aggregate_query(params)
    timeout = float(farm.get("timeout_s") or 60)
    try:
        rows = assistant.run_sparql(query, endpoint, timeout=timeout)[1]
        running = {}
        if params["group_by"] == "wind_speed":
            running = {float(r["bin"]): r for r in assistant.run_sparql(power_curve_query(True), endpoint, timeout=timeout)[1]}
        rated = None
        if params["metric"] == "Power":
            rated_rows = assistant.run_sparql(RATED_QUERY, endpoint, timeout=timeout)[1]
            rated = float(rated_rows[0]["rated"]) if rated_rows and rated_rows[0]["rated"] else None
    except assistant.Unavailable as error:
        return {**base, "status": "offline", "detail": f"{farm['name']} did not answer ({error})", "sparql": query}
    except ValueError as error:
        return {**base, "status": "error", "detail": f"{farm['name']} rejected the query: {str(error)[:300]}", "sparql": query}
    out = []
    if params["group_by"] == "underperformance":
        out = underperformance_rows(rows, base, params["min_wind"])[: int(farm.get("max_rows") or 50)]
        rows = []
    if params["group_by"] == "wind_speed":
        for row in rows[:60]:
            low, run = float(row["bin"]), running.get(float(row["bin"]))
            out.append({**base, "wind_speed_m_s": f"{int(low)}-{int(low) + 1}", "unit": "kW", "value": round(float(row["value"]), 1),
                        "readings": int(float(row["readings"])),
                        "running_value": round(float(run["value"]), 1) if run else None,
                        "running_readings": int(float(run["readings"])) if run else 0})
        rows = []
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
    elif p["group_by"] == "underperformance":
        caveats.append(f"Wind speed WAS queried: only readings with wind at or above {p['min_wind']:g} m/s count. expected_kw is the farm's own mean power for the same 1 m/s wind bins, "
                       f"shortfall_kw = expected_kw - value (positive = the turbine produced less than its farm peers at the same wind), low_share_pct = share of those readings under {RUNNING_KW} kW "
                       "(stopped, faulted or curtailed). Rows are ranked by shortfall within each farm; the expectation is relative to the farm, not the manufacturer power curve.")
    elif p["group_by"] == "wind_speed":
        caveats.append(f"Each row is the average power in one 1 m/s wind-speed bin (the power curve); running_value counts only readings above {RUNNING_KW} kW, "
                       "so a gap between value and running_value in a bin means stopped or curtailed turbines. Power rises with wind speed until the rated power, then stays flat.")
    elif len(answered) > 1 and p["group_by"] == "farm":
        caveats.append("Ranking is indicative only: the farms use the same turbine class, so differences mostly reflect wind resource, availability and the covered period, not turbine quality."
                       + (" Capacity factor (average power / rated power) is shown to normalise." if "capacity_factor" in (rows[0] if rows else {}) else ""))
    if missing:
        caveats.append("No measurement from: " + "; ".join(m["detail"] for m in missing) + ". The result covers only the farms that answered.")
    return {"params": p, "rows": rows, "missing": missing, "caveats": caveats,
            "farms": [{"farm": r["farm"], "name": r["name"], "status": r["status"], "endpoint": r.get("endpoint"), "sparql": r.get("sparql")} for r in results]}
