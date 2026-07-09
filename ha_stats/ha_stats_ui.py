"""
ha_stats_ui — optional ingress web UI for selecting tracked entities.

Runs as a *separate* add-on service from the sampler daemon (ha_stats.py).
It only edits configuration; it never samples or exports. Selections are
persisted back into the add-on's own options via the Supervisor API
(POST /addons/self/options), so they show up in the normal Configuration
tab and are read by the daemon on its next start. The daemon/CLI behaviour
is unchanged whether or not this UI is ever opened.

Requires in config.yaml:
  homeassistant_api: true   # GET /core/api/states
  hassio_api: true          # GET/POST /addons/self/*
  hassio_role: manager      # permission to write self options
"""

import json
import logging
import os

import aiohttp
from aiohttp import web

log = logging.getLogger("ha_stats_ui")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SUPERVISOR = "http://supervisor"
HA_API = f"{SUPERVISOR}/core/api"
ADDON_API = f"{SUPERVISOR}/addons/self"
# 8099 is Home Assistant's default ingress port (config.yaml omits it).
INGRESS_PORT = int(os.environ.get("HA_STATS_UI_PORT", "8099"))

_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}


# ── Supervisor / HA API helpers ───────────────────────────────────────────

# Rendered server-side via the template API to resolve each entity's area —
# areas live in the registry, not in /core/api/states.
_AREA_TEMPLATE = (
    "[{% for s in states %}"
    '{{ {"e": s.entity_id, "a": area_name(s.entity_id)} | tojson }}'
    '{{ "," if not loop.last }}'
    "{% endfor %}]"
)


async def _fetch_areas(session: aiohttp.ClientSession) -> dict:
    """Map entity_id -> area name (best-effort; empty dict on any failure)."""
    try:
        async with session.post(
            f"{HA_API}/template", headers=_HEADERS, json={"template": _AREA_TEMPLATE}
        ) as r:
            if r.status != 200:
                log.warning("area lookup failed (HTTP %s); continuing without areas", r.status)
                return {}
            pairs = json.loads(await r.text())
    except Exception as e:  # noqa: BLE001 — areas are optional, never fatal
        log.warning("area lookup error (%s); continuing without areas", e)
        return {}
    return {p["e"]: p["a"] for p in pairs if isinstance(p, dict) and p.get("a")}


async def _fetch_states(session: aiohttp.ClientSession) -> list[dict]:
    """All HA entities as {entity_id, name, domain, area, state}."""
    async with session.get(f"{HA_API}/states", headers=_HEADERS) as r:
        r.raise_for_status()
        states = await r.json()
    areas = await _fetch_areas(session)
    out = []
    for st in states:
        entity_id = st.get("entity_id", "")
        attrs = st.get("attributes", {})
        out.append({
            "entity_id": entity_id,
            "name": attrs.get("friendly_name", entity_id),
            "domain": entity_id.split(".")[0] if "." in entity_id else "",
            "area": areas.get(entity_id, ""),
            "state": st.get("state", ""),
        })
    out.sort(key=lambda e: e["entity_id"])
    return out


async def _fetch_options(session: aiohttp.ClientSession) -> dict:
    """The add-on's current, full options object."""
    async with session.get(f"{ADDON_API}/info", headers=_HEADERS) as r:
        r.raise_for_status()
        body = await r.json()
    return dict(body.get("data", {}).get("options") or {})


async def _save_entities(session: aiohttp.ClientSession, entities: list[str]) -> None:
    """Merge tracked_entities into the full options and persist them.

    The Supervisor replaces the whole options object, so we read the
    current options first and only swap tracked_entities — never dropping
    the S3 / rclone / scheduling keys.
    """
    options = await _fetch_options(session)
    options["tracked_entities"] = entities
    async with session.post(
        f"{ADDON_API}/options", headers=_HEADERS, json={"options": options}
    ) as r:
        text = await r.text()
        if r.status != 200:
            raise web.HTTPBadGateway(text=f"Supervisor rejected options ({r.status}): {text}")


# ── HTTP routes ───────────────────────────────────────────────────────────

async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


async def handle_entities(request: web.Request) -> web.Response:
    async with aiohttp.ClientSession() as session:
        entities = await _fetch_states(session)
        selected = (await _fetch_options(session)).get("tracked_entities", [])
    return web.json_response({"entities": entities, "selected": selected})


async def handle_save(request: web.Request) -> web.Response:
    body = await request.json()
    entities = body.get("tracked_entities")
    if not isinstance(entities, list) or not all(isinstance(e, str) for e in entities):
        raise web.HTTPBadRequest(text="tracked_entities must be a list of strings")
    async with aiohttp.ClientSession() as session:
        await _save_entities(session, entities)
    log.info("saved %d tracked entities via UI", len(entities))
    return web.json_response({"ok": True, "count": len(entities)})


async def handle_restart(request: web.Request) -> web.Response:
    """Restart the add-on so the daemon picks up the new selection."""
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{ADDON_API}/restart", headers=_HEADERS) as r:
            # The container goes down as part of this call; a clean response
            # is not guaranteed. Report best-effort.
            if r.status not in (200, 502, 503):
                text = await r.text()
                raise web.HTTPBadGateway(text=f"restart failed ({r.status}): {text}")
    return web.json_response({"ok": True})


def make_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.get("/", handle_index),
        web.get("/api/entities", handle_entities),
        web.post("/api/selected", handle_save),
        web.post("/api/restart", handle_restart),
    ])
    return app


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Stats Lake — Tracked entities</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body {
    font-family: system-ui, -apple-system, Roboto, sans-serif;
    margin: 0; padding: 16px; line-height: 1.4; max-width: 820px;
  }
  h1 { font-size: 1.25rem; margin: 0 0 2px; }
  .muted { opacity: .65; font-size: .85rem; }
  .card {
    border: 1px solid color-mix(in srgb, CanvasText 14%, transparent);
    border-radius: 12px; padding: 14px; margin-top: 14px;
  }
  label.field { display: block; font-size: .8rem; font-weight: 600; opacity: .8; margin-bottom: 6px; }
  input[type=text], input[type=search] {
    width: 100%; padding: 9px 11px; font-size: .95rem;
    border: 1px solid color-mix(in srgb, CanvasText 25%, transparent);
    border-radius: 8px; background: Canvas; color: CanvasText;
  }
  input:focus { outline: 2px solid #03a9f4; outline-offset: -1px; }

  /* type-ahead picker */
  .picker { position: relative; }
  .suggestions {
    position: absolute; z-index: 20; left: 0; right: 0; top: calc(100% + 4px);
    margin: 0; padding: 4px; list-style: none; max-height: 300px; overflow-y: auto;
    background: Canvas; color: CanvasText;
    border: 1px solid color-mix(in srgb, CanvasText 25%, transparent);
    border-radius: 8px; box-shadow: 0 8px 24px rgba(0,0,0,.25);
  }
  .suggestions[hidden] { display: none; }
  .suggestions li {
    padding: 7px 9px; border-radius: 6px; cursor: pointer;
    display: flex; flex-direction: column;
  }
  .suggestions li.active, .suggestions li:hover {
    background: color-mix(in srgb, #03a9f4 18%, transparent);
  }
  .suggestions .empty { cursor: default; opacity: .6; }

  /* configured table */
  .row-between { display: flex; align-items: center; gap: 10px; justify-content: space-between; flex-wrap: wrap; margin-bottom: 10px; }
  .row-between .grow { flex: 1 1 200px; }
  table { width: 100%; border-collapse: collapse; }
  tbody tr { border-top: 1px solid color-mix(in srgb, CanvasText 12%, transparent); }
  td { padding: 8px 4px; vertical-align: middle; }
  td.actions { width: 1%; white-space: nowrap; text-align: right; }
  .nm { font-size: .95rem; }
  .eid { font-family: ui-monospace, monospace; font-size: .8rem; opacity: .65; }
  .area {
    display: inline-block; font-size: .72rem; line-height: 1.5;
    padding: 0 8px; border-radius: 999px; margin-left: 6px; vertical-align: middle;
    background: color-mix(in srgb, CanvasText 12%, transparent);
    opacity: .85;
  }
  .area.none { opacity: .45; font-style: italic; }
  .del {
    border: 0; background: transparent; cursor: pointer; font-size: 1.1rem; line-height: 1;
    padding: 4px 8px; border-radius: 6px; color: #f44336;
  }
  .del:hover { background: color-mix(in srgb, #f44336 15%, transparent); }
  .empty-row td { padding: 16px 4px; opacity: .6; }

  /* actions */
  .bar { display: flex; gap: 8px; align-items: center; margin-top: 16px; flex-wrap: wrap; }
  button.act { padding: 9px 15px; border-radius: 8px; border: 0; cursor: pointer; font-size: .9rem; font-weight: 600; }
  .primary { background: #03a9f4; color: #fff; }
  .ghost { background: transparent; color: CanvasText; border: 1px solid color-mix(in srgb, CanvasText 25%, transparent); }
  button:disabled { opacity: .5; cursor: default; }
  .status { font-size: .85rem; min-height: 1.2em; }
  .status.ok { color: #4caf50; }
  .status.err { color: #f44336; }
  .count { font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
<h1>Tracked entities</h1>
<div class="muted">Pick the entities Stats Lake should sample. Saved to the add-on configuration.</div>

<div class="card picker">
  <label class="field" for="picker">Add an entity</label>
  <input id="picker" type="text" placeholder="Search by name, entity id or domain…" autocomplete="off"
         role="combobox" aria-expanded="false" aria-controls="suggestions" />
  <ul id="suggestions" class="suggestions" role="listbox" hidden></ul>
</div>

<div class="card">
  <div class="row-between">
    <strong>Configured <span class="count">(<span id="selCount">0</span>)</span></strong>
    <input id="filter" class="grow" type="search" placeholder="Filter configured entities…" />
  </div>
  <table>
    <tbody id="tbody"></tbody>
  </table>
</div>

<div class="bar">
  <button id="save" class="act primary">Save</button>
  <button id="saveRestart" class="act ghost">Save &amp; restart</button>
  <span id="status" class="status"></span>
</div>

<script>
const $ = (s) => document.querySelector(s);

function areaChip(area) {
  const a = document.createElement("span");
  a.className = "area";
  a.textContent = area;
  return a;
}

let ALL = [];               // [{entity_id, name, domain, area}]
const BYID = new Map();     // entity_id -> entity
let SELECTED = [];          // ordered list of entity_id
let activeIdx = -1;         // highlighted suggestion

// ── data load ────────────────────────────────────────────────────────────
async function load() {
  try {
    const r = await fetch("api/entities");
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    ALL = data.entities;
    BYID.clear();
    for (const e of ALL) BYID.set(e.entity_id, e);
    SELECTED = (data.selected || []).slice();
    setStatus("");
    renderTable();
  } catch (err) {
    setStatus("Failed to load entities: " + err.message, "err");
  }
}

// ── type-ahead picker ────────────────────────────────────────────────────
function candidates() {
  const q = $("#picker").value.trim().toLowerCase();
  const sel = new Set(SELECTED);
  let list = ALL.filter((e) => !sel.has(e.entity_id));
  if (q) list = list.filter((e) => (e.entity_id + " " + e.name + " " + e.domain + " " + (e.area || "")).toLowerCase().includes(q));
  return list.slice(0, 50);
}

function renderSuggestions() {
  const box = $("#picker");
  const ul = $("#suggestions");
  const items = candidates();
  activeIdx = -1;
  ul.innerHTML = "";
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "No matching entities";
    ul.append(li);
  } else {
    items.forEach((e, i) => {
      const li = document.createElement("li");
      li.setAttribute("role", "option");
      li.dataset.id = e.entity_id;
      const nm = document.createElement("span");
      nm.className = "nm"; nm.textContent = e.name;
      if (e.area) nm.append(" ", areaChip(e.area));
      const eid = document.createElement("span");
      eid.className = "eid"; eid.textContent = e.entity_id;
      li.append(nm, eid);
      li.addEventListener("mousedown", (ev) => { ev.preventDefault(); addEntity(e.entity_id); });
      ul.append(li);
    });
  }
  ul.hidden = false;
  box.setAttribute("aria-expanded", "true");
}

function hideSuggestions() {
  $("#suggestions").hidden = true;
  $("#picker").setAttribute("aria-expanded", "false");
  activeIdx = -1;
}

function moveActive(delta) {
  const opts = [...$("#suggestions").querySelectorAll("li[data-id]")];
  if (!opts.length) return;
  activeIdx = (activeIdx + delta + opts.length) % opts.length;
  opts.forEach((li, i) => li.classList.toggle("active", i === activeIdx));
  opts[activeIdx].scrollIntoView({ block: "nearest" });
}

function addEntity(id) {
  if (!SELECTED.includes(id)) SELECTED.push(id);
  $("#picker").value = "";
  hideSuggestions();
  renderTable();
  setStatus("");
  $("#picker").focus();
}

// ── configured table ─────────────────────────────────────────────────────
function renderTable() {
  const q = $("#filter").value.trim().toLowerCase();
  const tbody = $("#tbody");
  tbody.innerHTML = "";
  $("#selCount").textContent = SELECTED.length;

  const rows = SELECTED.filter((id) => {
    if (!q) return true;
    const e = BYID.get(id);
    const hay = (id + " " + (e ? e.name + " " + (e.area || "") : "")).toLowerCase();
    return hay.includes(q);
  });

  if (!SELECTED.length) {
    addEmptyRow("No entities configured yet — search above to add one.");
    return;
  }
  if (!rows.length) {
    addEmptyRow("No configured entities match “" + $("#filter").value + "”.");
    return;
  }

  for (const id of rows) {
    const e = BYID.get(id) || { entity_id: id, name: id };
    const tr = document.createElement("tr");

    const tdName = document.createElement("td");
    const nm = document.createElement("div");
    nm.className = "nm"; nm.textContent = e.name;
    if (e.area) nm.append(" ", areaChip(e.area));
    const eid = document.createElement("div");
    eid.className = "eid"; eid.textContent = id;
    if (!BYID.has(id)) { eid.textContent = id + "  (not currently in Home Assistant)"; }
    tdName.append(nm, eid);

    const tdAct = document.createElement("td");
    tdAct.className = "actions";
    const del = document.createElement("button");
    del.className = "del"; del.title = "Remove"; del.setAttribute("aria-label", "Remove " + id);
    del.textContent = "✕";
    del.addEventListener("click", () => {
      SELECTED = SELECTED.filter((x) => x !== id);
      renderTable();
      setStatus("");
    });
    tdAct.append(del);

    tr.append(tdName, tdAct);
    tbody.append(tr);
  }
}

function addEmptyRow(msg) {
  const tr = document.createElement("tr");
  tr.className = "empty-row";
  const td = document.createElement("td");
  td.colSpan = 2; td.textContent = msg;
  tr.append(td);
  $("#tbody").append(tr);
}

// ── save ─────────────────────────────────────────────────────────────────
function setStatus(msg, kind) {
  const el = $("#status");
  el.textContent = msg;
  el.className = "status" + (kind ? " " + kind : "");
}

async function save(restart) {
  $("#save").disabled = true;
  $("#saveRestart").disabled = true;
  setStatus("Saving…");
  try {
    const r = await fetch("api/selected", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tracked_entities: SELECTED }),
    });
    if (!r.ok) throw new Error(await r.text());
    if (restart) {
      setStatus("Saved. Restarting add-on…", "ok");
      await fetch("api/restart", { method: "POST" }).catch(() => {});
    } else {
      setStatus("Saved " + SELECTED.length + " entities. Restart the add-on to apply.", "ok");
    }
  } catch (err) {
    setStatus("Save failed: " + err.message, "err");
  } finally {
    $("#save").disabled = false;
    $("#saveRestart").disabled = false;
  }
}

// ── wiring ───────────────────────────────────────────────────────────────
$("#picker").addEventListener("input", renderSuggestions);
$("#picker").addEventListener("focus", renderSuggestions);
$("#picker").addEventListener("blur", () => setTimeout(hideSuggestions, 120));
$("#picker").addEventListener("keydown", (ev) => {
  if (ev.key === "ArrowDown") { ev.preventDefault(); moveActive(1); }
  else if (ev.key === "ArrowUp") { ev.preventDefault(); moveActive(-1); }
  else if (ev.key === "Escape") { hideSuggestions(); }
  else if (ev.key === "Enter") {
    ev.preventDefault();
    const opts = [...$("#suggestions").querySelectorAll("li[data-id]")];
    const pick = activeIdx >= 0 ? opts[activeIdx] : opts[0];
    if (pick) addEntity(pick.dataset.id);
  }
});
$("#filter").addEventListener("input", renderTable);
$("#save").addEventListener("click", () => save(false));
$("#saveRestart").addEventListener("click", () => save(true));
load();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    log.info("Stats Lake UI listening on :%d", INGRESS_PORT)
    web.run_app(make_app(), host="0.0.0.0", port=INGRESS_PORT, print=None)
