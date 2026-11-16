const queryBox = document.querySelector("#sparql");
const form = queryBox ? queryBox.form : null;
const journey = document.querySelector("#journey");
const statusLine = document.querySelector("#journey-status");
const evidence = document.querySelector("#evidence");
const stages = [...document.querySelectorAll(".stage")];
const wires = [...document.querySelectorAll(".wire")];
const answerPanel = document.querySelector("#live-answer");
const answerTable = document.querySelector("#live-table");
const answerText = document.querySelector("#live-explanation");
const replayButton = document.querySelector("#replay");
const nextButton = document.querySelector("#next-step");
const runButton = form?.querySelector('button[type="submit"]');
let run = null;

function add(tag, content, parent = evidence) {
  const node = document.createElement(tag);
  node.textContent = content;
  parent.append(node);
  return node;
}

function sourceRefs() {
  if (!run.answer) return [];
  const refs = new Set();
  for (const row of run.answer.rows) {
    for (const [file, number] of [["sourceFile", "sourceRow"], ["eventFile", "eventRow"]]) {
      if (row[file] && row[number]) refs.add(`${row[file]}, row ${row[number]}`);
    }
  }
  return [...refs];
}

function synthetic() {
  return run.reform?.relations.includes("synthetic_maintenance") ||
    run.answer?.rows.some(row => row.recordKind === "synthetic_demo");
}

function matchingRules() {
  if (!run.reform) return run.inspect.mappings;
  return run.inspect.mappings.filter(rule => {
    const source = rule.source.match(/\b(?:FROM|JOIN)\s+"?(\w+)/gi) || [];
    return source.some(fragment => run.reform.relations.includes(fragment.split(/\s+"?/).at(-1)));
  });
}

function showStage(index) {
  const visible = run.replaying ? run.shown : run.complete;
  if (index > visible) return;
  run.selected = index;
  stages.forEach((stage, i) => stage.classList.toggle("current", i === index));
  evidence.replaceChildren();
  const titles = ["Your submitted query", "Graph terms in your query", "Matching R2RML rules", "Ontop's reformulation", "PostgreSQL relations in Ontop's SQL", "Live answer and source rows"];
  add("h3", titles[index]);

  if (index === 0) {
    add("p", "This is the query you typed. Highlighted terms were found in the V1 ontology or runtime mapping.");
    const pre = add("pre", "");
    const tokens = run.inspect?.terms.map(term => term.token).sort((a, b) => b.length - a.length) || [];
    const pattern = tokens.length && new RegExp(tokens.map(token => token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "g");
    if (!pattern) pre.textContent = run.query;
    else {
      let start = 0;
      for (const match of run.query.matchAll(pattern)) {
        pre.append(document.createTextNode(run.query.slice(start, match.index)));
        add("mark", match[0], pre);
        start = match.index + match[0].length;
      }
      pre.append(document.createTextNode(run.query.slice(start)));
    }
  } else if (index === 1) {
    const terms = run.inspect.terms;
    add("p", `${terms.length} graph term(s) matched definitions or mapping targets. These are terms in your query, not database columns.`);
    const list = add("ul", "");
    for (const term of terms) {
      const item = add("li", `${term.token} → ${term.iri}`, list);
      add("small", ` ${term.definition}`, item);
    }
    if (!terms.length) add("p", "No directly indexed V1 term was found. Ontop's SQL remains the authoritative source trace.");
  } else if (index === 2) {
    add("p", run.reform ? "These rules match query terms and refer to relations in Ontop's SQL. Several rules can share a relation, so this is a matching-rule list, not an Ontop rule-execution log." : "These rules match query terms. Ontop's SQL will narrow the list to relevant database relations.");
    for (const rule of matchingRules()) {
      const detail = add("details", "");
      add("summary", `${rule.id} · ${rule.terms.join(", ")}`, detail);
      add("p", `Mapping SQL source: ${rule.source || "See rule snippet"}`, detail);
      add("pre", rule.snippet, detail);
    }
    if (!matchingRules().length) add("p", run.reform ? "No direct R2RML term match for the confirmed SQL relations. Inspect Ontop's generated SQL in the next stage." : "No direct R2RML term match. Ontop will still try to reformulate your query.");
  } else if (index === 3) {
    add("p", "Returned by this Ontop instance for the submitted query. The output includes Ontop's plan and its generated SQL.");
    add("pre", run.reform.reformulation);
  } else if (index === 4) {
    const relations = run.reform.relations;
    add("p", "Only relations named in Ontop's generated SQL are listed here.");
    if (relations.length) for (const relation of relations) add("span", relation, evidence).className = "badge";
    else add("p", "No PostgreSQL relation was found in the reformulation. Inspect Ontop's raw output in the preceding stage.");
    for (const [name, definition] of Object.entries(run.reform.views)) {
      const detail = add("details", "");
      add("summary", `${name} is a view; show its V1 schema definition`, detail);
      add("pre", definition, detail);
    }
    if (synthetic()) add("p", "synthetic_demo: maintenance cases are fabricated examples, not operator-confirmed repairs.").className = "warning";
  } else {
    add("p", `Ontop returned ${run.answer.rows.length} row(s) from PostgreSQL for this query.`);
    const refs = sourceRefs();
    if (refs.length) {
      add("p", "Source references returned by your query:");
      const list = add("ul", "");
      for (const ref of refs.slice(0, 8)) add("li", ref, list);
      if (refs.length > 8) add("li", `${refs.length - 8} more references in the answer table.`, list);
    } else add("p", "This query did not select source file and row fields, so no exact CSV row is claimed.");
    if (synthetic()) add("p", "synthetic_demo: any maintenance case in this path is fabricated for the demo.").className = "warning";
  }
}

function paint() {
  const visible = run.replaying ? run.shown : run.complete;
  stages.forEach((stage, i) => {
    stage.classList.toggle("done", i <= visible);
    stage.classList.toggle("waiting", i > visible);
    stage.querySelector("span").textContent = i <= visible ? "Evidence ready" : "Waiting";
  });
  wires.forEach((wire, i) => {
    wire.classList.toggle("done", i < visible);
    wire.classList.toggle("flowing", i === visible - 1 && (run.complete < 5 || run.replaying));
  });
  nextButton.disabled = !run.replaying || run.shown >= run.complete;
  if (run.selected > visible) run.selected = visible;
  showStage(run.selected);
}

function advance(index, message) {
  run.complete = index;
  run.selected = index;
  statusLine.textContent = message;
  paint();
}

async function phase(path, query) {
  const response = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/x-www-form-urlencoded"},
    body: new URLSearchParams({sparql: query}),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}

function renderAnswer() {
  const {columns, rows} = run.answer;
  answerPanel.hidden = false;
  answerPanel.querySelectorAll(".warning").forEach(node => node.remove());
  document.querySelector("#live-answer-title").textContent = `Answer · ${rows.length} row(s)`;
  answerTable.replaceChildren();
  if (columns.length) {
    const table = add("table", "", answerTable);
    const head = add("thead", "", table);
    const heading = add("tr", "", head);
    for (const column of columns) add("th", column, heading).scope = "col";
    const body = add("tbody", "", table);
    for (const row of rows) {
      const tr = add("tr", "", body);
      for (const column of columns) add("td", row[column] || "", tr);
    }
  }
  answerText.textContent = `Live Ontop answer. ${run.reform.relations.length ? `PostgreSQL relations in its SQL: ${run.reform.relations.join(", ")}.` : "Inspect the Ontop reformulation for its source."} ${sourceRefs().length ? "Source file and row references are in the answer and journey above." : "Exact source rows were not selected by this query."}`;
  if (synthetic()) add("p", "synthetic_demo: linked maintenance data is fabricated for this demo.", answerPanel).className = "warning";
}

if (form) form.addEventListener("submit", async event => {
  event.preventDefault();
  const query = queryBox.value.trim();
  if (!query) return;
  run = {query, inspect: null, reform: null, answer: null, complete: 0, shown: 0, selected: 0, replaying: false};
  journey.hidden = false;
  answerPanel.hidden = true;
  statusLine.className = "";
  document.querySelector("#legacy-result")?.setAttribute("hidden", "");
  replayButton.disabled = true;
  nextButton.disabled = true;
  runButton.disabled = true;
  advance(0, "Query submitted. Checking graph terms against the runtime mapping…");
  try {
    run.inspect = await phase("/inspect", query);
    advance(1, "Graph terms identified from your query.");
    advance(2, "Matching R2RML rules found. Asking Ontop for its SQL…");
    run.reform = await phase("/reformulate", query);
    advance(3, "Ontop returned its reformulation and generated SQL.");
    advance(4, "PostgreSQL relations identified from Ontop's SQL. Running the query…");
    run.answer = await phase("/answer", query);
    advance(5, `Answer ready: ${run.answer.rows.length} row(s) returned.`);
    renderAnswer();
    replayButton.disabled = false;
  } catch (error) {
    statusLine.textContent = `Stopped: ${error.message}`;
    statusLine.className = "error";
    add("p", error.message).className = "error";
  } finally {
    runButton.disabled = false;
  }
});

stages.forEach((stage, index) => stage.addEventListener("click", () => run && showStage(index)));
replayButton?.addEventListener("click", () => {
  if (!run?.answer) return;
  run.replaying = true;
  run.shown = 0;
  run.selected = 0;
  statusLine.className = "";
  statusLine.textContent = "Replay of the completed live query. Press Next step to inspect each piece of evidence.";
  paint();
});
nextButton?.addEventListener("click", () => {
  if (!run?.replaying || run.shown >= run.complete) return;
  run.shown++;
  run.selected = run.shown;
  paint();
});

function table(columns, rows, parent) {
  const wrap = add("div", "", parent);
  wrap.className = "scroll";
  const tableNode = add("table", "", wrap);
  const heading = add("tr", "", add("thead", "", tableNode));
  for (const column of columns) add("th", column, heading).scope = "col";
  const body = add("tbody", "", tableNode);
  for (const row of rows) {
    const tr = add("tr", "", body);
    for (const column of columns) add("td", row[column] || "", tr);
  }
  return wrap;
}

// ---------- Service status, one line per kind of question so one service being down only affects its own ----------
async function checkServices() {
  const farmLine = document.querySelector('[data-status="farm"]');
  const crossLine = document.querySelector('[data-status="cross"]');
  let data;
  try {
    const response = await fetch("/status");
    data = await response.json();
  } catch (error) {
    for (const line of [farmLine, crossLine]) { line.textContent = "The site could not check its services."; line.className = "service down"; }
    setPill("down", "Services unreachable");
    return;
  }
  const farm = data.scopes.farm;
  if (farm.via === "hub") {
    farmLine.textContent = `This farm (farm A): hub online with farm A's graph${data.farmA.ok ? "; farm A's own Ontop endpoint is online too" : "; farm A's own endpoint is offline, so the at-the-farm panel will not answer"}.`;
    farmLine.className = data.farmA.ok ? "service ok" : "service degraded";
  } else if (farm.via === "farm") {
    farmLine.textContent = "This farm (farm A): the hub is unavailable, so questions will be answered at the farm, on farm A's own Ontop endpoint.";
    farmLine.className = "service degraded";
  } else {
    farmLine.textContent = "This farm (farm A): neither the hub nor farm A's own endpoint is reachable, so these questions cannot be answered right now.";
    farmLine.className = "service down";
  }
  const graphs = data.hub.graphs || [];
  const box = document.querySelector("#farm-graphs");
  box.replaceChildren();
  if (data.scopes.cross.ok) {
    crossLine.textContent = `All farms: hub online with ${graphs.length} farm graph(s).`;
    crossLine.className = "service ok";
    add("p", "Each graph is harvested from the farm's own endpoint and SHACL-validated before loading:", box).className = "hint";
    table(["farm", "name", "graph", "harvestedAt", "triples"], graphs, box);
  } else {
    crossLine.textContent = data.hub.ok ? "All farms: the hub is online but holds no farm graph yet. Run the hub build." : "All farms: the hub is unavailable, so cross-farm questions cannot be answered right now. Farm A questions still work at the farm.";
    crossLine.className = "service down";
  }
  const states = [farmLine.className, crossLine.className];
  if (states.every(c => c.includes("ok"))) setPill("ok", "All services online");
  else if (states.some(c => c.includes("down")) && !states.some(c => c.includes("ok"))) setPill("down", "Services down");
  else setPill("degraded", "Some services limited");
}
function setPill(state, text) {
  const pill = document.querySelector("#status-pill");
  if (!pill) return;
  pill.dataset.state = state;
  pill.textContent = text;
}
document.querySelector("#status-pill")?.addEventListener("click", () => document.querySelector("#explain-open")?.click());
checkServices();

// ---------- One question box; scope is auto-detected unless a chip is chosen ----------
const askForm = document.querySelector("#ask-form");
const askResult = document.querySelector("#ask-result");
const scopeNames = {farm: "This farm (farm A, Kelmarsh)", cross: "All farms"};
const exampleButtons = [...askForm.querySelectorAll(".example")];
const ASK_TIMEOUT_MS = 130000;  // a little over the server's own 100 s budget, so the server's clear message wins
let asking = false;
exampleButtons.forEach(example => example.addEventListener("click", () => {
  if (asking) return;
  askForm.question.value = example.textContent.trim();
  askForm.scope.value = "auto";
  askForm.requestSubmit();
  askForm.scrollIntoView({behavior: "smooth", block: "start"});
}));

function showSpinner(started) {
  const box = add("div", "", askResult);
  box.className = "spinner";
  box.append(document.createElement("i"));
  const text = add("span", "", box);
  const tick = () => { text.textContent = `Detecting the scope, choosing hub or live farm query, and running it… ${Math.round((Date.now() - started) / 1000)} s (gives up after about 100 s)`; };
  tick();
  return setInterval(tick, 1000);
}

askForm.addEventListener("submit", async event => {
  event.preventDefault();
  if (asking) return;
  const question = askForm.question.value.trim() || askForm.question.placeholder;
  const scope = askForm.scope.value;
  const button = askForm.querySelector('button[type="submit"]');
  asking = true;
  button.disabled = true;
  exampleButtons.forEach(example => { example.disabled = true; });
  askResult.replaceChildren();
  const timer = showSpinner(Date.now());
  const controller = new AbortController();
  const abort = setTimeout(() => controller.abort(), ASK_TIMEOUT_MS);
  try {
    const response = await fetch("/ask", {method: "POST", signal: controller.signal, headers: {"Content-Type": "application/x-www-form-urlencoded"}, body: new URLSearchParams({question, scope})});
    let data;
    try { data = await response.json(); } catch { throw new Error(`The site answered with an unreadable response (HTTP ${response.status}).`); }
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
    askResult.replaceChildren();
    add("p", data.answer, askResult).className = "reply";
    if (data.notice) add("p", data.notice, askResult).className = "warning";
    const kinds = new Set(data.rows.map(row => row.recordKind).filter(Boolean));
    if (kinds.has("synthetic_demo")) add("p", "synthetic_demo: maintenance and repair records in this answer are fabricated for the demo, not real repairs.", askResult).className = "warning";
    if (kinds.has("curated_demo")) add("p", "curated_demo: fault links and confidences come from illustrative demo rules, not validated diagnoses.", askResult).className = "warning";
    add("p", `Scope: ${scopeNames[data.scope]} · ${data.scope_reason}`, askResult).className = "hint";
    if (data.tool === "farm_metric") {
      const live = add("p", "Live farm query: these numbers were measured just now by asking each farm's own endpoint for one aggregate. Nothing was read from the hub's copy, and raw readings stayed at the farms.", askResult);
      live.className = "live";
      const st = {ok: "answered", offline: "did not answer", not_published: "does not publish this signal", withheld: "withholds this signal", no_data: "no readings", error: "rejected the query", unsupported: "not supported yet"};
      add("p", "Farms asked: " + data.farm_metric.farms.map(f => `${f.name} (${st[f.status] || f.status})`).join(", "), askResult).className = "hint";
    }
    add("p", `Answered on ${data.where} · model ${data.model} · ${data.rows.length} row(s) from ${data.endpoint}` + (data.attempts.length ? ` · ${data.attempts.length} query attempt(s) corrected automatically` : ""), askResult).className = "hint";
    const details = add("details", "", askResult);
    add("summary", data.tool === "farm_metric" ? "Per-farm aggregate queries and result rows" : "Generated SPARQL and result rows", details);
    add("pre", data.sparql, details);
    if (data.columns.length) table(data.columns, data.rows, details);
    if (data.farm_sparql && form) {
      const show = add("button", "Show how farm A's own Ontop endpoint answers this, at the farm", askResult);
      show.type = "button";
      show.addEventListener("click", () => {
        document.querySelector("#at-the-farm").open = true;
        queryBox.value = data.farm_sparql;
        form.requestSubmit();
        journey.scrollIntoView({behavior: "smooth"});
      });
    }
  } catch (error) {
    askResult.replaceChildren();
    const message = error.name === "AbortError" ? "Timed out: no answer after more than two minutes. The language model or the hub may be overloaded. Try again in a minute." : error.message;
    add("p", message, askResult).className = "error";
  } finally {
    clearInterval(timer);
    clearTimeout(abort);
    asking = false;
    button.disabled = false;
    exampleButtons.forEach(example => { example.disabled = false; });
  }
});

// ---------- Raw SPARQL on the hub (hub panel) ----------
const hubForm = document.querySelector("#hub-form");
hubForm?.addEventListener("submit", async event => {
  event.preventDefault();
  const out = document.querySelector("#hub-result");
  const query = hubForm.sparql.value.trim() || hubForm.sparql.placeholder;
  out.replaceChildren();
  add("p", "Running on the hub…", out);
  try {
    const data = await phase("/hub-answer", query);
    out.replaceChildren();
    add("p", `${data.rows.length} row(s) from the hub.`, out).className = "hint";
    table(data.columns, data.rows, out);
    if (data.rows.some(row => row.recordKind === "synthetic_demo")) add("p", "synthetic_demo: these maintenance records are fabricated for the demo.", out).className = "warning";
  } catch (error) {
    out.replaceChildren();
    add("p", error.message, out).className = "error";
  }
});
