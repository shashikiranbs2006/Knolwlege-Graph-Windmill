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
document.querySelector("#status-pill")?.addEventListener("click", () => showView("sources"));
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
    const record = {question, scopeChoice: scope, ts: Date.now(), data};
    renderAnswerCard(record, askResult);
    saveHistory(record);
    const ex = document.querySelector("#examples");
    if (ex) ex.open = false;
    askResult.scrollIntoView({behavior: "smooth", block: "start"});
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

// ---------- App shell: sidebar navigation ----------
const VIEWS = {
  ask: ["Ask", "Ask in plain English. Every fact comes from the knowledge graph or from live farm queries."],
  sources: ["Sources & farms", "Which services are online and which farm graphs the hub holds (governance)."],
  graph: ["Knowledge graph", "Farms, turbines, alarms, faults and repairs, and how they link. Live from the hub, or the ontology itself."],
  queries: ["Queries (SPARQL)", "Run read-only SPARQL at farm A's own endpoint or on the hub, and inspect the query journey."],
  explain: ["How it works", "How a question becomes an answer, what the hub handles, and what stays at the farms."],
  history: ["History", "Questions asked in this browser. Click one to see its answer again."],
};
function showView(name) {
  if (!VIEWS[name]) name = "ask";
  document.querySelectorAll(".view").forEach(v => { v.hidden = v.dataset.view !== name; });
  document.querySelectorAll(".nav-item").forEach(n => n.classList.toggle("active", n.dataset.nav === name));
  document.querySelector("#view-title").textContent = VIEWS[name][0];
  document.querySelector("#view-lead").textContent = VIEWS[name][1];
  document.querySelector("#side")?.classList.remove("open");
  if (name === "history") renderHistory();
  if (name === "graph") onGraphShown();
  try { history.replaceState(null, "", "#" + name); } catch {}
  window.scrollTo(0, 0);
}
document.querySelectorAll(".nav-item").forEach(n => n.addEventListener("click", () => showView(n.dataset.nav)));
document.querySelector("#menu-btn")?.addEventListener("click", () => document.querySelector("#side").classList.toggle("open"));
document.querySelector("#side")?.addEventListener("click", e => { if (e.target.id === "side") e.target.classList.remove("open"); });

// ---------- History (kept in this browser only) ----------
const HKEY = "vkg-history-v1";
function loadHistory() { try { return JSON.parse(localStorage.getItem(HKEY) || "[]"); } catch { return []; } }
function saveHistory(record) {
  let list = loadHistory();
  list.unshift(record);
  list = list.slice(0, 15);
  while (list.length) {
    try { localStorage.setItem(HKEY, JSON.stringify(list)); break; } catch { list.pop(); }
  }
  updateHistCount();
}
function updateHistCount() {
  const n = loadHistory().length, badge = document.querySelector("#hist-count");
  if (badge) { badge.hidden = !n; badge.textContent = n; }
}
function renderHistory() {
  const box = document.querySelector("#history-list");
  const list = loadHistory();
  box.replaceChildren();
  if (!list.length) { add("p", "Nothing yet. Questions you ask are kept in this browser only.", box).className = "hint"; return; }
  list.forEach(record => {
    const item = add("div", "", box);
    item.className = "hist-item";
    item.tabIndex = 0;
    add("strong", record.question, item);
    add("small", new Date(record.ts).toLocaleString(), item);
    add("span", (record.data.answer || "").slice(0, 220), item).className = "hist-a";
    const open = () => { showView("ask"); askResult.replaceChildren(); renderAnswerCard(record, askResult); };
    item.addEventListener("click", open);
    item.addEventListener("keydown", e => { if (e.key === "Enter") open(); });
  });
}
document.querySelector("#hist-clear")?.addEventListener("click", () => { try { localStorage.removeItem(HKEY); } catch {} updateHistCount(); renderHistory(); });
updateHistCount();

// ---------- Answer card: headline, scope notes, chart + table, sources ----------
function numeric(value) { return value !== "" && value != null && Number.isFinite(Number(String(value).replace(/,/g, ""))); }
function chartFor(columns, rows, parent) {
  if (rows.length < 2 || rows.length > 14) return;
  const num = columns.find(c => rows.every(r => numeric(r[c])));
  const label = columns.find(c => c !== num && rows.some(r => !numeric(r[c])));
  if (!num || !label) return;
  const values = rows.map(r => Number(String(r[num]).replace(/,/g, "")));
  const max = Math.max(...values.map(Math.abs)) || 1;
  const box = add("div", "", parent);
  box.className = "chart";
  box.setAttribute("role", "img");
  box.setAttribute("aria-label", `Bar chart of ${num} by ${label}`);
  rows.forEach((row, i) => {
    const line = add("div", "", box);
    line.className = "bar";
    add("span", row[label], line).className = "lab";
    const track = add("span", "", line);
    track.className = "track";
    const fill = add("span", "", track);
    fill.className = "fill";
    fill.style.width = Math.max(2, Math.abs(values[i]) / max * 100) + "%";
    add("span", row[num], line).className = "val";
  });
  add("p", `Bars show ${num} by ${label}.`, parent).className = "hint";
}
function section(card, title) {
  const sec = add("div", "", card);
  sec.className = "sec";
  add("h3", title, sec);
  return sec;
}
function renderAnswerCard(record, parent) {
  const {question, ts, data} = record;
  const live = data.tool === "farm_metric";
  const wrap = add("div", "", parent);
  wrap.className = "qa";
  add("p", question, wrap).className = "qa-q";
  const card = add("div", "", wrap);
  card.className = "answer-card" + (live ? " islive" : "");
  const head = add("div", "", card);
  head.className = "answer-head";
  const badges = add("div", "", head);
  badges.className = "badges";
  add("span", live ? "Live farm query" : "Knowledge graph (hub)", badges).className = "badge" + (live ? " islive" : "");
  add("span", scopeNames[data.scope] || data.scope, badges).className = "badge muted";
  const kinds = new Set(data.rows.map(row => row.recordKind).filter(Boolean));
  if (kinds.has("synthetic_demo")) add("span", "synthetic demo data", badges).className = "badge warn";
  if (kinds.has("curated_demo")) add("span", "curated demo rules", badges).className = "badge warn";
  add("p", data.answer, head).className = "headline";
  const notes = [];
  if (data.notice) notes.push([data.notice, ""]);
  if (kinds.has("synthetic_demo")) notes.push(["synthetic_demo: maintenance and repair records in this answer are fabricated for the demo, not real repairs.", ""]);
  if (kinds.has("curated_demo")) notes.push(["curated_demo: fault links and confidences come from illustrative demo rules, not validated diagnoses.", ""]);
  if (live) notes.push(["Measured just now by asking each farm's own endpoint for one aggregate. Nothing was read from the hub's copy, and raw readings stayed at the farms.", "info"]);
  const sec0 = section(card, "Scope");
  add("p", data.scope_reason || "", sec0);
  if (notes.length) {
    const list = add("div", "", sec0);
    list.className = "notes";
    notes.forEach(([text, kind]) => add("p", text, list).className = "note " + kind);
  }
  if (live && data.farm_metric) {
    const st = {ok: "answered", offline: "did not answer", not_published: "does not publish this signal", withheld: "withholds this signal", no_data: "no readings", error: "rejected the query", unsupported: "not supported yet"};
    const chips = add("div", "", sec0);
    chips.className = "farm-chips";
    chips.style.marginTop = ".6rem";
    data.farm_metric.farms.forEach(f => add("span", `${f.name}: ${st[f.status] || f.status}`, chips).className = "fchip " + (f.status === "ok" ? "ok" : "bad"));
  }
  if (data.columns.length && data.rows.length) {
    const sec1 = section(card, `Results · ${data.rows.length} row(s)`);
    chartFor(data.columns, data.rows, sec1);
    const det = add("details", "", sec1);
    det.open = data.rows.length <= 12;
    add("summary", "Result table", det);
    table(data.columns, data.rows, det);
  }
  const src = section(card, "Sources & references");
  const dl = add("dl", "", src);
  dl.className = "srcgrid";
  const fact = (k, v) => { add("dt", k, dl); add("dd", v, dl); };
  fact("Answered on", data.where);
  fact("Endpoint", data.endpoint);
  if (live && data.farm_metric) fact("Farms queried", data.farm_metric.farms.map(f => f.name).join(", "));
  fact("Rows returned", String(data.rows.length));
  fact("Language model", data.model);
  fact("Asked at", new Date(ts).toLocaleString());
  if (data.attempts.length) fact("Corrections", `${data.attempts.length} query attempt(s) were corrected automatically`);
  const q = add("details", "", src);
  add("summary", live ? "Per-farm aggregate queries used" : "SPARQL used", q);
  add("pre", data.sparql, q);
  if (data.attempts.length) {
    const at = add("details", "", src);
    add("summary", "Earlier attempts and why they were retried", at);
    add("pre", typeof data.attempts[0] === "string" ? data.attempts.join("\n\n") : JSON.stringify(data.attempts, null, 2), at);
  }
  const tools = add("div", "", card);
  tools.className = "tools";
  const copy = add("button", "Copy answer", tools);
  copy.type = "button";
  copy.addEventListener("click", () => navigator.clipboard?.writeText(data.answer).then(() => { copy.textContent = "Copied"; setTimeout(() => { copy.textContent = "Copy answer"; }, 1500); }));
  if (data.farm_sparql && form) {
    const show = add("button", "Show how farm A's own endpoint answers this", tools);
    show.type = "button";
    show.addEventListener("click", () => {
      showView("queries");
      document.querySelector("#at-the-farm").open = true;
      queryBox.value = data.farm_sparql;
      form.requestSubmit();
    });
  }
}

// ---------- Graph tab: interactive force-directed view of the knowledge graph ----------
const KIND = {
  farm: ["Wind farm", "#0f6b50", 20], turbine: ["Turbine", "#4aa37f", 9], component: ["Component", "#7a8fa6", 12],
  fault: ["Fault", "#d9534f", 15], alarm: ["Alarm", "#e0a82e", 11], repair: ["Repair / maintenance", "#1f4f8f", 12],
};
const G = {data: null, mode: "live", farmId: "", farmList: [], data0: null, nodes: [], links: [], hidden: new Set(), farm: "", sel: null, tx: 0, ty: 0, k: 1, raf: 0, alpha: 1, loaded: ""};
const gsvg = document.querySelector("#g-svg");
const NS = "http://www.w3.org/2000/svg";
function sv(tag, attrs = {}, parent) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.append(n);
  return n;
}
function gsize() { const r = gsvg.getBoundingClientRect(); return [r.width || 800, r.height || 560]; }

const MODE_TEXT = {
  live: "Hub view: the shared hub's summary. It holds one validated named graph per farm plus curated alarm-to-fault knowledge, not the time series.",
  farm: "Farm view: this farm's OWN knowledge graph, read from its own Ontop endpoint (a virtual graph over its database). Node size shows how many instances exist; links show how the classes connect. Dashed links are time series that stay at the farm. The right-hand chain shows how the graph reaches the hub.",
  all: "Combined view: every farm's own graph side by side, each harvested into its own named graph in the hub, plus the hub's shared curated knowledge.",
  schema: "Ontology view: the shared wind-core vocabulary every farm maps to. Farms differ in their data and mappings but use these same classes.",
};
function modeButtons() {
  const box = document.querySelector("#g-modes");
  box.replaceChildren();
  const entries = [["live", "", "Hub"], ...G.farmList.map(f => ["farm", f.id, f.name]), ["all", "", "Combined"], ["schema", "", "Ontology"]];
  entries.forEach(([mode, farm, label]) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "segbtn" + (G.mode === mode && G.farmId === farm ? " active" : "");
    b.textContent = label;
    b.addEventListener("click", () => { G.tx = G.ty = 0; G.k = 1; loadGraph(mode, farm); });
    box.append(b);
  });
}
async function loadGraph(mode, farm = "") {
  if (!G.farmList.length) {
    try { G.farmList = await (await fetch("/graph?mode=farms")).json(); } catch { G.farmList = []; }
  }
  G.mode = mode;
  G.farmId = farm;
  modeButtons();
  document.querySelector("#g-explain").textContent = MODE_TEXT[mode];
  const status = document.querySelector("#g-status");
  status.className = "hint";
  status.textContent = mode === "live" || mode === "schema" ? "Loading the graph…" : "Reading the farm's own endpoint (this can take 10 to 30 seconds the first time)…";
  try {
    const response = await fetch(`/graph?mode=${mode}&farm=${encodeURIComponent(farm)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    G.data = data;
    G.loaded = mode + "|" + farm;
    const farms = data.nodes.filter(n => n.kind === "farm");
    const sel = document.querySelector("#g-farm");
    sel.replaceChildren(new Option("All farms", ""));
    if (mode === "live") farms.forEach(f => sel.append(new Option(f.label, f.farm)));
    sel.disabled = mode !== "live";
    G.farm = "";
    G.hidden.clear();
    G.sel = null;
    G.nodes = [];
    buildGraph();
    status.textContent = `${data.nodes.length} nodes, ${data.edges.length} links · source: ${data.source}` + (data.notes?.length ? " · " + data.notes.join("; ") : "");
  } catch (error) {
    G.data = null;
    G.loaded = "";
    gsvg.replaceChildren();
    status.textContent = `Could not load this graph: ${error.message}`;
    status.className = "error";
  }
}

function visibleSet() {
  const keep = new Set();
  for (const n of G.data.nodes) {
    if (G.hidden.has(n.kind)) continue;
    keep.add(n.id);
  }
  if (G.farm) {
    // keep the farm, its turbines/repairs, and whatever those connect to
    const own = new Set(["farm:" + G.farm]);
    for (const e of G.data.edges) {
      const touches = e.farm === G.farm || e.source === "farm:" + G.farm || e.target === "farm:" + G.farm;
      if (touches) { own.add(e.source); own.add(e.target); }
    }
    for (const e of G.data.edges) if (own.has(e.source) && own.has(e.target) === false && G.data.nodes.find(n => n.id === e.target)?.kind !== "farm") own.add(e.target);
    for (const e of G.data.edges) if (own.has(e.target) && !own.has(e.source) && G.data.nodes.find(n => n.id === e.source)?.kind !== "farm") own.add(e.source);
    for (const id of [...keep]) if (!own.has(id)) keep.delete(id);
  }
  return keep;
}

function buildGraph() {
  gsvg.replaceChildren();
  const [w, h] = gsize();
  const keep = visibleSet();
  const old = new Map(G.nodes.map(n => [n.id, n]));
  G.nodes = G.data.nodes.filter(n => keep.has(n.id)).map((n, i) => {
    const prev = old.get(n.id);
    const angle = i * 2.4, r = 30 + 14 * Math.sqrt(i);
    return Object.assign({}, n, {x: prev?.x ?? w / 2 + Math.cos(angle) * r, y: prev?.y ?? h / 2 + Math.sin(angle) * r, vx: 0, vy: 0, deg: 0});
  });
  const byId = new Map(G.nodes.map(n => [n.id, n]));
  G.links = G.data.edges.filter(e => byId.has(e.source) && byId.has(e.target)).map(e => ({...e, s: byId.get(e.source), t: byId.get(e.target)}));
  G.links.forEach(l => { l.s.deg++; l.t.deg++; });
  G.root = sv("g", {}, gsvg);
  G.root.setAttribute("transform", `translate(${G.tx} ${G.ty}) scale(${G.k})`);
  const lg = sv("g", {}, G.root), ng = sv("g", {}, G.root);
  G.links.forEach(l => {
    l.el = sv("line", {"stroke-width": l.weight ? Math.min(6, 1 + Math.log2(1 + l.weight) / 3) : 1.3, class: l.dashed ? "dashed" : ""}, lg);
    const tip = sv("title", {}, l.el);
    tip.textContent = `${l.s.label} — ${l.label} → ${l.t.label}` + (l.detail ? ` (${l.detail})` : "");
  });
  G.nodes.forEach(n => {
    const [name, color, size] = KIND[n.kind] || ["Node", "#888", 10];
    n.r = n.count ? 9 + Math.min(20, Math.log10(1 + n.count) * 3.6) : size + Math.min(8, n.deg * 0.6);
    n.el = sv("g", {class: "gnode" + (n.kind === "turbine" ? " tlabel" : "")}, ng);
    sv("circle", {r: n.r, fill: color}, n.el);
    const shown = n.count ? `${n.label} · ${n.count.toLocaleString()}` : n.label;
    sv("text", {y: n.r + 12, "text-anchor": "middle"}, n.el).textContent = shown.length > 30 ? shown.slice(0, 29) + "…" : shown;
    sv("title", {}, n.el).textContent = `${n.label} (${name})`;
    n.el.addEventListener("click", e => { e.stopPropagation(); select(n); });
    drag(n);
  });
  legend();
  G.alpha = 1;
  cancelAnimationFrame(G.raf);
  tick();
  if (G.sel) select(byId.get(G.sel.id) || null);
}

function legend() {
  const box = document.querySelector("#g-legend");
  box.replaceChildren();
  const kinds = [...new Set(G.data.nodes.map(n => n.kind))];
  kinds.forEach(k => {
    const count = G.data.nodes.filter(n => n.kind === k).length;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "lg" + (G.hidden.has(k) ? " off" : "");
    const dot = document.createElement("i");
    dot.style.background = KIND[k]?.[1] || "#888";
    b.append(dot, `${KIND[k]?.[0] || k} (${count})`);
    b.addEventListener("click", () => { G.hidden.has(k) ? G.hidden.delete(k) : G.hidden.add(k); buildGraph(); });
    box.append(b);
  });
}

function tick() {
  const [w, h] = gsize();
  const nodes = G.nodes;
  if (G.alpha > 0.02) {
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy || 0.01;
        if (d2 > 160000) continue;
        const f = 6500 / d2, d = Math.sqrt(d2);
        dx = dx / d * f; dy = dy / d * f;
        a.vx += dx; a.vy += dy; b.vx -= dx; b.vy -= dy;
      }
    }
    for (const l of G.links) {
      const dx = l.t.x - l.s.x, dy = l.t.y - l.s.y, d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const rest = 90 + l.s.r + l.t.r, f = (d - rest) * 0.04;
      const fx = dx / d * f, fy = dy / d * f;
      l.s.vx += fx; l.s.vy += fy; l.t.vx -= fx; l.t.vy -= fy;
    }
    for (const n of nodes) {
      n.vx += (w / 2 - n.x) * 0.006; n.vy += (h / 2 - n.y) * 0.006;
      if (n.fixed) { n.vx = n.vy = 0; continue; }
      n.x += n.vx * G.alpha; n.y += n.vy * G.alpha;
      n.vx *= 0.82; n.vy *= 0.82;
    }
    G.alpha *= 0.985;
  }
  for (const n of nodes) n.el.setAttribute("transform", `translate(${n.x} ${n.y})`);
  for (const l of G.links) { l.el.setAttribute("x1", l.s.x); l.el.setAttribute("y1", l.s.y); l.el.setAttribute("x2", l.t.x); l.el.setAttribute("y2", l.t.y); }
  if (G.alpha > 0.02 || G.dragging) G.raf = requestAnimationFrame(tick);
}

function drag(n) {
  n.el.addEventListener("pointerdown", e => {
    e.stopPropagation();
    n.el.setPointerCapture(e.pointerId);
    G.dragging = true;
    n.fixed = true;
    const move = ev => {
      const r = gsvg.getBoundingClientRect();
      n.x = (ev.clientX - r.left - G.tx) / G.k;
      n.y = (ev.clientY - r.top - G.ty) / G.k;
      G.alpha = Math.max(G.alpha, 0.3);
    };
    const up = () => {
      n.fixed = false;
      G.dragging = false;
      n.el.removeEventListener("pointermove", move);
      n.el.removeEventListener("pointerup", up);
      cancelAnimationFrame(G.raf);
      tick();
    };
    n.el.addEventListener("pointermove", move);
    n.el.addEventListener("pointerup", up);
    cancelAnimationFrame(G.raf);
    tick();
  });
}

function select(n) {
  G.sel = n;
  const box = document.querySelector("#g-inspect");
  box.replaceChildren();
  G.nodes.forEach(x => x.el.classList.toggle("sel", x === n));
  const linked = new Set();
  if (n) G.links.forEach(l => { if (l.s === n) linked.add(l.t); if (l.t === n) linked.add(l.s); });
  G.nodes.forEach(x => x.el.classList.toggle("dim", !!n && x !== n && !linked.has(x)));
  G.links.forEach(l => l.el.classList.toggle("dim", !!n && l.s !== n && l.t !== n));
  if (!n) { add("h3", "Inspect", box); add("p", "Click a node to see what it is and what it links to.", box).className = "hint"; return; }
  add("h3", KIND[n.kind]?.[0] || n.kind, box);
  add("h4", n.label, box);
  if (n.note) add("p", n.note, box);
  if (n.recordKind) add("span", n.recordKind, box).className = "badge warn";
  if (n.top_values?.length) {
    add("h3", n.top_title || "Details", box).style.marginTop = ".8rem";
    const tv = add("ul", "", box);
    n.top_values.forEach(v => add("li", v, tv));
  }
  const out = G.links.filter(l => l.s === n), inn = G.links.filter(l => l.t === n);
  const list = (title, ls, other) => {
    if (!ls.length) return;
    add("h3", title, box).style.marginTop = ".8rem";
    const ul = add("ul", "", box);
    ls.slice(0, 14).forEach(l => {
      const li = add("li", `${l.label} → ${l[other].label}${l.detail ? " (" + l.detail + ")" : ""}`, ul);
      li.style.cursor = "pointer";
      li.addEventListener("click", () => select(l[other]));
    });
    if (ls.length > 14) add("li", `${ls.length - 14} more`, ul);
  };
  list("Links out", out, "t");
  list("Linked from", inn, "s");
}

// pan / zoom
gsvg.addEventListener("wheel", e => {
  e.preventDefault();
  const r = gsvg.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
  const k = Math.min(3, Math.max(0.3, G.k * (e.deltaY < 0 ? 1.12 : 0.89)));
  G.tx = mx - (mx - G.tx) * k / G.k; G.ty = my - (my - G.ty) * k / G.k; G.k = k;
  G.root?.setAttribute("transform", `translate(${G.tx} ${G.ty}) scale(${G.k})`);
  gsvg.classList.toggle("zoomed", G.k > 1.4);
}, {passive: false});
gsvg.addEventListener("pointerdown", e => {
  if (e.target !== gsvg) return;
  const sx = e.clientX - G.tx, sy = e.clientY - G.ty;
  gsvg.setPointerCapture(e.pointerId);
  gsvg.style.cursor = "grabbing";
  const move = ev => { G.tx = ev.clientX - sx; G.ty = ev.clientY - sy; G.root?.setAttribute("transform", `translate(${G.tx} ${G.ty}) scale(${G.k})`); };
  const up = () => { gsvg.style.cursor = ""; gsvg.removeEventListener("pointermove", move); gsvg.removeEventListener("pointerup", up); };
  gsvg.addEventListener("pointermove", move);
  gsvg.addEventListener("pointerup", up);
});
gsvg.addEventListener("click", e => { if (e.target === gsvg) select(null); });
document.querySelector("#g-farm")?.addEventListener("change", e => { G.farm = e.target.value; buildGraph(); });
document.querySelector("#g-reset")?.addEventListener("click", () => { G.tx = G.ty = 0; G.k = 1; G.hidden.clear(); G.farm = ""; document.querySelector("#g-farm").value = ""; G.nodes = []; if (G.data) buildGraph(); });
document.querySelector("#g-search")?.addEventListener("input", e => {
  const q = e.target.value.trim().toLowerCase();
  if (!q) return select(null);
  const hit = G.nodes.find(n => n.label.toLowerCase().includes(q));
  if (hit) select(hit);
});
function onGraphShown() { if (!G.data) loadGraph(G.mode, G.farmId); }

showView((location.hash || "#ask").slice(1));
