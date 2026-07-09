"""
ha_stats_ui — optional ingress web UI for selecting tracked entities.

Runs as a *separate* add-on service from the sampler daemon (ha_stats.py).
It only edits configuration; it never samples or exports. Selections are
persisted back into the add-on's own options via the Supervisor API
(POST /addons/self/options), so they show up in the normal Configuration
tab and are read by the daemon on its next start — exactly like editing
the config by hand. The daemon/CLI behaviour is unchanged whether or not
this UI is ever opened.

Requires in config.yaml:
  homeassistant_api: true   # GET /core/api/states
  hassio_api: true          # GET/POST /addons/self/*
  hassio_role: manager      # permission to write self options
"""

import logging
import os

import aiohttp
from aiohttp import web

log = logging.getLogger("ha_stats_ui")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SUPERVISOR = "http://supervisor"
HA_API = f"{SUPERVISOR}/core/api"
ADDON_API = f"{SUPERVISOR}/addons/self"
INGRESS_PORT = int(os.environ.get("HA_STATS_UI_PORT", "8099"))

_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}


# ── Supervisor / HA API helpers ───────────────────────────────────────────

async def _fetch_states(session: aiohttp.ClientSession) -> list[dict]:
    """All HA entities as {entity_id, name, domain, state}."""
    async with session.get(f"{HA_API}/states", headers=_HEADERS) as r:
        r.raise_for_status()
        states = await r.json()
    out = []
    for st in states:
        entity_id = st.get("entity_id", "")
        attrs = st.get("attributes", {})
        out.append({
            "entity_id": entity_id,
            "name": attrs.get("friendly_name", entity_id),
            "domain": entity_id.split(".")[0] if "." in entity_id else "",
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
    margin: 0; padding: 16px; line-height: 1.4;
  }
  header { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  h1 { font-size: 1.25rem; margin: 0 0 4px; }
  .muted { opacity: .65; font-size: .85rem; }
  .toolbar {
    position: sticky; top: 0; padding: 12px 0; margin-bottom: 8px;
    display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
    background: Canvas;
  }
  input[type=search] {
    flex: 1 1 220px; min-width: 160px; padding: 8px 10px;
    border: 1px solid color-mix(in srgb, CanvasText 25%, transparent);
    border-radius: 8px; background: Canvas; color: CanvasText; font-size: .95rem;
  }
  button {
    padding: 8px 14px; border-radius: 8px; border: 0; cursor: pointer;
    font-size: .9rem; font-weight: 600;
  }
  .primary { background: #03a9f4; color: #fff; }
  .ghost {
    background: transparent; color: CanvasText;
    border: 1px solid color-mix(in srgb, CanvasText 25%, transparent);
  }
  button:disabled { opacity: .5; cursor: default; }
  ul { list-style: none; margin: 0; padding: 0; }
  li {
    display: flex; align-items: center; gap: 10px; padding: 8px 6px;
    border-bottom: 1px solid color-mix(in srgb, CanvasText 12%, transparent);
  }
  li.hidden { display: none; }
  label { flex: 1; display: flex; flex-direction: column; cursor: pointer; }
  .eid { font-family: ui-monospace, monospace; font-size: .82rem; opacity: .7; }
  .nm { font-size: .95rem; }
  .status { font-size: .85rem; min-height: 1.2em; }
  .status.ok { color: #4caf50; }
  .status.err { color: #f44336; }
  .count { font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
<header>
  <div>
    <h1>Tracked entities</h1>
    <div class="muted">Pick the entities Stats Lake should sample. Saved to the add-on configuration.</div>
  </div>
</header>

<div class="toolbar">
  <input id="search" type="search" placeholder="Filter by name, entity id or domain…" autocomplete="off" />
  <span class="muted count"><span id="selCount">0</span> selected</span>
  <button id="save" class="primary" disabled>Save</button>
  <button id="saveRestart" class="ghost" disabled>Save &amp; restart</button>
</div>
<div id="status" class="status"></div>

<ul id="list"><li class="muted">Loading entities…</li></ul>

<script>
const $ = (s) => document.querySelector(s);
let ENTITIES = [];
let SELECTED = new Set();

function render() {
  const list = $("#list");
  list.innerHTML = "";
  for (const e of ENTITIES) {
    const li = document.createElement("li");
    li.dataset.hay = (e.entity_id + " " + e.name + " " + e.domain).toLowerCase();
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = SELECTED.has(e.entity_id);
    cb.addEventListener("change", () => {
      cb.checked ? SELECTED.add(e.entity_id) : SELECTED.delete(e.entity_id);
      updateCount();
    });
    const label = document.createElement("label");
    const nm = document.createElement("span");
    nm.className = "nm"; nm.textContent = e.name;
    const eid = document.createElement("span");
    eid.className = "eid"; eid.textContent = e.entity_id;
    label.append(nm, eid);
    li.append(cb, label);
    list.append(li);
  }
  applyFilter();
  updateCount();
}

function applyFilter() {
  const q = $("#search").value.trim().toLowerCase();
  for (const li of $("#list").children) {
    li.classList.toggle("hidden", q && !li.dataset.hay.includes(q));
  }
}

function updateCount() {
  $("#selCount").textContent = SELECTED.size;
  $("#save").disabled = false;
  $("#saveRestart").disabled = false;
}

function setStatus(msg, kind) {
  const el = $("#status");
  el.textContent = msg;
  el.className = "status" + (kind ? " " + kind : "");
}

async function load() {
  try {
    const r = await fetch("api/entities");
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    ENTITIES = data.entities;
    SELECTED = new Set(data.selected || []);
    render();
    setStatus("");
  } catch (err) {
    setStatus("Failed to load entities: " + err.message, "err");
    $("#list").innerHTML = "";
  }
}

async function save(restart) {
  $("#save").disabled = true;
  $("#saveRestart").disabled = true;
  setStatus("Saving…");
  try {
    const r = await fetch("api/selected", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tracked_entities: [...SELECTED] }),
    });
    if (!r.ok) throw new Error(await r.text());
    if (restart) {
      setStatus("Saved. Restarting add-on…", "ok");
      await fetch("api/restart", { method: "POST" }).catch(() => {});
    } else {
      setStatus("Saved " + SELECTED.size + " entities. Restart the add-on to apply.", "ok");
    }
  } catch (err) {
    setStatus("Save failed: " + err.message, "err");
  } finally {
    updateCount();
  }
}

$("#search").addEventListener("input", applyFilter);
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
