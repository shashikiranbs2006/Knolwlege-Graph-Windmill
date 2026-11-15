"""One assistant for both use cases: plain-English question -> a tool -> grounded reply.

The LLM first picks one of two tools (route):
    ask the hub          knowledge questions (alarms, faults, repairs, governance): SPARQL on the hub
                         (Apache Jena Fuseki), one named graph per farm plus the shared curated knowledge
    ask the farms        measurement questions (average power, wind speed ...): farm_metric, a small
                         validated tool (farm_metrics.py) that asks each farm's own Ontop for one
                         aggregate, live. The LLM only picks parameters; it never writes this SPARQL.
The scope is detected from the question's wording (detect_scope), unless the user picks it:

    scope "farm"   use case 1: only farm A's named graph (plus the shared curated knowledge)
    scope "cross"  use case 2: every farm graph, joined through the hub's farm registry

If the hub cannot be reached, farm-A questions fall back to farm A's own Ontop endpoint
("answered at the farm"), so each kind of question fails on its own.

Works with any OpenAI-compatible chat API (Gemini by default; Groq: LLM_BASE_URL=https://api.groq.com/openai/v1).
Environment:
    LLM_API_KEY         required (keep it in .env, never in code)
    LLM_BASE_URL        default https://generativelanguage.googleapis.com/v1beta/openai/
    LLM_MODEL           default gemini-flash-latest
    HUB_ENDPOINT        hub SPARQL endpoint (default http://127.0.0.1:3030/hub/sparql)
    FARM_A_ENDPOINT     farm A's Ontop endpoint, used only as the at-the-farm fallback
                        (default http://127.0.0.1:8080/sparql; empty disables the fallback)
    FARM_A_GRAPH        farm A's named graph in the hub

The model only ever writes read-only SELECT queries; the graph supplies every fact in the
answer, and the reply is written from the returned rows only.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
MODEL = os.getenv("LLM_MODEL", "gemini-flash-latest")
HUB = os.getenv("HUB_ENDPOINT", "http://127.0.0.1:3030/hub/sparql")
FARM_A = os.getenv("FARM_A_ENDPOINT", "http://127.0.0.1:8080/sparql")
FARM_A_GRAPH = os.getenv("FARM_A_GRAPH", "https://w3id.org/wind-core/data/graph/kelmarsh")
ONTOLOGY = Path(os.getenv("VKG_ONTOLOGY_FILE", "vkg/ontology.ttl"))
EXAMPLES = Path(os.getenv("ASSISTANT_EXAMPLES", "queries"))
MAX_ROWS = 50
BUDGET = float(os.getenv("ASK_BUDGET_SECONDS", "100"))  # hard limit for one question, so the page never waits forever
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT_SECONDS", "40"))
_clock = threading.local()
FORBIDDEN = re.compile(r"\b(INSERT|DELETE|LOAD|CLEAR|DROP|CREATE|COPY|MOVE|ADD|SERVICE|FROM)\b", re.I)
SCOPES = {
    "farm": {"endpoint": lambda: HUB, "examples": "farm_a_*.rq", "where": "the hub, farm A's graph"},
    "cross": {"endpoint": lambda: HUB, "examples": "hub_0[123]_*.rq", "where": "the hub, all farm graphs"},
    "farm-direct": {"endpoint": lambda: FARM_A, "examples": "0[1235]_*.rq", "where": "farm A's own Ontop endpoint"},
}
FARM_A_ID = FARM_A_GRAPH.rstrip("/").rsplit("/", 1)[-1]  # "kelmarsh": the only farm a "This farm" question may touch


# Scope detection from the question's wording. Anything naming another farm, several farms or a
# comparison between farms is use case 2 ("cross"); everything else is about farm A ("farm").
OTHER_FARMS = re.compile(r"\b(penmanshiel|farm\s*[bc]|farm-c|neo4j\s+farm)\b", re.I)
CROSS_WORDS = re.compile(
    r"\b(both|all|each|every|across|between|other|per|three|two)\s+(of\s+the\s+)?(wind\s*)?farms?\b"
    r"|\bfarms\b|\bcross[- ]farm\b|\bwhich\s+farm\b", re.I)
FARM_A_WORDS = re.compile(r"\b(kelmarsh(\s+\d+)?|farm\s*a|this\s+farm|our\s+farm)\b", re.I)


def detect_scope(question):
    """Return (scope, reason) for a question: "cross" if it mentions another farm or several farms,
    otherwise "farm" (farm A, Kelmarsh). The reason quotes the words that decided it."""
    for pattern in (OTHER_FARMS, CROSS_WORDS):
        found = pattern.search(question)
        if found:
            return "cross", f'the question mentions "{found.group(0)}"'
    found = FARM_A_WORDS.search(question)
    if found:
        return "farm", f'the question mentions "{found.group(0)}"'
    return "farm", "no other farm is mentioned, so it is about farm A (Kelmarsh)"


class Unavailable(Exception):
    """The SPARQL endpoint could not be reached (as opposed to rejecting the query)."""


def vocabulary():
    """A compact, model-readable summary of wind-core built from ontology.ttl."""
    lines = []
    for match in re.finditer(r"(?ms)^(wind:\w+) a ([\w:]+)(.*?)\s\.$", ONTOLOGY.read_text(encoding="utf-8")):
        term, kind, rest = match.groups()
        details = []
        for key in ("rdfs:subClassOf", "rdfs:domain", "rdfs:range"):
            found = re.search(key + r" ([\w:]+)", rest)
            if found:
                details.append(f"{key.split(':')[1]} {found.group(1)}")
        comment = re.search(r'rdfs:comment "([^"]*)"', rest)
        kind = {"owl:Class": "class", "owl:ObjectProperty": "object property", "owl:DatatypeProperty": "datatype property",
                "sosa:ObservableProperty": "observable property (use with sosa:observedProperty)"}.get(kind, kind)
        lines.append(f"- {term} ({kind}{'; ' + ', '.join(details) if details else ''}): {comment.group(1) if comment else ''}")
    return "\n".join(lines)


def examples(scope):
    return "\n\n".join(path.read_text(encoding="utf-8").strip() for path in sorted(EXAMPLES.glob(SCOPES[scope]["examples"])))


def system_prompt(scope):
    rules = [
        "You translate wind-farm engineering questions into ONE SPARQL 1.1 SELECT query over the wind-core knowledge graph.",
        "Use only the vocabulary below plus sosa:, prov:, skos:, xsd:. Declare every prefix you use.",
        "PREFIX wind: <https://w3id.org/wind-core/ontology#> ; PREFIX sosa: <http://www.w3.org/ns/sosa/> ; PREFIX prov: <http://www.w3.org/ns/prov#> ; PREFIX skos: <http://www.w3.org/2004/02/skos/core#> ; PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>",
        "Instance IRIs follow https://w3id.org/wind-core/data/farm/{farm_id}/turbine/{turbine_id}; faults are https://w3id.org/wind-core/data/fault/{FaultCode}. Prefer matching names with wind:name instead of guessing IRIs.",
        "Match alarm or fault text with FILTER(CONTAINS(LCASE(?text), \"lower-case words\")); alarm messages look like 'High temp. gear bearing 2', 'Low gearbox oil pressure', 'Overload generator fan 1'.",
        "Confidence of an event->fault link is on the wind:Diagnosis node (wind:diagnosedEvent, wind:diagnosedFault, wind:confidence).",
        "Whenever maintenance, faults or diagnoses are returned, also return their wind:recordKind as ?recordKind.",
        f"Never return more than {MAX_ROWS} rows: always end with LIMIT {MAX_ROWS} or less. Use aggregates for counts.",
        "Never use SERVICE, FROM or FROM NAMED.",
        "Reply with the query only, inside a ```sparql code block.",
    ]
    if scope == "farm":
        rules.append(f"The hub keeps each farm in its own named graph. This question is about farm A (Kelmarsh) ONLY: put every farm data pattern inside GRAPH <{FARM_A_GRAPH}> {{ ... }} and never use any other GRAPH. "
                     "Curated cross-farm knowledge (alarm types, wind:suggestsFault with RDF 1.2 annotations) is in the default graph, outside GRAPH.")
    elif scope == "cross":
        rules.append("The hub keeps each farm in its own named graph. Compare farms by binding the graph from the registry in the default graph: "
                     "?farmIri wind:farmId ?farm ; wind:graph ?g . GRAPH ?g { ...farm patterns... }. Always return ?farm. "
                     "Curated cross-farm knowledge is in the default graph, outside GRAPH: the shared fault catalogue (read fault names there: ?f wind:name ?fault, outside GRAPH, because not every farm graph carries them) and alarm types with wind:suggestsFault and RDF 1.2 annotations {| wind:confidence ?c |}.")
    else:
        rules.append("You query farm A's own Ontop endpoint directly. Do not use GRAPH.")
    return "\n".join(rules) + "\n\nVocabulary:\n" + vocabulary() + "\n\nWorking example queries:\n" + examples(scope)


def remaining():
    """Seconds left in this question's time budget (large when no budget is running, e.g. direct chat calls)."""
    deadline = getattr(_clock, "deadline", None)
    return 10**6 if deadline is None else deadline - time.monotonic()


def api_keys():
    """Every configured key, in order: LLM_API_KEYS (comma or space separated) then LLM_API_KEY. Duplicates dropped."""
    raw = re.sub(r"[;\s]+", ",", os.getenv("LLM_API_KEYS", "")) + "," + os.getenv("LLM_API_KEY", "")
    keys = []
    for key in raw.split(","):
        key = key.strip()
        if key and key not in keys:
            keys.append(key)
    return keys


_current = 0  # index of the key that worked last, so later questions start there instead of on a limited key


def describe(error):
    code = getattr(error, "code", None)
    if code == 429:
        return "rate limited (429)"
    if code in (401, 403):
        return f"rejected (HTTP {code})"
    if code:
        return f"server error (HTTP {code})"
    return "unreachable or timed out"


def chat(messages):
    """One chat completion. Tries each configured key in turn: a rate limit (429), overload or 5xx, a
    timeout or a rejected key moves on to the next key; after a full pass of retryable failures it
    backs off briefly and goes round once more. Keys are never logged or included in error messages."""
    global _current
    keys = api_keys()
    if not keys:
        raise RuntimeError("No LLM_API_KEY is set. Add LLM_API_KEY=... (or LLM_API_KEYS=key1,key2 for backups) to the .env file and restart the site.")
    payload = json.dumps({"model": MODEL, "messages": messages, "temperature": 0}).encode()
    problems = {}
    for round_number in range(2):
        for step in range(len(keys)):
            index = (_current + step) % len(keys)
            left = remaining()
            if left < 3:
                raise RuntimeError(f"Timed out: the question took longer than {BUDGET:.0f} seconds. The language model was too slow or overloaded"
                                   + (f" ({'; '.join(f'key {i + 1}: {m}' for i, m in sorted(problems.items()))})." if problems else ".") + " Try again in a minute.")
            request = urllib.request.Request(
                BASE_URL.rstrip("/") + "/chat/completions", data=payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {keys[index]}",
                         "User-Agent": "vkg-windmill-assistant/1.0"},  # Cloudflare-fronted APIs (Groq) block Python-urllib's default UA (error 1010)
            )
            try:
                with urllib.request.urlopen(request, timeout=min(LLM_TIMEOUT, left)) as response:
                    body = json.load(response)
                _current = index
                return body["choices"][0]["message"]["content"] or ""
            except urllib.error.HTTPError as error:
                error.read()
                if error.code == 400 or error.code == 404:  # a bad request or model name is not fixed by another key
                    raise RuntimeError(f"LLM API error {error.code}: check LLM_MODEL and LLM_BASE_URL.") from error
                problems[index] = describe(error)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as error:
                problems[index] = describe(error)
        if all("rejected" in m for m in problems.values()):
            break
        if round_number == 0 and remaining() > 8:
            time.sleep(2)  # short backoff before going round the keys again
    summary = "; ".join(f"key {i + 1} of {len(keys)}: {m}" for i, m in sorted(problems.items()))
    if all("rejected" in m for m in problems.values()):
        raise RuntimeError(f"The language model rejected the API key(s) ({summary}). Check LLM_API_KEY / LLM_API_KEYS in .env and restart the site.")
    raise RuntimeError(f"Every configured language-model key failed ({summary}). The provider may be overloaded: wait a minute and ask again, or add another key to LLM_API_KEYS.")


def extract(text):
    block = re.search(r"```(?:sparql)?\s*(.*?)```", text, re.S | re.I)
    return (block.group(1) if block else text).strip()


def code_only(sparql):
    """The query without string literals, IRIs and comments, for keyword checks."""
    text = re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|#[^\n]*', '""', re.sub(r"<[^<>\s]*>", "<>", sparql))
    return re.sub(r"[?$]\w+|\b\w*:\w+", "_", text)  # variables and prefixed names are not keywords


def check(sparql, scope="farm"):
    if not re.search(r"\bSELECT\b", sparql, re.I) or FORBIDDEN.search(code_only(sparql)):
        raise ValueError("The generated query is not a read-only SELECT query on one endpoint (no updates, SERVICE, FROM or FROM NAMED).")
    graphs = re.findall(r"\bGRAPH\s+(<[^>]*>|\?\w+|\$\w+|\w*:\w*)", re.sub(r'"(?:[^"\\]|\\.)*"', '""', sparql), re.I)
    if scope == "farm":
        if not graphs or any(graph != f"<{FARM_A_GRAPH}>" for graph in graphs):
            raise ValueError(f"For a farm A question, every farm pattern must be inside GRAPH <{FARM_A_GRAPH}> and no other graph may be used.")
    elif scope == "cross":
        if not graphs:
            raise ValueError("Cross-farm questions must read the farm graphs with GRAPH ?g (bound from ?farmIri wind:graph ?g).")
    elif graphs:
        raise ValueError("GRAPH is not used at the farm endpoint.")
    limits = [int(value) for value in re.findall(r"\bLIMIT\s+(\d+)", sparql, re.I)]
    if not limits:
        sparql += f"\nLIMIT {MAX_ROWS}"
    elif max(limits) > MAX_ROWS:
        raise ValueError(f"Use LIMIT {MAX_ROWS} or less.")
    return sparql


def run_sparql(sparql, endpoint, timeout=120):
    request = urllib.request.Request(
        endpoint, data=urllib.parse.urlencode({"query": sparql}).encode(),
        headers={"Accept": "application/sparql-results+json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in (502, 503, 504):
            raise Unavailable(f"{endpoint} answered HTTP {error.code}") from error
        raise ValueError(error.read().decode("utf-8", errors="replace").strip()[:1500] or f"HTTP {error.code}") from error
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as error:
        raise Unavailable(f"{endpoint} is not reachable ({getattr(error, 'reason', error)})") from error
    columns = result["head"]["vars"]
    rows = [{column: binding.get(column, {}).get("value", "") for column in columns} for binding in result["results"]["bindings"]]
    return columns, rows


def at_the_farm(sparql):
    """Turn a farm-A hub query into the equivalent query for farm A's Ontop endpoint by
    unwrapping GRAPH <farm A> { ... } into a plain group. Returns None if that is not possible."""
    opener = re.compile(r"\bGRAPH\s+<" + re.escape(FARM_A_GRAPH) + r">\s*\{", re.I)
    out, position = [], 0
    for match in opener.finditer(sparql):
        if match.start() < position:
            return None
        out.append(sparql[position:match.start()] + "{")
        position = match.end()
    if not out:
        return None
    result = "".join(out) + sparql[position:]
    return None if re.search(r"\bGRAPH\b", code_only(result), re.I) else result


def answer_text(question, sparql, columns, rows, scope):
    if not rows:
        return "The knowledge graph returned no rows for this question, so I can't answer it from the data."
    focus = ("Each row is one farm; compare farms only using these rows. " if scope == "cross" else "")
    prompt = (
        "Answer the engineer's question in 2-4 plain sentences using ONLY the result rows below. " + focus +
        "Quote numbers, dates, fault names and actions exactly as given. If any row has recordKind synthetic_demo, say clearly that those maintenance/repair records are synthetic demo data, not real repairs; "
        "if curated_demo, say the fault link or confidence comes from an illustrative demo rule. Do not add causes, fixes or numbers that are not in the rows.\n\n"
        f"Question: {question}\nSPARQL used:\n{sparql}\nColumns: {columns}\nRows (JSON): {json.dumps(rows[:30])}"
    )
    text = chat([{"role": "user", "content": prompt}]).strip()
    kinds = {row.get("recordKind") for row in rows}
    if "synthetic_demo" in kinds and "synthetic" not in text.lower():  # the demo must always say so
        text += " Note: the maintenance and repair records in this answer are synthetic demo data, not real repairs."
    return text


def generate(question, scope, attempts):
    """LLM loop: write a query for the scope, run it, feed errors back. Raises Unavailable if the endpoint is down."""
    endpoint = SCOPES[scope]["endpoint"]()
    messages = [{"role": "system", "content": system_prompt(scope)}, {"role": "user", "content": question}]
    trail = []
    for _ in range(attempts):
        reply = chat(messages)
        sparql = extract(reply)
        try:
            sparql = check(sparql, scope)
            columns, rows = run_sparql(sparql, endpoint)
        except ValueError as error:
            trail.append({"sparql": sparql, "error": str(error)})
            messages += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": f"That query failed: {str(error)[:800]}\nReturn a corrected query."}]
            continue
        if not rows and len(trail) < attempts - 1:
            trail.append({"sparql": sparql, "error": "0 rows"})
            messages += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": "That query returned 0 rows. If the question can be answered, relax overly specific filters or IRIs and try once more."}]
            continue
        return sparql, columns, rows, trail, endpoint
    raise ValueError("I could not produce a working query: " + (trail[-1]["error"] if trail else "no reply"))


def route_prompt(scope, farms):
    listing = "\n".join(f"- {fid}: {f['name']}, signals {', '.join(f['signal_list'])}" for fid, f in farms.items())
    limit = (f'This question is limited to farm "{FARM_A_ID}": use only that farm.' if scope == "farm" else "Use every farm that is needed (usually all).")
    return (
        "You choose how to answer a wind-farm engineering question. Reply with ONE JSON object and nothing else.\n"
        'Tool "hub": knowledge questions (alarms, faults, diagnoses, repairs, work orders, turbines, what a farm publishes or withholds). Reply {"tool": "hub"}.\n'
        'Tool "farm_metric": measurement questions answered from the farms\' live SCADA readings (average, minimum, maximum, total or count of power, wind speed, exported energy, gear oil temperature), optionally per farm, per turbine or per month. Reply '
        '{"tool": "farm_metric", "metric": "Power|WindSpeed|EnergyExport|GearOilTemperature", "agg": "avg|min|max|sum|count", "group_by": "farm|turbine|month", "farms": ["farm id", ...], "start": "YYYY-MM-DD or null", "end": "YYYY-MM-DD or null (exclusive)"}.\n'
        "Use group_by farm unless the question asks per turbine or per month. Leave start and end null unless the question names a period. Never write SPARQL or endpoints.\n"
        "If a question needs both knowledge and measurements, pick the main one.\n" + limit + "\nFarms:\n" + listing)


def route(question, scope):
    """Let the LLM choose the tool. Returns ("hub", None) or ("farm_metric", validated parameters).
    The scope rule is enforced here in code: a "This farm" question can only ever touch farm A."""
    import farm_metrics
    farms = farm_metrics.registry()
    messages = [{"role": "system", "content": route_prompt(scope, farms)}, {"role": "user", "content": question}]
    for _ in range(2):
        reply = chat(messages)
        try:
            choice = json.loads(re.search(r"\{.*\}", reply, re.S).group(0))
        except (AttributeError, ValueError):
            return "hub", None
        if choice.get("tool") != "farm_metric":
            return "hub", None
        if scope == "farm":
            choice["farms"] = [FARM_A_ID]
        try:
            return "farm_metric", farm_metrics.validate(choice, farms)
        except ValueError as error:
            messages += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": f"Invalid parameters: {error} Reply with a corrected JSON object."}]
    return "hub", None


def farm_answer_text(question, result):
    rows, missing = result["rows"], result["missing"]
    if not rows:
        reasons = "; ".join(m["detail"] for m in missing) or "the farms returned no readings for that request"
        return f"No measurement could be returned: {reasons}."
    prompt = (
        "Answer the engineer's question in 2-4 plain sentences using ONLY the measurement rows below. Each row is one aggregate computed live at that farm's own endpoint. "
        "Quote values, units and farm names exactly. Mention the number of readings. Include the caveats below in your own words. Do not add numbers that are not in the rows.\n\n"
        f"Question: {question}\nParameters: {json.dumps(result['params'])}\nCaveats: {json.dumps(result['caveats'])}\nRows (JSON): {json.dumps(rows[:50])}")
    text = chat([{"role": "user", "content": prompt}]).strip()
    for m in missing:  # an unanswered farm must always be named
        if m["name"].lower() not in text.lower():
            text += f" Note: {m['detail']}."
    return text


def ask_farms(question, scope, params):
    """Run the farm_metric tool and shape the result like a hub answer so the page can show it."""
    import farm_metrics
    result = farm_metrics.farm_metric(params)
    called = [f for f in result["farms"] if f["endpoint"]]
    names = ", ".join(f["name"] for f in result["farms"])
    where = "live queries to each farm's own Ontop endpoint (" + names + ")"
    columns = ["farm", "turbine", "month", "unit", "value", "readings", "rated_kw", "capacity_factor"]
    columns = [c for c in columns if any(c in row for row in result["rows"])] or ["farm", "unit", "value", "readings"]
    return {"question": question, "scope": scope, "tool": "farm_metric", "via": "farm-live", "where": where, "notice": " ".join(result["caveats"]),
            "sparql": "\n\n".join(f"# {f['name']} ({f['status']})\n{f['sparql']}" for f in result["farms"] if f["sparql"]),
            "farm_sparql": None, "columns": columns, "rows": result["rows"], "answer": farm_answer_text(question, result),
            "attempts": [], "model": MODEL, "endpoint": ", ".join(f["endpoint"] for f in called) or "none reachable",
            "farm_metric": {"params": result["params"], "farms": [{k: f[k] for k in ("farm", "name", "status")} for f in result["farms"]],
                            "missing": result["missing"], "caveats": result["caveats"]}}


def ask(question, scope="auto", attempts=3):
    """Answer a question. scope "auto" detects it from the wording; "farm" or "cross" is the
    user's explicit choice. A farm-scoped question that names another farm is refused, and every
    generated farm-scoped query is checked to stay inside farm A's graph."""
    _clock.deadline = time.monotonic() + BUDGET
    try:
        return _ask(question, scope, attempts)
    finally:
        _clock.deadline = None


def _ask(question, scope, attempts):
    question = question.strip()
    if scope not in ("auto", "farm", "cross"):
        raise ValueError("Unknown scope: use auto, farm or cross.")
    if not question or len(question) > 1000:
        raise ValueError("Ask a question of up to 1,000 characters.")
    detected, reason = detect_scope(question)
    chosen_by = "you chose it"
    if scope == "auto":
        scope, chosen_by = detected, "detected: " + reason
    elif scope == "farm" and OTHER_FARMS.search(question):
        raise ValueError(f'This question names another farm ("{OTHER_FARMS.search(question).group(0)}"), but the scope is "This farm", '
                         "which may only read farm A's data. Switch the scope to All farms (or Auto) to compare farms.")
    tool, params = route(question, scope)
    if tool == "farm_metric":
        reply = ask_farms(question, scope, params)
        reply["scope_reason"] = chosen_by
        return reply
    via, notice = scope, ""
    try:
        sparql, columns, rows, trail, endpoint = generate(question, scope, attempts)
    except Unavailable as error:
        if scope != "farm" or not FARM_A:
            raise RuntimeError(f"The hub is unavailable, so cross-farm questions cannot be answered right now: {error}") from error
        notice = f"The hub is unavailable ({error}), so this was answered at the farm, on farm A's own Ontop endpoint."
        via = "farm-direct"
        try:
            sparql, columns, rows, trail, endpoint = generate(question, via, attempts)
        except Unavailable as farm_error:
            raise RuntimeError(f"Neither the hub nor farm A's own endpoint is reachable: {farm_error}") from farm_error
    if not any(any(value for value in row.values()) for row in rows) and via == "farm" and FARM_A and remaining() > 20:
        # The hub holds events and maintenance, not the SCADA time series; those stay at the farm.
        try:
            f_sparql, f_columns, f_rows, f_trail, f_endpoint = generate(question, "farm-direct", 2)
            if f_rows:
                sparql, columns, rows, trail, endpoint, via = f_sparql, f_columns, f_rows, trail + f_trail, f_endpoint, "farm-direct"
                notice = "The hub had no rows for this (it does not hold the SCADA time series), so it was answered at the farm, on farm A's own Ontop endpoint."
        except (Unavailable, ValueError):
            pass
    return {"question": question, "scope": scope, "tool": "hub", "scope_reason": chosen_by, "via": via, "where": SCOPES[via]["where"], "notice": notice,
            "sparql": sparql, "farm_sparql": sparql if via == "farm-direct" else (at_the_farm(sparql) if scope == "farm" else None),
            "columns": columns, "rows": rows, "answer": answer_text(question, sparql, columns, rows, scope),
            "attempts": trail, "model": MODEL, "endpoint": endpoint}


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    scope = {"--cross": "cross", "--farm": "farm"}.get(args[0], "auto") if args else "auto"
    args = args[1:] if scope != "auto" else args
    print(json.dumps(ask(" ".join(args) or "What usually causes high gear bearing temperature on a gearbox, and what fixed it last time?", scope), indent=2))
