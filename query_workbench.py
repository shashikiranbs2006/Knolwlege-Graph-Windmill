"""One site, one question box, one assistant, all on the hub.

One question box serves both use cases. The scope (This farm = farm A only, All farms) is detected
from the wording or chosen with a chip. A collapsed "at the farm" panel keeps farm A's own Ontop
endpoint with its query journey, and farm A questions fall back to it if the hub is down.
Each kind of question checks its own services, so one being down does not take the other with it."""

import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import assistant

ONTOP = os.getenv("ONTOP_ENDPOINT", "http://127.0.0.1:8080")
MAPPING = Path(os.getenv("VKG_MAPPING_FILE", "vkg/mapping.ttl"))
ONTOLOGY = Path(os.getenv("VKG_ONTOLOGY_FILE", "vkg/ontology.ttl"))
SCHEMA = Path(os.getenv("VKG_SCHEMA_FILE", "db/schema.sql"))
HUB = os.getenv("HUB_ENDPOINT", "http://127.0.0.1:3030/hub/sparql")


def ask(path, sparql, accept):
    request = urllib.request.Request(
        ONTOP + path,
        data=urllib.parse.urlencode({"query": sparql}).encode(),
        headers={"Accept": accept},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace").strip()
        raise ValueError(detail or f"Ontop rejected the SPARQL query (HTTP {error.code}).") from error


def prepared(sparql):
    if len(sparql) > 12000 or not re.search(r"\bSELECT\b", sparql, re.I):
        raise ValueError("Enter a SPARQL SELECT query (maximum 12,000 characters).")
    limits = [int(value) for value in re.findall(r"\bLIMIT\s+(\d+)\b", sparql, re.I)]
    if any(value > 100 for value in limits):
        raise ValueError("For this local workbench, use LIMIT 100 or less.")
    if not limits:
        sparql += "\nLIMIT 100"
    return sparql


def answers(sparql):
    sparql = prepared(sparql)
    result = json.loads(ask("/sparql", sparql, "application/sparql-results+json"))
    columns = result["head"]["vars"]
    rows = [{column: binding.get(column, {}).get("value", "") for column in columns} for binding in result["results"]["bindings"]]
    return {"columns": columns, "rows": rows}


def reformulate(sparql):
    sparql = prepared(sparql)
    reformulation = ask("/ontop/reformulate", sparql, "text/plain")
    sql = reformulation[reformulation.find("SELECT "):] if "SELECT " in reformulation else reformulation
    relations = sorted(set(re.findall(r'"([A-Za-z_][A-Za-z_0-9]*)"\s+v\d+\b', sql)))
    schema = SCHEMA.read_text(encoding="utf-8")
    views = {name: definition.strip() for name, definition in re.findall(r"(?is)CREATE (?:OR REPLACE )?VIEW (\w+) AS\s*(.*?);", schema) if name in relations}
    return {"reformulation": reformulation, "relations": relations, "views": views}


def inspect(sparql):
    prepared(sparql)
    mapping = MAPPING.read_text(encoding="utf-8")
    ontology = ONTOLOGY.read_text(encoding="utf-8")
    map_prefixes = dict(re.findall(r"(?m)^@prefix\s+(\w*):\s*<([^>]+)>", mapping))
    query_prefixes = dict(re.findall(r"(?im)^\s*PREFIX\s+(\w*):\s*<([^>]+)>", sparql))

    def iri(token, prefixes):
        if token.startswith("<"):
            return token[1:-1]
        prefix, local = token.split(":", 1)
        return prefixes.get(prefix, "") + local if prefix in prefixes else ""

    # shortcut: index the generated R2RML shape used here; use an RDF parser if its serializer changes.
    rules = []
    for rule in re.finditer(r"(?ms)^<urn:r2rml:([^>]+)> a rr:TriplesMap;(.*?)(?=^<urn:r2rml:|\Z)", mapping):
        targets = set()
        for token in re.findall(r"rr:(?:class|predicate)\s+((?:\w*:[\w-]+)|(?:<[^>]+>))", rule.group(2)):
            targets.add(iri(token, map_prefixes))
        source = re.search(r'rr:sqlQuery\s+"([^"\n]+)"', rule.group(2))
        rules.append({"id": rule.group(1), "targets": targets, "source": source.group(1) if source else "", "snippet": rule.group(0).strip()})

    declarations = {}
    for line in ontology.splitlines():
        match = re.match(r"^(:[\w-]+|\w+:[\w-]+)\s+a\s+", line)
        if match:
            declarations[iri(match.group(1), map_prefixes)] = line.strip()
    known = set(declarations) | {target for rule in rules for target in rule["targets"]}
    tokens = re.findall(r"<[^>]+>|(?<![\w])(?:\w*):[\w-]+", sparql)
    terms = []
    seen = set()
    for token in tokens:
        expanded = iri(token, query_prefixes)
        if expanded in known and expanded not in seen:
            terms.append({"token": token, "iri": expanded, "definition": declarations.get(expanded, "External vocabulary term used by a mapping rule.")})
            seen.add(expanded)
    matches = []
    for rule in rules:
        matched = [term["token"] for term in terms if term["iri"] in rule["targets"]]
        if matched:
            matches.append({"id": rule["id"], "terms": matched, "source": rule["source"], "snippet": rule["snippet"]})
    return {"terms": terms, "mappings": matches}


def execute(sparql):
    answer = answers(sparql)
    trace = reformulate(sparql)
    return answer["columns"], answer["rows"], trace["reformulation"], trace["relations"]


def escape(value):
    return html.escape(str(value), quote=True)


def explanation(rows, relations):
    relation_text = ", ".join(relations) if relations else "the relations shown in the generated SQL"
    parts = [
        "Your SPARQL terms were interpreted using the wind-core ontology and this farm's R2RML mapping.",
        f"Ontop generated the SQL below and read PostgreSQL relations: {relation_text}.",
        f"It returned {len(rows)} row(s) from that live query.",
    ]
    if "measurement_observations" in relations:
        parts.append("measurement_observations is a PostgreSQL view over scada_readings; the original SCADA rows remain in PostgreSQL.")
    files = sorted({(row.get("sourceFile", ""), row.get("sourceRow", "")) for row in rows if row.get("sourceFile")})
    if files:
        first_file, first_row = files[0]
        parts.append(f"The result includes source row references; the first is {first_file}, row {first_row}.")
    else:
        parts.append("This query did not return source file and row variables, so an exact source row cannot be identified from its result.")
    if "synthetic_maintenance" in relations:
        parts.append("This query uses synthetic_maintenance, which contains fabricated demo cases, not operator-confirmed faults or repairs.")
    elif any(row.get("recordKind") == "synthetic_demo" for row in rows):
        parts.append("At least one returned record is synthetic_demo: it is a fabricated example, not an operator-confirmed fault or repair.")
    return " ".join(parts)


REGISTRY = """PREFIX wind: <https://w3id.org/wind-core/ontology#>
SELECT ?farm ?name ?graph ?harvestedAt ?triples WHERE {
  ?f wind:farmId ?farm ; wind:name ?name ; wind:graph ?graph .
  OPTIONAL { ?f wind:harvestedAt ?harvestedAt } OPTIONAL { ?f wind:tripleCount ?triples }
} ORDER BY ?farm"""


def status():
    """Per-scope health: farm A questions need the hub with farm A's graph, or farm A's own endpoint;
    all-farms questions need the hub. Short timeouts so a dead service cannot hang the page."""
    report = {"hub": {"ok": False, "graphs": []}, "farmA": {"ok": False}}
    try:
        report["hub"]["graphs"] = assistant.run_sparql(REGISTRY, HUB, timeout=5)[1]
        report["hub"]["ok"] = True
    except Exception as error:
        report["hub"]["error"] = str(error)
    try:
        assistant.run_sparql("SELECT * WHERE { ?s ?p ?o } LIMIT 1", ONTOP + "/sparql", timeout=5)
        report["farmA"]["ok"] = True
    except Exception as error:
        report["farmA"]["error"] = str(error)
    farm_graph = any(g["graph"] == assistant.FARM_A_GRAPH for g in report["hub"]["graphs"])
    report["scopes"] = {
        "farm": {"ok": report["hub"]["ok"] and farm_graph or report["farmA"]["ok"],
                 "via": "hub" if report["hub"]["ok"] and farm_graph else ("farm" if report["farmA"]["ok"] else "none")},
        "cross": {"ok": report["hub"]["ok"] and len(report["hub"]["graphs"]) > 0, "farms": len(report["hub"]["graphs"])},
    }
    return report


def hub_answers(sparql):
    """Raw SPARQL on the hub for the hub panel (read-only SELECT, LIMIT 100 or less)."""
    sparql = prepared(sparql)
    if re.search(r"(INSERT|DELETE|LOAD|CLEAR|DROP|CREATE|COPY|MOVE|ADD|SERVICE)", assistant.code_only(sparql), re.I):
        raise ValueError("Only read-only SELECT queries, without SERVICE, are allowed here.")
    try:
        columns, rows = assistant.run_sparql(sparql, HUB, timeout=60)
    except assistant.Unavailable as error:
        raise RuntimeError(f"The hub is unavailable: {error}") from error
    return {"columns": columns, "rows": rows}


EXPLAIN_CSS = """
.explain-btn{position:fixed;top:.75rem;right:1rem;z-index:20;margin:0;display:flex;align-items:center;gap:.4rem;padding:.45rem .8rem;background:#175c45;color:#fff;border:0;border-radius:999px;font:inherit;font-size:.9rem;font-weight:700;cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,.25)}
.explain-btn:hover,.explain-btn:focus-visible{background:#0d4734}
.explain-btn svg{width:1.1rem;height:1.1rem}
#explain-dialog{width:min(980px,94vw);max-height:92vh;padding:0;border:1px solid #91a79b;border-radius:6px;background:#fff;color:#17332b}
#explain-dialog::backdrop{background:rgba(16,44,37,.55)}
.xd-head{display:flex;justify-content:space-between;align-items:center;gap:1rem;padding:.9rem 1.25rem;border-bottom:1px solid #d5dfd7;position:sticky;top:0;background:#fff}
.xd-head h2{margin:0;font-size:1.2rem}
.xd-head button{margin:0;padding:.35rem .8rem;background:#e8f0e9;color:#174a35;border:1px solid #9dbda8}
.xd-body{padding:1rem 1.25rem 1.5rem;font-size:.92rem}
.xd-body h3{font-size:1rem;margin:1.1rem 0 .4rem}
.xd-flow{display:flex;flex-direction:column;align-items:stretch;gap:.35rem}
.xd-box{border:2px solid #175c45;background:#eef5ef;padding:.55rem .8rem}
.xd-box b{display:block}
.xd-box small{display:block;color:#3c5d4c;margin-top:.15rem}
.xd-box.llm{border-color:#8a5a00;background:#fff4dc}
.xd-box.hub{border-color:#1f4f8f;background:#e8f0fb}
.xd-box.src{border-style:dashed;background:#f4f6f2}
.xd-box.live{border-color:#1f4f8f;background:#e8f0fb;border-style:double;border-width:4px}
.xd-arrow{text-align:center;color:#175c45;font-weight:700;line-height:1}
.xd-arrow small{font-weight:400;color:#52685e;margin-left:.4rem}
.xd-row{display:grid;grid-template-columns:repeat(3,1fr);gap:.6rem}
.xd-lane{border:1px dashed #91a79b;padding:.7rem;margin-top:.4rem;background:#fafbf9}
.xd-note{background:#fff4dc;border-left:4px solid #a86000;padding:.5rem .75rem;margin:.8rem 0 0}
.xd-body ul{margin:.3rem 0;padding-left:1.2rem}
.xd-cards{display:grid;grid-template-columns:1fr 1fr;gap:.75rem}
.xd-card{border:1px solid #d5dfd7;border-left:4px solid #175c45;background:#fafbf9;padding:.6rem .8rem;font-size:.9rem}
.xd-card+.xd-card{border-left-color:#8a5a00}
.xd-body .service{margin:.3rem 0;font-size:.9rem}
.xd-scroll{overflow-x:auto;border:1px solid #d5dfd7;border-radius:6px;margin:.4rem 0}
.xd-split{display:grid;grid-template-columns:1.4fr 1fr;gap:.75rem;align-items:start}
.xd-col{border:2px solid #1f4f8f;background:#f1f6fc;padding:.5rem .8rem .7rem;font-size:.88rem}
.xd-col.livecol{border:4px double #0b7a55;background:#e9f7f0}
.xd-col h4{margin:.1rem 0 .3rem;font-size:1rem}
.xd-col h4 small{display:block;font-weight:400;color:#52685e}
.xd-col ul{margin:.2rem 0;padding-left:1.1rem}.xd-col li{margin:.35rem 0}
@media(max-width:700px){.xd-split{grid-template-columns:1fr}}
@media(max-width:700px){.xd-cards{grid-template-columns:1fr}}
@media(max-width:700px){.xd-row{grid-template-columns:1fr}}
"""

EXPLAIN_HTML = """<button type="button" id="explain-open" class="explain-btn" aria-haspopup="dialog" aria-controls="explain-dialog"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M12 11v6M12 7.5v.01" stroke-linecap="round"/></svg>Explanation</button>
<dialog id="explain-dialog" aria-labelledby="explain-title"><div class="xd-head"><h2 id="explain-title">How a question is answered</h2><button type="button" id="explain-close">Close</button></div><div class="xd-body">
<h3 style="margin-top:.2rem">Service status</h3>
<p class="service" data-status="farm" role="status">Checking the hub and farm A…</p>
<p class="service" data-status="cross" role="status">Checking the hub…</p>
<h3>How the data is arranged</h3>
<div class="xd-cards">
<div class="xd-card"><strong>Data stays at the farm.</strong> Each farm keeps its own database and publishes it through its own Ontop endpoint, a virtual graph: nothing is copied at the farm. Only the validated hub is shared.</div>
<div class="xd-card"><strong>The hub is shared.</strong> It holds one named graph per farm (harvested from that farm's Ontop endpoint and checked against the SHACL shapes before loading), plus curated alarm-to-fault knowledge with confidence scores as RDF 1.2 annotations. SCADA time series stay at the farm and are asked live.</div>
</div>
<h3>What the hub handles, and what the metric tool handles</h3>
<div class="xd-split">
<div class="xd-col hubcol"><h4>Handled by the hub <small>knowledge records</small></h4><ul>
<li><strong>Turbines, components and which farm they belong to.</strong> Small, and cross-farm questions need them joined.</li>
<li><strong>Alarms, faults, diagnoses and maintenance fixes.</strong> These are the cross-farm joins, and they ran about 12 times faster on the hub (0.2 s versus 2.5 s).</li>
<li><strong>Curated alarm-to-fault rules, confidence scores and the shared fault catalogue.</strong> This knowledge lives only at the hub.</li>
<li><strong>Farm registry, and who publishes or withholds what (governance).</strong> Access rules and the SHACL check run once in one place.</li>
<li><strong>Farm C (Neo4j) fault names.</strong> They are exported into the hub because the metric tool does not cover farm C yet.</li>
<li><strong>Questions when a farm is offline.</strong> The hub answers alone from its last good copy.</li></ul></div>
<div class="xd-col livecol"><h4>Handled by the metric tool <small>live farm queries</small></h4><ul>
<li><strong>Power, wind speed and other sensor readings, and their averages per farm, turbine or month.</strong> They are millions of rows, too big to copy, and copying them would break the "raw data stays at the farm" promise.</li>
<li><strong>Anything that must be fresh to the second.</strong> The hub copy can be out of date.</li>
<li><strong>Farm A-only questions with no hub data.</strong> Farm A's own Ontop can answer them directly.</li></ul></div>
</div>
<h3>How a question is answered</h3>
<p>New to RDF? An <strong>ontology</strong> is the shared list of terms (turbine, alarm, fault). A <strong>mapping</strong> says which database column makes which term. <strong>SPARQL</strong> is the query language over those terms. The assistant writes the SPARQL for you.</p>
<p class="hint">Solid arrows carry a query or its result. Dashed amber lines are retry loops. Dashed green lines are the fallback (hub down, farm A only). Thick green lines are the live measurement path. M2, M3 and M5 are the measurement steps.</p><div class="xd-scroll"><svg viewBox="0 0 1000 1190" width="100%" style="min-width:760px;display:block" role="img" aria-label="Swimlane: a question from the engineer to the answer, with the knowledge path and the live measurement path"><defs><marker id="mg" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="#6b7a72"/></marker><marker id="ma" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="#b45a00"/></marker><marker id="mn" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="#0b7a55"/></marker></defs><rect x="0" y="0" width="200" height="1190" fill="#f4f6fa" stroke="#e1e6ee"/><text x="100" y="26" text-anchor="middle" font-size="15" font-weight="700">Engineer</text><text x="100" y="44" text-anchor="middle" font-size="10.5" fill="#555">browser, port 8081</text><rect x="200" y="0" width="200" height="1190" fill="#ffffff" stroke="#e1e6ee"/><text x="300" y="26" text-anchor="middle" font-size="15" font-weight="700">Assistant</text><text x="300" y="44" text-anchor="middle" font-size="10.5" fill="#555">assistant.py, farm_metrics.py</text><rect x="400" y="0" width="200" height="1190" fill="#f4f6fa" stroke="#e1e6ee"/><text x="500" y="26" text-anchor="middle" font-size="15" font-weight="700">Cloud LLM</text><text x="500" y="44" text-anchor="middle" font-size="10.5" fill="#555">Gemini or Groq</text><rect x="600" y="0" width="200" height="1190" fill="#ffffff" stroke="#e1e6ee"/><text x="700" y="26" text-anchor="middle" font-size="15" font-weight="700">Fuseki hub</text><text x="700" y="44" text-anchor="middle" font-size="10.5" fill="#555">port 3030, knowledge</text><rect x="800" y="0" width="200" height="1190" fill="#f4f6fa" stroke="#e1e6ee"/><text x="900" y="26" text-anchor="middle" font-size="15" font-weight="700">Farm nodes</text><text x="900" y="44" text-anchor="middle" font-size="10.5" fill="#555">Ontop + PostgreSQL, A and B</text><rect x="0" y="312" width="1000" height="350" fill="#1f4f8f" opacity="0.07"/><text x="8" y="328" font-size="11" font-weight="700" fill="#1f4f8f">Knowledge question: goes to the hub</text><rect x="0" y="682" width="1000" height="228" fill="#0b7a55" opacity="0.10"/><text x="8" y="698" font-size="11" font-weight="700" fill="#0b7a55">Measurement question: asked live at the farms (new)</text><path d="M188 102 L300 102 L300 150" fill="none" stroke="#6b7a72" stroke-width="2" marker-end="url(#mg)"/><path d="M388 182 L500 182 L500 240" fill="none" stroke="#6b7a72" stroke-width="2" marker-end="url(#mg)"/><text x="392" y="177" font-size="10.5" fill="#6b7a72" font-weight="600">question + farm list</text><path d="M412 272 L300 272 L300 330" fill="none" stroke="#6b7a72" stroke-width="2" marker-end="url(#mg)"/><text x="306" y="300" font-size="10.5" fill="#6b7a72" font-weight="600">hub</text><path d="M588 272 L600 272 L600 730 L388 730" fill="none" stroke="#0b7a55" stroke-width="2" marker-end="url(#mn)"/><text x="606" y="360" font-size="10.5" fill="#0b7a55" font-weight="600">farm_metric parameters</text><path d="M388 362 L500 362 L500 400" fill="none" stroke="#6b7a72" stroke-width="2" marker-end="url(#mg)"/><text x="420" y="357" font-size="10.5" fill="#6b7a72" font-weight="600">prompt</text><path d="M412 440 L300 440 L300 470" fill="none" stroke="#6b7a72" stroke-width="2" marker-end="url(#mg)"/><text x="330" y="436" font-size="10.5" fill="#6b7a72" font-weight="600">SPARQL</text><path d="M388 502 L700 502 L700 540" fill="none" stroke="#6b7a72" stroke-width="2" marker-end="url(#mg)"/><text x="520" y="497" font-size="10.5" fill="#6b7a72" font-weight="600">valid query</text><path d="M212 508 L204 508 L204 422 L412 422" fill="none" stroke="#b45a00" stroke-width="2" stroke-dasharray="6 4" marker-end="url(#ma)"/><text x="40" y="506" font-size="10.5" fill="#b45a00" font-weight="600">unsafe: retry</text><path d="M612 586 L300 586 L300 610" fill="none" stroke="#6b7a72" stroke-width="2" marker-end="url(#mg)"/><text x="520" y="582" font-size="10.5" fill="#6b7a72" font-weight="600">rows</text><path d="M788 572 L812 572" fill="none" stroke="#0b7a55" stroke-width="2" stroke-dasharray="6 4" marker-end="url(#mn)"/><text x="790" y="566" font-size="10.5" fill="#0b7a55" font-weight="600">hub down</text><path d="M900 604 L900 654 L388 654" fill="none" stroke="#0b7a55" stroke-width="2" stroke-dasharray="6 4" marker-end="url(#mn)"/><text x="700" y="670" font-size="10.5" fill="#0b7a55" font-weight="600">rows (fallback)</text><path d="M212 642 L190 642 L190 410 L412 410" fill="none" stroke="#b45a00" stroke-width="2" stroke-dasharray="6 4" marker-end="url(#ma)"/><text x="16" y="662" font-size="10.5" fill="#b45a00" font-weight="600">error or 0 rows: retry</text><path d="M388 754 L900 754 L900 780" fill="none" stroke="#0b7a55" stroke-width="2" marker-end="url(#mn)"/><text x="520" y="750" font-size="10.5" fill="#0b7a55" font-weight="600">one aggregate per farm, in parallel</text><path d="M812 812 L700 812 L700 882 L388 882" fill="none" stroke="#0b7a55" stroke-width="2" marker-end="url(#mn)"/><text x="520" y="904" font-size="10.5" fill="#0b7a55" font-weight="600">rows; farm that did not answer is named</text><path d="M388 630 L400 630 L400 970 L412 970" fill="none" stroke="#6b7a72" stroke-width="2" marker-end="url(#mg)"/><text x="406" y="862" font-size="10.5" fill="#6b7a72" font-weight="600">rows</text><path d="M388 874 L400 874" fill="none" stroke="#6b7a72" stroke-width="2" marker-end="url(#mg)"/><path d="M412 994 L300 994 L300 1030" fill="none" stroke="#6b7a72" stroke-width="2" marker-end="url(#mg)"/><text x="330" y="990" font-size="10.5" fill="#6b7a72" font-weight="600">answer text</text><path d="M212 1062 L100 1062 L100 1100" fill="none" stroke="#6b7a72" stroke-width="2" marker-end="url(#mg)"/><text x="116" y="1057" font-size="10.5" fill="#6b7a72" font-weight="600">reply</text><rect x="12" y="70" width="176" height="64" rx="8" fill="#ffffff" stroke="#555" stroke-width="2"/><text x="24" y="94" font-size="12.5" font-weight="700">Asks a question</text><text x="24" y="110" font-size="10.5" fill="#35514a">scope: Auto, This farm,</text><text x="24" y="124" font-size="10.5" fill="#35514a">or All farms</text><circle cx="12" cy="70" r="12" fill="#17332b"/><text x="12" y="74" text-anchor="middle" font-size="11" font-weight="700" fill="#fff">1</text><rect x="212" y="150" width="176" height="64" rx="8" fill="#eef5ef" stroke="#175c45" stroke-width="2"/><text x="224" y="174" font-size="12.5" font-weight="700">Receive and pick scope</text><text x="224" y="190" font-size="10.5" fill="#35514a">POST /ask, detect_scope()</text><text x="224" y="204" font-size="10.5" fill="#35514a">farm A, or all farms</text><circle cx="212" cy="150" r="12" fill="#17332b"/><text x="212" y="154" text-anchor="middle" font-size="11" font-weight="700" fill="#fff">2</text><rect x="412" y="240" width="176" height="64" rx="8" fill="#fff4dc" stroke="#a86000" stroke-width="2"/><text x="424" y="264" font-size="12.5" font-weight="700">Pick the tool</text><text x="424" y="280" font-size="10.5" fill="#35514a">hub (knowledge) or</text><text x="424" y="294" font-size="10.5" fill="#35514a">farm_metric (measurements)</text><circle cx="412" cy="240" r="12" fill="#17332b"/><text x="412" y="244" text-anchor="middle" font-size="11" font-weight="700" fill="#fff">3</text><rect x="212" y="330" width="176" height="64" rx="8" fill="#eef5ef" stroke="#175c45" stroke-width="2"/><text x="224" y="354" font-size="12.5" font-weight="700">Build the prompt</text><text x="224" y="370" font-size="10.5" fill="#35514a">rules, ontology vocabulary,</text><text x="224" y="384" font-size="10.5" fill="#35514a">example queries</text><circle cx="212" cy="330" r="12" fill="#17332b"/><text x="212" y="334" text-anchor="middle" font-size="11" font-weight="700" fill="#fff">4</text><rect x="412" y="400" width="176" height="64" rx="8" fill="#fff4dc" stroke="#a86000" stroke-width="2"/><text x="424" y="424" font-size="12.5" font-weight="700">Write the SPARQL</text><text x="424" y="440" font-size="10.5" fill="#35514a">one read-only SELECT</text><circle cx="412" cy="400" r="12" fill="#17332b"/><text x="412" y="404" text-anchor="middle" font-size="11" font-weight="700" fill="#fff">5</text><rect x="212" y="470" width="176" height="64" rx="8" fill="#eef5ef" stroke="#175c45" stroke-width="2"/><text x="224" y="494" font-size="12.5" font-weight="700">Safety check</text><text x="224" y="510" font-size="10.5" fill="#35514a">SELECT only, LIMIT 50,</text><text x="224" y="524" font-size="10.5" fill="#35514a">right GRAPH for scope</text><circle cx="212" cy="470" r="12" fill="#17332b"/><text x="212" y="474" text-anchor="middle" font-size="11" font-weight="700" fill="#fff">6</text><rect x="612" y="540" width="176" height="64" rx="8" fill="#e8f0fb" stroke="#1f4f8f" stroke-width="2"/><text x="624" y="564" font-size="12.5" font-weight="700">Run on the hub</text><text x="624" y="580" font-size="10.5" fill="#35514a">graph per farm +</text><text x="624" y="594" font-size="10.5" fill="#35514a">shared knowledge</text><circle cx="612" cy="540" r="12" fill="#17332b"/><text x="612" y="544" text-anchor="middle" font-size="11" font-weight="700" fill="#fff">7</text><rect x="812" y="540" width="176" height="64" rx="8" fill="#f4f6f2" stroke="#0b7a55" stroke-width="2" stroke-dasharray="6 4"/><text x="824" y="564" font-size="12.5" font-weight="700">Fallback: hub down</text><text x="824" y="580" font-size="10.5" fill="#35514a">farm A only: Ontop A</text><text x="824" y="594" font-size="10.5" fill="#35514a">rewrites SPARQL to SQL</text><circle cx="812" cy="540" r="12" fill="#17332b"/><text x="812" y="544" text-anchor="middle" font-size="11" font-weight="700" fill="#fff">8</text><rect x="212" y="610" width="176" height="64" rx="8" fill="#eef5ef" stroke="#175c45" stroke-width="2"/><text x="224" y="634" font-size="12.5" font-weight="700">Result check</text><text x="224" y="650" font-size="10.5" fill="#35514a">error or 0 rows: retry,</text><text x="224" y="664" font-size="10.5" fill="#35514a">three attempts at most</text><circle cx="212" cy="610" r="12" fill="#17332b"/><text x="212" y="614" text-anchor="middle" font-size="11" font-weight="700" fill="#fff">9</text><rect x="212" y="710" width="176" height="64" rx="8" fill="#eef5ef" stroke="#175c45" stroke-width="2"/><text x="224" y="734" font-size="12.5" font-weight="700">Validate parameters</text><text x="224" y="750" font-size="10.5" fill="#35514a">against hub/farms.csv;</text><text x="224" y="764" font-size="10.5" fill="#35514a">This farm = farm A only</text><circle cx="212" cy="710" r="12" fill="#17332b"/><text x="212" y="714" text-anchor="middle" font-size="10" font-weight="700" fill="#fff">M2</text><rect x="812" y="780" width="176" height="64" rx="8" fill="#dff5ec" stroke="#0b7a55" stroke-width="4"/><text x="824" y="804" font-size="12.5" font-weight="700">Ask each farm live</text><text x="824" y="820" font-size="10.5" fill="#35514a">one aggregate per farm</text><text x="824" y="834" font-size="10.5" fill="#35514a">AVG runs as SQL there</text><circle cx="812" cy="780" r="12" fill="#17332b"/><text x="812" y="784" text-anchor="middle" font-size="10" font-weight="700" fill="#fff">M3</text><rect x="212" y="850" width="176" height="64" rx="8" fill="#eef5ef" stroke="#175c45" stroke-width="2"/><text x="224" y="874" font-size="12.5" font-weight="700">Merge and label</text><text x="224" y="890" font-size="10.5" fill="#35514a">names offline farm, units</text><text x="224" y="904" font-size="10.5" fill="#35514a">checked, capacity factor</text><circle cx="212" cy="850" r="12" fill="#17332b"/><text x="212" y="854" text-anchor="middle" font-size="10" font-weight="700" fill="#fff">M5</text><rect x="412" y="950" width="176" height="64" rx="8" fill="#fff4dc" stroke="#a86000" stroke-width="2"/><text x="424" y="974" font-size="12.5" font-weight="700">Write the answer</text><text x="424" y="990" font-size="10.5" fill="#35514a">from the rows only;</text><text x="424" y="1004" font-size="10.5" fill="#35514a">labels synthetic data</text><circle cx="412" cy="950" r="12" fill="#17332b"/><text x="412" y="954" text-anchor="middle" font-size="10" font-weight="700" fill="#fff">10</text><rect x="212" y="1030" width="176" height="64" rx="8" fill="#eef5ef" stroke="#175c45" stroke-width="2"/><text x="224" y="1054" font-size="12.5" font-weight="700">Assemble the reply</text><text x="224" y="1070" font-size="10.5" fill="#35514a">answer, SPARQL, rows,</text><text x="224" y="1084" font-size="10.5" fill="#35514a">scope and why, model</text><circle cx="212" cy="1030" r="12" fill="#17332b"/><text x="212" y="1034" text-anchor="middle" font-size="10" font-weight="700" fill="#fff">11</text><rect x="12" y="1100" width="176" height="64" rx="8" fill="#ffffff" stroke="#555" stroke-width="2"/><text x="24" y="1124" font-size="12.5" font-weight="700">Page shows the result</text><text x="24" y="1140" font-size="10.5" fill="#35514a">answer, table, scope;</text><text x="24" y="1154" font-size="10.5" fill="#35514a">Live farm query badge</text><circle cx="12" cy="1100" r="12" fill="#17332b"/><text x="12" y="1104" text-anchor="middle" font-size="10" font-weight="700" fill="#fff">12</text></svg></div>
<h3>Filled beforehand, not at question time (hub/build_hub.py)</h3>
<div class="xd-lane">
<div class="xd-row">
<div class="xd-box src"><b>Farm A: PostgreSQL</b><small>then Ontop :8080 with vkg/mapping.obda. Table columns become RDF, e.g. status_events.message becomes wind:message.</small></div>
<div class="xd-box src"><b>Farm B: PostgreSQL</b><small>then Ontop :8082 with farm_b/mapping.obda. Different tables, same wind-core terms.</small></div>
<div class="xd-box src"><b>Farm C: Neo4j (fictional)</b><small>neo4j_farm_c.py exports it as RDF to build/hub/farm_c.ttl, with neo4j/fault_names.csv matching fault names.</small></div>
</div>
<div class="xd-arrow">&darr;<small>CONSTRUCT from each Ontop endpoint</small></div>
<div class="xd-box"><b>SHACL gate (vkg/shapes.ttl)</b><small>A farm graph is loaded only if it conforms, otherwise the last good copy stays. Farm C skips this check.</small></div>
<div class="xd-arrow">&darr;<small>loaded into the hub</small></div>
</div>
<p class="xd-note"><strong>Fallback:</strong> if the hub is down, farm A questions go straight to farm A's Ontop, which rewrites the SPARQL into SQL for PostgreSQL. SCADA time series never leave the farm: measurement questions go to the farms live through farm_metric (and the "At the farm" panel shows the raw SPARQL).</p>
<h3>Adding a new source</h3>
<ul><li>CSV or SQL database: add a table and a mapping rule (or a new Ontop node, as farm B does), using the same wind: terms.</li><li>REST API: load it into PostgreSQL, or export RDF as neo4j_farm_c.py does.</li><li>Add a row to hub/farms.csv with its signals and any withheld signals so farm_metric can ask it.</li><li>Then add a shape in vkg/shapes.ttl if needed, an example under queries/, and re-run hub/build_hub.py.</li></ul>
</div></dialog>
<script>(function(){var d=document.getElementById("explain-dialog"),o=document.getElementById("explain-open"),c=document.getElementById("explain-close");if(!d||!o)return;
o.addEventListener("click",function(){if(d.showModal){d.showModal()}else{d.setAttribute("open","")}});
c.addEventListener("click",function(){d.close()});
d.addEventListener("click",function(e){if(e.target===d){d.close()}});})();</script>
"""


def render(sparql="", columns=(), rows=None, reformulation="", relations=(), error=""):
    return render_page(sparql, columns, rows, reformulation, relations, error)


def render_page(sparql="", columns=(), rows=None, reformulation="", relations=(), error=""):
    result_html = ""
    if rows is not None:
        heads = "".join(f"<th scope=\"col\">{escape(column)}</th>" for column in columns)
        body = "".join("<tr>" + "".join(f"<td>{escape(row[column])}</td>" for column in columns) + "</tr>" for row in rows)
        result_html = f'''<section id="legacy-result" aria-labelledby="result-title"><h2 id="result-title">Answer <small>{len(rows)} row(s)</small></h2><div class="scroll"><table><thead><tr>{heads}</tr></thead><tbody>{body}</tbody></table></div>
<h2>How Ontop got this answer</h2><p>{escape(explanation(rows, relations))}</p>
<ol class="flow"><li>Your SPARQL query</li><li>Ontology + R2RML mappings</li><li>Ontop SQL rewrite</li><li>PostgreSQL source tables</li><li>Answer above</li></ol>
<details><summary>Show Ontop's actual SQL rewrite</summary><pre>{escape(reformulation)}</pre></details></section>'''
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Wind knowledge graph</title><style>
body{{margin:0;background:#f4f6f2;color:#17332b;font:16px/1.5 system-ui,sans-serif}}main{{max-width:1100px;margin:auto;padding:2rem 1rem 4rem}}h1{{font-size:1.9rem;margin:.2rem 0}}h2{{font-size:1.25rem;margin:1.5rem 0 .5rem}}small{{font-weight:400;color:#5b7067}}.lead{{color:#52685e;margin:.25rem 0 1.5rem}}form,section{{background:white;border:1px solid #d5dfd7;padding:1.25rem;margin:1rem 0}}label{{display:block;font-weight:700;margin-bottom:.5rem}}#ask-form{{border:0;padding:0;margin:0}}#question{{width:100%;box-sizing:border-box;padding:.7rem;font:inherit;border:1px solid #91a79b;border-radius:3px}}.reply{{font-size:1.05rem;background:#eef5ef;border-left:4px solid #175c45;padding:.75rem 1rem}}.warning{{color:#7a4b00;background:#fff4dc;padding:.5rem .75rem}}textarea{{width:100%;min-height:13rem;box-sizing:border-box;padding:.8rem;font:14px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;border:1px solid #91a79b;border-radius:3px;resize:vertical}}button{{margin-top:.75rem;padding:.65rem 1.3rem;background:#175c45;color:white;border:0;font:inherit;font-weight:700;cursor:pointer}}button:hover,button:focus-visible{{background:#0d4734}}.hint{{color:#5b7067;font-size:.9rem}}.error{{color:#9d2828;font-weight:700}}.scroll{{overflow:auto;max-height:26rem}}table{{border-collapse:collapse;font-size:.85rem;min-width:100%}}th,td{{padding:.5rem .65rem;border-bottom:1px solid #dce5de;text-align:left;vertical-align:top;overflow-wrap:anywhere}}th{{background:#ecf2ed;position:sticky;top:0}}.flow{{display:flex;flex-wrap:wrap;gap:.5rem;padding:0;list-style:none}}.flow li{{background:#e9f0e9;padding:.35rem .55rem}}.flow li+li::before{{content:'→ ';color:#175c45}}details{{margin-top:1rem}}summary{{cursor:pointer;font-weight:700}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#102c25;color:#e6f4e8;padding:1rem;max-height:30rem;overflow:auto;font-size:.78rem}}
#journey[hidden],#live-answer[hidden]{{display:none}}.journey-scroll{{overflow-x:auto;padding:.5rem 0}}.journey-map{{width:1200px;height:120px;position:relative;display:grid;grid-template-columns:repeat(6,160px);gap:40px;padding:10px 20px;box-sizing:border-box}}.wires{{position:absolute;inset:0;width:1200px;height:120px;pointer-events:none}}.wire{{fill:none;stroke:#bdcbc1;stroke-width:4;stroke-linecap:round}}.wire.done{{stroke:#177454}}.wire.flowing{{stroke-dasharray:8 7;animation:travel .7s linear infinite}}@keyframes travel{{to{{stroke-dashoffset:-15}}}}.stage{{position:relative;z-index:1;margin:0;height:100px;padding:.55rem;background:#f4f6f2;color:#3c5d4c;border:2px solid #bdcbc1;text-align:left;font-size:.84rem;line-height:1.25;cursor:pointer}}.stage strong{{display:block;font-size:.95rem;color:#18382c;margin:.3rem 0}}.stage.done{{background:#e8f5eb;border-color:#177454}}.stage.current{{outline:3px solid #eabf54;outline-offset:2px}}.stage.waiting{{border-style:dashed}}.journey-controls{{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}}.journey-controls button{{margin:0;background:#e8f0e9;color:#174a35;border:1px solid #9dbda8}}.journey-controls button:disabled{{opacity:.5;cursor:not-allowed}}.evidence{{border-left:4px solid #177454;background:#f8faf7;padding:1rem;margin-top:1rem}}.evidence h3{{margin:.1rem 0 .5rem;font-size:1.05rem}}.evidence p{{margin:.35rem 0}}.evidence ul{{margin:.4rem 0;padding-left:1.3rem}}.evidence li{{margin:.25rem 0}}.evidence pre{{margin:.5rem 0}}mark{{background:#f8de91;color:#17332b;padding:0 .1rem}}.badge{{display:inline-block;background:#dff0e3;color:#16573b;padding:.1rem .4rem;margin:.15rem .3rem .15rem 0}}.warning{{background:#fff2dc;border-left:4px solid #a86000;padding:.55rem .8rem}}.explain{{margin-top:.7rem}}.story{{display:grid;grid-template-columns:1fr 1fr;gap:.75rem;padding:0;list-style:none;margin:0 0 1rem}}.story li{{background:white;border:1px solid #d5dfd7;border-left:4px solid #175c45;padding:.6rem .8rem;font-size:.92rem}}.story li+li{{border-left-color:#8a5a00}}.tabs{{display:flex;gap:.25rem;border-bottom:2px solid #175c45;flex-wrap:wrap}}.tabs button{{margin:0;background:#e3ebe5;color:#17332b;border:1px solid #c2d1c6;border-bottom:0;padding:.6rem 1rem;text-align:left}}.tabs button small{{display:block;font-weight:400;color:#52685e;font-size:.8rem}}.tabs button[aria-selected=true]{{background:#175c45;color:white}}.tabs button[aria-selected=true] small{{color:#d6eadd}}.panel{{margin-top:0;border-top:0}}.panel[hidden]{{display:none}}.service{{padding:.45rem .75rem;margin:0 0 1rem;background:#eef2ee}}.service.ok{{background:#e5f3e8;border-left:4px solid #177454}}.service.degraded{{background:#fff4dc;border-left:4px solid #a86000}}.service.down{{background:#fbe6e6;border-left:4px solid #9d2828}}.examples button{{margin:.2rem .3rem 0 0;padding:.3rem .6rem;background:#eef5ef;color:#175c45;border:1px solid #9dbda8;font-weight:400;font-size:.88rem}}.ask-form{{border:0;padding:0;margin:0}}.ask-form input{{width:100%;box-sizing:border-box;padding:.7rem;font:inherit;border:1px solid #91a79b;border-radius:3px}}#at-the-farm{{margin-top:1.5rem;border-top:1px solid #d5dfd7;padding-top:1rem}}#at-the-farm form,#at-the-farm section{{margin:.75rem 0}}#farm-graphs table{{margin-bottom:1rem}}.scope{{border:0;padding:0;margin:.6rem 0 0;display:flex;gap:1.2rem;flex-wrap:wrap}}.scope legend{{font-weight:700;padding:0;float:left;margin-right:1rem}}.scope label{{display:inline;font-weight:400;margin:0}}.scope input{{width:auto;margin-right:.3rem}}.examples{{margin:.6rem 0 0}}.chip-groups{{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-top:1rem}}.chip-group{{background:#fff;border:1px solid #d5dfd7;border-radius:6px;padding:.75rem .9rem}}.chip-group h3{{margin:0 0 .5rem;font-size:.98rem}}.chips{{display:flex;flex-direction:column;gap:.4rem}}.chips .example{{margin:0;width:100%;text-align:left;padding:.5rem .7rem .5rem 1.9rem;position:relative;background:#eef5ef;color:#175c45;border:1px solid #9dbda8;border-radius:6px;font:inherit;font-size:.88rem;font-weight:500;line-height:1.35}}.chips .example::before{{content:"\\25B6";position:absolute;left:.65rem;top:.6rem;font-size:.7rem}}.chips .example:hover,.chips .example:focus-visible{{background:#175c45;color:#fff;border-color:#175c45}}.chips .example[disabled]{{opacity:.55;cursor:wait}}#ask-form button#ask-button{{padding:.65rem 2rem}}.spinner{{display:flex;align-items:center;gap:.75rem;padding:.8rem 1rem;background:#eef5ef;border-left:4px solid #175c45}}.spinner i{{width:1.2rem;height:1.2rem;border:3px solid #b9d3c3;border-top-color:#175c45;border-radius:50%;animation:spin .8s linear infinite;flex:none}}@keyframes spin{{to{{transform:rotate(360deg)}}}}.ask-result .reply{{font-size:1.1rem}}.ask-result .live{{background:#e8f0fb;border-left:4px solid #1f4f8f;padding:.55rem .8rem;margin:.6rem 0 0;font-weight:600}}.ask-result .error{{background:#fbe6e6;border-left:4px solid #9d2828;padding:.7rem 1rem}}.ask-result details{{margin-top:.8rem}}@media(max-width:800px){{.chip-groups{{grid-template-columns:1fr}}}}@media(prefers-reduced-motion:reduce){{.spinner i{{animation-duration:3s}}}}
.top{{display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;margin-bottom:1.1rem;padding-right:8.5rem}}.top h1{{font-size:1.7rem;margin:0}}.top .lead{{margin:.2rem 0 0}}.pill{{margin:.2rem 0 0;padding:.3rem .75rem;border-radius:999px;font-size:.8rem;font-weight:600;background:#eef2ee;color:#3c5d4c;border:1px solid #c2d1c6;white-space:nowrap}}.pill:hover,.pill:focus-visible{{background:#e3ebe5}}.pill::before{{content:"";display:inline-block;width:.55rem;height:.55rem;border-radius:50%;background:#9aa8a0;margin-right:.45rem}}.pill[data-state=ok]::before{{background:#177454}}.pill[data-state=degraded]{{background:#fff4dc;border-color:#e2c27a;color:#6b4300}}.pill[data-state=degraded]::before{{background:#d98a00}}.pill[data-state=down]{{background:#fbe6e6;border-color:#e0a6a6;color:#7d1f1f}}.pill[data-state=down]::before{{background:#c23a3a}}
#ask-form{{background:#fff;border:1px solid #d5dfd7;border-radius:10px;padding:1.1rem 1.2rem 1.2rem;box-shadow:0 1px 3px rgba(23,51,43,.06)}}#ask-form>label{{font-size:1.05rem}}#question{{font-size:1.05rem;padding:.85rem .9rem;border-radius:8px}}#question:focus{{outline:3px solid #b9d3c3;border-color:#175c45}}.scope{{margin:.8rem 0 0;align-items:center}}.scope legend{{font-size:.9rem;color:#52685e}}.scope label{{padding:.25rem .7rem;border:1px solid #c2d1c6;border-radius:999px;background:#f4f7f4;cursor:pointer}}.scope label:has(input:checked){{background:#175c45;border-color:#175c45;color:#fff}}.scope label:has(input:checked) small{{color:#d6eadd}}.scope input{{position:absolute;opacity:0;width:1px;height:1px}}.scope label:has(input:focus-visible){{outline:3px solid #eabf54}}#ask-form button#ask-button{{border-radius:8px;margin-top:.9rem}}#scope-hint{{margin:.5rem 0 0}}.chip-groups{{margin-top:1.1rem}}.chip-group{{background:#fafbf9}}.chip-group h3{{font-size:.95rem}}.chip-group h3 small{{display:block}}.chip-group.live{{grid-column:1/-1;border-color:#9fc5b4;background:#f1f8f4}}.chip-group.live .chips{{display:grid;grid-template-columns:1fr 1fr;gap:.4rem}}@media(max-width:800px){{.chip-group.live .chips{{grid-template-columns:1fr}}}}details>summary{{padding:.2rem 0}}
@media(max-width:700px){{.story{{grid-template-columns:1fr}}.top{{padding-right:0;flex-wrap:wrap;margin-top:2.5rem}}}}@media(prefers-reduced-motion:reduce){{.wire.flowing{{animation:none}}}}
</style><style>{EXPLAIN_CSS}</style><script defer src="/workbench.js"></script></head><body>{EXPLAIN_HTML}<main>
<header class="top"><div><h1>Wind-energy knowledge graph</h1>
<p class="lead">Ask in plain English. Every fact in the answer comes from the knowledge graph or from live farm queries.</p></div>
<button type="button" id="status-pill" class="pill" data-state="checking" aria-label="Service status: open the Explanation for details">Checking services…</button></header>
<form id="ask-form" class="ask-form"><label for="question">Ask a question <small>or click an example below: it fills the box and runs</small></label><input id="question" name="question" maxlength="1000" autocomplete="off" placeholder="What usually causes high gear bearing temperature on a gearbox, and what fixed it last time?">
<fieldset class="scope" aria-label="Scope"><legend>Scope</legend>
<label><input type="radio" name="scope" value="auto" checked> Auto <small>from the wording</small></label>
<label><input type="radio" name="scope" value="farm"> This farm <small>farm A, Kelmarsh only</small></label>
<label><input type="radio" name="scope" value="cross"> All farms <small>every farm graph</small></label></fieldset>
<button type="submit" id="ask-button">Ask</button>
<p class="hint" id="scope-hint">Auto reads the wording: no farm named means Kelmarsh (farm A); "both farms", "each farm" or another farm's name means all farms.</p>
<div class="chip-groups"><div class="chip-group"><h3>Fault diagnosis <small>use case 1 &middot; farm A</small></h3><div class="chips">
<button type="button" class="example">What usually causes high gear bearing temperature on a gearbox, and what fixed it last time?</button>
<button type="button" class="example">Kelmarsh 2 raised a high gear bearing temperature alarm. What usually causes it, and what fixed it before?</button>
<button type="button" class="example">Which Kelmarsh turbines raised gear bearing temperature alarms, and how often?</button></div></div>
<div class="chip-group"><h3>Cross-farm learning <small>use case 2 &middot; all farms</small></h3><div class="chips">
<button type="button" class="example">For gearbox faults, how did each farm fix them and how often?</button>
<button type="button" class="example">Which alarm types point to which faults, with what confidence, and how often did each fire on each farm?</button>
<button type="button" class="example">What does each farm publish, what does it withhold, and how many technicians can we see?</button>
<button type="button" class="example">Across all three farms, how were bearing faults fixed?</button>

</div></div>
<div class="chip-group live"><h3>Live measurements <small>asked at the farms right now &middot; blue badge on the answer</small></h3><div class="chips">
<button type="button" class="example">What is the average power per farm, and which farm is highest?</button>
<button type="button" class="example">Compare average wind speed by month between the farms.</button>
<button type="button" class="example">What was the average power of each Kelmarsh turbine?</button>
<button type="button" class="example">What is the average gear oil temperature per farm?</button></div></div></div>
</form>
<div id="ask-result" class="ask-result" aria-live="polite"></div>
<details id="farm-graphs-box"><summary>Farm graphs in the hub</summary><div id="farm-graphs"></div></details>
<details id="at-the-farm"{" open" if sparql or error else ""}><summary>At the farm: SPARQL on farm A's own Ontop endpoint, with its query journey</summary>
<p class="hint">This is farm A's node itself: Ontop rewrites SPARQL into SQL over farm A's PostgreSQL, live. Use it for the SCADA time series, which never leave the farm, or to see how an answer is built.</p>
<form method="post" action="/"><label for="sparql">Your query</label><textarea id="sparql" name="sparql" required spellcheck="false" placeholder="PREFIX wind: &lt;https://w3id.org/wind-core/ontology#&gt;&#10;SELECT ?turbine ?name WHERE {{ ?turbine a wind:WindTurbine ; wind:name ?name . }} LIMIT 10">{escape(sparql)}</textarea><p class="hint">SELECT queries only. Add LIMIT 100 or less; the workbench adds LIMIT 100 if omitted. For exact source rows, include <code>?sourceFile</code> and <code>?sourceRow</code> in your SELECT.</p><button type="submit">Run query</button></form>{f'<p class="error" role="alert">{escape(error)}</p>' if error else ''}<section id="journey" hidden aria-labelledby="journey-title"><h2 id="journey-title">Live query journey</h2><p id="journey-status" role="status" aria-live="polite">Waiting for a query.</p><div class="journey-scroll"><div class="journey-map"><svg class="wires" viewBox="0 0 1200 120" aria-hidden="true"><path class="wire" d="M180 60 H220"/><path class="wire" d="M380 60 H420"/><path class="wire" d="M580 60 H620"/><path class="wire" d="M780 60 H820"/><path class="wire" d="M980 60 H1020"/></svg><button type="button" class="stage waiting" data-stage="0">01<strong>Your query</strong><span>Waiting</span></button><button type="button" class="stage waiting" data-stage="1">02<strong>Graph terms</strong><span>Waiting</span></button><button type="button" class="stage waiting" data-stage="2">03<strong>R2RML mapping</strong><span>Waiting</span></button><button type="button" class="stage waiting" data-stage="3">04<strong>Ontop SQL</strong><span>Waiting</span></button><button type="button" class="stage waiting" data-stage="4">05<strong>PostgreSQL</strong><span>Waiting</span></button><button type="button" class="stage waiting" data-stage="5">06<strong>Answer</strong><span>Waiting</span></button></div></div><div class="journey-controls"><button id="replay" type="button" disabled>Replay journey</button><button id="next-step" type="button" disabled>Next step</button><span class="hint">Select a stage to inspect its evidence.</span></div><div id="evidence" class="evidence" aria-live="polite"></div></section><section id="live-answer" hidden aria-labelledby="live-answer-title"><h2 id="live-answer-title">Answer</h2><div id="live-table" class="scroll"></div><p id="live-explanation" class="explain"></p></section>{result_html}</details>
<details><summary>Write SPARQL against the hub</summary>
<form id="hub-form"><label for="hub-sparql">Hub query</label><textarea id="hub-sparql" name="sparql" spellcheck="false" placeholder="PREFIX wind: &lt;https://w3id.org/wind-core/ontology#&gt;&#10;SELECT ?farm (COUNT(?e) AS ?events) WHERE {{ ?f wind:farmId ?farm ; wind:graph ?g . GRAPH ?g {{ ?e a wind:OperationalEvent }} }} GROUP BY ?farm"></textarea><p class="hint">SELECT only, LIMIT 100 or less. The hub endpoint is also open directly at http://127.0.0.1:3030/hub/sparql; saved examples are in queries/hub_*.rq.</p><button type="submit">Run on the hub</button></form>
<div id="hub-result" aria-live="polite"></div></details>
</main></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, body, content_type="text/html; charset=utf-8"):
        content = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path == "/workbench.js":
            self.respond(200, Path("/app/workbench.js").read_text(encoding="utf-8") if Path("/app/workbench.js").exists() else Path("workbench.js").read_text(encoding="utf-8"), "text/javascript; charset=utf-8")
            return
        if self.path == "/status":
            self.respond(200, json.dumps(status()), "application/json; charset=utf-8")
            return
        if self.path != "/":
            self.send_error(404)
            return
        self.respond(200, render())

    def do_POST(self):
        if self.path not in ("/", "/inspect", "/reformulate", "/answer", "/ask", "/hub-answer"):
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > 16000:
            self.respond(413, render(error="Query is too long or missing."))
            return
        params = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        sparql = params.get("sparql", [""])[0].strip()
        if self.path == "/ask":
            try:
                self.respond(200, json.dumps(assistant.ask(params.get("question", [""])[0], params.get("scope", ["auto"])[0])), "application/json; charset=utf-8")
            except ValueError as error:
                self.respond(400, json.dumps({"error": str(error)}), "application/json; charset=utf-8")
            except Exception as error:
                self.respond(503, json.dumps({"error": str(error)}), "application/json; charset=utf-8")
            return
        try:
            if self.path != "/":
                result = {"/inspect": inspect, "/reformulate": reformulate, "/answer": answers, "/hub-answer": hub_answers}[self.path](sparql)
                self.respond(200, json.dumps(result), "application/json; charset=utf-8")
                return
            columns, rows, rewrite, relations = execute(sparql)
            self.respond(200, render(sparql, columns, rows, rewrite, relations))
        except ValueError as error:
            self.respond(400, json.dumps({"error": str(error)}) if self.path != "/" else render(sparql, error=str(error)), "application/json; charset=utf-8" if self.path != "/" else "text/html; charset=utf-8")
        except Exception as error:
            self.respond(503, json.dumps({"error": f"Service unavailable: {error}"}) if self.path != "/" else render(sparql, error=f"Ontop is unavailable: {error}"), "application/json; charset=utf-8" if self.path != "/" else "text/html; charset=utf-8")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
