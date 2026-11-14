"""One site, two tabs, one assistant, all on the hub.

Tab 1, farm A (use case 1): fault diagnosis on farm A's named graph in the hub. A collapsed
"at the farm" panel keeps farm A's own Ontop endpoint with its query journey (SPARQL ->
mapping -> SQL -> PostgreSQL), and the assistant falls back to it if the hub is down.
Tab 2, all farms (use case 2): the same kind of question across every farm graph in the hub.
Each tab checks its own services, so one being down does not take the other tab with it."""

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
    """Per-tab health: the farm A tab needs the hub with farm A's graph, or farm A's own endpoint;
    the all-farms tab needs the hub. Short timeouts so a dead service cannot hang the page."""
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
    report["tabs"] = {
        "farm": {"ok": report["hub"]["ok"] and farm_graph or report["farmA"]["ok"],
                 "via": "hub" if report["hub"]["ok"] and farm_graph else ("farm" if report["farmA"]["ok"] else "none")},
        "cross": {"ok": report["hub"]["ok"] and len(report["hub"]["graphs"]) > 0, "farms": len(report["hub"]["graphs"])},
    }
    return report


def hub_answers(sparql):
    """Raw SPARQL on the hub for the all-farms tab (read-only SELECT, LIMIT 100 or less)."""
    sparql = prepared(sparql)
    if re.search(r"(INSERT|DELETE|LOAD|CLEAR|DROP|CREATE|COPY|MOVE|ADD|SERVICE)", assistant.code_only(sparql), re.I):
        raise ValueError("Only read-only SELECT queries, without SERVICE, are allowed here.")
    try:
        columns, rows = assistant.run_sparql(sparql, HUB, timeout=60)
    except assistant.Unavailable as error:
        raise RuntimeError(f"The hub is unavailable: {error}") from error
    return {"columns": columns, "rows": rows}


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
#journey[hidden],#live-answer[hidden]{{display:none}}.journey-scroll{{overflow-x:auto;padding:.5rem 0}}.journey-map{{width:1200px;height:120px;position:relative;display:grid;grid-template-columns:repeat(6,160px);gap:40px;padding:10px 20px;box-sizing:border-box}}.wires{{position:absolute;inset:0;width:1200px;height:120px;pointer-events:none}}.wire{{fill:none;stroke:#bdcbc1;stroke-width:4;stroke-linecap:round}}.wire.done{{stroke:#177454}}.wire.flowing{{stroke-dasharray:8 7;animation:travel .7s linear infinite}}@keyframes travel{{to{{stroke-dashoffset:-15}}}}.stage{{position:relative;z-index:1;margin:0;height:100px;padding:.55rem;background:#f4f6f2;color:#3c5d4c;border:2px solid #bdcbc1;text-align:left;font-size:.84rem;line-height:1.25;cursor:pointer}}.stage strong{{display:block;font-size:.95rem;color:#18382c;margin:.3rem 0}}.stage.done{{background:#e8f5eb;border-color:#177454}}.stage.current{{outline:3px solid #eabf54;outline-offset:2px}}.stage.waiting{{border-style:dashed}}.journey-controls{{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}}.journey-controls button{{margin:0;background:#e8f0e9;color:#174a35;border:1px solid #9dbda8}}.journey-controls button:disabled{{opacity:.5;cursor:not-allowed}}.evidence{{border-left:4px solid #177454;background:#f8faf7;padding:1rem;margin-top:1rem}}.evidence h3{{margin:.1rem 0 .5rem;font-size:1.05rem}}.evidence p{{margin:.35rem 0}}.evidence ul{{margin:.4rem 0;padding-left:1.3rem}}.evidence li{{margin:.25rem 0}}.evidence pre{{margin:.5rem 0}}mark{{background:#f8de91;color:#17332b;padding:0 .1rem}}.badge{{display:inline-block;background:#dff0e3;color:#16573b;padding:.1rem .4rem;margin:.15rem .3rem .15rem 0}}.warning{{background:#fff2dc;border-left:4px solid #a86000;padding:.55rem .8rem}}.explain{{margin-top:.7rem}}.story{{display:grid;grid-template-columns:1fr 1fr;gap:.75rem;padding:0;list-style:none;margin:0 0 1rem}}.story li{{background:white;border:1px solid #d5dfd7;border-left:4px solid #175c45;padding:.6rem .8rem;font-size:.92rem}}.story li+li{{border-left-color:#8a5a00}}.tabs{{display:flex;gap:.25rem;border-bottom:2px solid #175c45;flex-wrap:wrap}}.tabs button{{margin:0;background:#e3ebe5;color:#17332b;border:1px solid #c2d1c6;border-bottom:0;padding:.6rem 1rem;text-align:left}}.tabs button small{{display:block;font-weight:400;color:#52685e;font-size:.8rem}}.tabs button[aria-selected=true]{{background:#175c45;color:white}}.tabs button[aria-selected=true] small{{color:#d6eadd}}.panel{{margin-top:0;border-top:0}}.panel[hidden]{{display:none}}.service{{padding:.45rem .75rem;margin:0 0 1rem;background:#eef2ee}}.service.ok{{background:#e5f3e8;border-left:4px solid #177454}}.service.degraded{{background:#fff4dc;border-left:4px solid #a86000}}.service.down{{background:#fbe6e6;border-left:4px solid #9d2828}}.examples button{{margin:.2rem .3rem 0 0;padding:.3rem .6rem;background:#eef5ef;color:#175c45;border:1px solid #9dbda8;font-weight:400;font-size:.88rem}}.ask-form{{border:0;padding:0;margin:0}}.ask-form input{{width:100%;box-sizing:border-box;padding:.7rem;font:inherit;border:1px solid #91a79b;border-radius:3px}}#at-the-farm{{margin-top:1.5rem;border-top:1px solid #d5dfd7;padding-top:1rem}}#at-the-farm form,#at-the-farm section{{margin:.75rem 0}}#farm-graphs table{{margin-bottom:1rem}}@media(max-width:700px){{.story{{grid-template-columns:1fr}}}}@media(prefers-reduced-motion:reduce){{.wire.flowing{{animation:none}}}}
</style><script defer src="/workbench.js"></script></head><body><main>
<h1>Wind-energy knowledge graph</h1>
<p class="lead">One assistant, one hub, two use cases. Ask in plain English: the language model writes a read-only SPARQL query for the hub, and every fact in the reply comes from the rows the graph returns.</p>
<ol class="story" aria-label="How the data is arranged">
<li><strong>Data stays at the farm.</strong> Each farm keeps its own database and publishes it through its own Ontop endpoint, a virtual graph: nothing is copied at the farm.</li>
<li><strong>The hub is shared.</strong> It holds one named graph per farm (harvested from that farm's Ontop endpoint and checked against the SHACL shapes before loading), plus curated alarm-to-fault knowledge with confidence scores as RDF 1.2 annotations. SCADA time series stay at the farm.</li>
</ol>
<div class="tabs" role="tablist" aria-label="Use cases">
<button type="button" role="tab" id="tab-farm" aria-controls="panel-farm" aria-selected="true" data-tab="farm">Farm A (Kelmarsh): fault diagnosis<small>use case 1 · farm A's graph only</small></button>
<button type="button" role="tab" id="tab-cross" aria-controls="panel-cross" aria-selected="false" data-tab="cross" tabindex="-1">All farms: cross-farm learning<small>use case 2 · every farm graph</small></button>
</div>
<section role="tabpanel" id="panel-farm" aria-labelledby="tab-farm" class="panel">
<p class="service" data-status="farm" role="status">Checking the hub and farm A…</p>
<form class="ask-form" data-scope="farm"><label for="question-farm">Ask about farm A</label><input id="question-farm" name="question" maxlength="1000" autocomplete="off" placeholder="What usually causes high gear bearing temperature on a gearbox, and what fixed it last time?"><button type="submit">Ask</button>
<p class="hint">Scope: the hub, inside <code>GRAPH &lt;{escape(assistant.FARM_A_GRAPH)}&gt;</code> only, plus the shared curated knowledge. If the hub is down, the question is answered at the farm, on farm A's own Ontop endpoint.</p>
<p class="examples">Try: <button type="button" class="example">What usually causes high gear bearing temperature on a gearbox, and what fixed it last time?</button> <button type="button" class="example">Which Kelmarsh turbines raised gear bearing temperature alarms, and how often?</button></p></form>
<div class="ask-result" aria-live="polite"></div>
<details id="at-the-farm"{" open" if sparql or error else ""}><summary>At the farm: SPARQL on farm A's own Ontop endpoint, with its query journey</summary>
<p class="hint">This is farm A's node itself: Ontop rewrites SPARQL into SQL over farm A's PostgreSQL, live. Use it for the SCADA time series, which never leave the farm, or to see how an answer is built.</p>
<form method="post" action="/"><label for="sparql">Your query</label><textarea id="sparql" name="sparql" required spellcheck="false" placeholder="PREFIX wind: &lt;https://w3id.org/wind-core/ontology#&gt;&#10;SELECT ?turbine ?name WHERE {{ ?turbine a wind:WindTurbine ; wind:name ?name . }} LIMIT 10">{escape(sparql)}</textarea><p class="hint">SELECT queries only. Add LIMIT 100 or less; the workbench adds LIMIT 100 if omitted. For exact source rows, include <code>?sourceFile</code> and <code>?sourceRow</code> in your SELECT.</p><button type="submit">Run query</button></form>{f'<p class="error" role="alert">{escape(error)}</p>' if error else ''}<section id="journey" hidden aria-labelledby="journey-title"><h2 id="journey-title">Live query journey</h2><p id="journey-status" role="status" aria-live="polite">Waiting for a query.</p><div class="journey-scroll"><div class="journey-map"><svg class="wires" viewBox="0 0 1200 120" aria-hidden="true"><path class="wire" d="M180 60 H220"/><path class="wire" d="M380 60 H420"/><path class="wire" d="M580 60 H620"/><path class="wire" d="M780 60 H820"/><path class="wire" d="M980 60 H1020"/></svg><button type="button" class="stage waiting" data-stage="0">01<strong>Your query</strong><span>Waiting</span></button><button type="button" class="stage waiting" data-stage="1">02<strong>Graph terms</strong><span>Waiting</span></button><button type="button" class="stage waiting" data-stage="2">03<strong>R2RML mapping</strong><span>Waiting</span></button><button type="button" class="stage waiting" data-stage="3">04<strong>Ontop SQL</strong><span>Waiting</span></button><button type="button" class="stage waiting" data-stage="4">05<strong>PostgreSQL</strong><span>Waiting</span></button><button type="button" class="stage waiting" data-stage="5">06<strong>Answer</strong><span>Waiting</span></button></div></div><div class="journey-controls"><button id="replay" type="button" disabled>Replay journey</button><button id="next-step" type="button" disabled>Next step</button><span class="hint">Select a stage to inspect its evidence.</span></div><div id="evidence" class="evidence" aria-live="polite"></div></section><section id="live-answer" hidden aria-labelledby="live-answer-title"><h2 id="live-answer-title">Answer</h2><div id="live-table" class="scroll"></div><p id="live-explanation" class="explain"></p></section>{result_html}</details>
</section>
<section role="tabpanel" id="panel-cross" aria-labelledby="tab-cross" class="panel" hidden>
<p class="service" data-status="cross" role="status">Checking the hub…</p>
<div id="farm-graphs"></div>
<form class="ask-form" data-scope="cross"><label for="question-cross">Ask across all farms</label><input id="question-cross" name="question" maxlength="1000" autocomplete="off" placeholder="For gearbox faults, how did each farm fix them and how often?"><button type="submit">Ask</button>
<p class="hint">Scope: the hub, every farm graph (<code>GRAPH ?g</code>, bound from the farm registry), plus the shared curated knowledge. Differences in repairs come from synthetic work orders and are not findings.</p>
<p class="examples">Try: <button type="button" class="example">For gearbox faults, how did each farm fix them and how often?</button> <button type="button" class="example">Which alarm types point to which faults, with what confidence, and how often did each fire on each farm?</button> <button type="button" class="example">What does each farm publish, what does it withhold, and how many technicians can we see?</button></p></form>
<div class="ask-result" aria-live="polite"></div>
<details><summary>Write SPARQL against the hub</summary>
<form id="hub-form"><label for="hub-sparql">Hub query</label><textarea id="hub-sparql" name="sparql" spellcheck="false" placeholder="PREFIX wind: &lt;https://w3id.org/wind-core/ontology#&gt;&#10;SELECT ?farm (COUNT(?e) AS ?events) WHERE {{ ?f wind:farmId ?farm ; wind:graph ?g . GRAPH ?g {{ ?e a wind:OperationalEvent }} }} GROUP BY ?farm"></textarea><p class="hint">SELECT only, LIMIT 100 or less. The hub endpoint is also open directly at http://127.0.0.1:3030/hub/sparql; saved examples are in queries/hub_*.rq.</p><button type="submit">Run on the hub</button></form>
<div id="hub-result" aria-live="polite"></div></details>
</section>
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
                self.respond(200, json.dumps(assistant.ask(params.get("question", [""])[0], params.get("scope", ["farm"])[0])), "application/json; charset=utf-8")
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
