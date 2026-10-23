// Voice Agent Platform — portal + call dashboard. Plain JS, hash routing.
"use strict";

const app = document.getElementById("app");
let pollTimer = null;
let activeRoom = null; // live browser test call (LivekitClient.Room)

// ---------- helpers ----------
const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const err = new Error(formatError(data) || `HTTP ${res.status}`);
    err.details = data;
    throw err;
  }
  return data;
}

function formatError(data) {
  if (!data) return "";
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail))
    return data.detail.map((d) => `${(d.loc || []).filter((x) => x !== "body").join(".")}: ${d.msg}`).join("\n");
  return JSON.stringify(data);
}

function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), 3200);
}

const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "—");
const fmtDur = (s) => (s == null ? "—" : s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`);
const fmtMs = (ms) => (ms == null ? "—" : `${Math.round(ms)} ms`);
const offset = (ts, base) => {
  if (!ts || !base) return "";
  const s = Math.max(0, ts - base);
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
};

function statusBadge(status) {
  const cls = { completed: "ok", in_progress: "info", dialing: "info", queued: "", voicemail: "warn", no_answer: "warn", busy: "warn", rejected: "warn", failed: "bad",
    running: "info", paused: "warn", pending: "", calling: "info", done: "ok", exhausted: "bad" }[status] ?? "";
  return `<span class="badge ${cls}">${esc((status || "").replace(/_/g, " "))}</span>`;
}
function outcomeBadge(outcome, postcall) {
  if (!outcome) return postcall === "failed" ? `<span class="badge bad">processing failed</span>` : postcall === "done" ? "—" : `<span class="badge">pending</span>`;
  const cls = /achieved$|^goal|^completed$|^rescheduled$/.test(outcome) && !/^not_|partially/.test(outcome) ? "ok" : /^not_|wrong|failed|rejected|declined|refused/.test(outcome) ? "bad" : "warn";
  return `<span class="badge ${cls}">${esc(outcome.replace(/_/g, " "))}</span>`;
}
function latencyClass(ms) {
  if (ms == null) return "";
  return ms < 1200 ? "ok" : ms < 2000 ? "warn" : "bad";
}

// ---------- routing ----------
const routes = [
  [/^#\/agents\/?$/, agentsPage],
  [/^#\/agents\/new$/, () => agentForm(null)],
  [/^#\/agents\/([\w-]+)\/edit$/, (m) => agentForm(m[1])],
  [/^#\/calls\/?$/, callsPage],
  [/^#\/calls\/([\w-]+)$/, (m) => callDetailPage(m[1])],
  [/^#\/test\/([\w-]+)$/, (m) => testCallPage(m[1])],
  [/^#\/webhooks$/, webhookInboxPage],
  [/^#\/campaigns\/?$/, campaignsPage],
  [/^#\/campaigns\/new$/, newCampaignPage],
  [/^#\/campaigns\/([\w-]+)$/, (m) => campaignDetailPage(m[1])],
];

function route() {
  clearInterval(pollTimer);
  pollTimer = null;
  const hash = location.hash || "#/agents";
  if (!hash.startsWith("#/test/") && activeRoom) {
    activeRoom.disconnect();
    activeRoom = null;
  }
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", hash.startsWith(`#/${a.dataset.nav}`)));
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) return fn(m);
  }
  location.hash = "#/agents";
}
window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", async () => {
  route();
  try {
    const h = await api("/api/health");
    const b = document.getElementById("telephony-badge");
    b.textContent = h.telephony ? "Phone calling: on" : "Phone calling: off (browser only)";
    b.className = `badge ${h.telephony ? "ok" : "warn"}`;
    window.TELEPHONY = h.telephony;
  } catch {}
});

// ---------- agents list ----------
async function loadTemplates() {
  loadTemplates.cache ??= await api("/api/templates");
  return loadTemplates.cache;
}

async function agentsPage() {
  const [agents, templates] = await Promise.all([api("/api/agents"), loadTemplates()]);
  app.innerHTML = `
    <div class="page-head">
      <h1>Agents</h1><div class="spacer"></div>
      <select id="tpl" class="btn" style="width:auto">
        <option value="">New from template…</option>
        ${templates.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("")}
      </select>
      <a class="btn primary" href="#/agents/new">+ New agent</a>
    </div>
    <div class="card table-wrap">
      ${agents.length === 0 ? `<div class="empty">No agents yet. Create one, or start from a template.</div>` : `
      <table>
        <thead><tr><th>Name</th><th>LLM</th><th>Voice</th><th>Language</th><th class="num">Max</th><th class="num">Fields</th><th class="num">Tools</th><th class="num">Calls</th><th></th></tr></thead>
        <tbody>${agents.map((a) => `
          <tr>
            <td><a href="#/agents/${a.id}/edit"><b>${esc(a.name)}</b></a><div class="muted" style="font-size:12px">${esc((a.first_message || "").slice(0, 70))}</div></td>
            <td>${esc(a.llm_provider)} · <span class="mono">${esc(a.llm_model)}</span></td>
            <td>${esc(a.tts_provider)} · ${esc(a.tts_voice)}</td>
            <td>${esc(a.language)}</td>
            <td class="num">${fmtDur(a.max_call_duration_seconds)}</td>
            <td class="num">${a.extraction_schema.length}</td>
            <td class="num">${a.tools.length}</td>
            <td class="num"><a href="#/calls?agent=${a.id}">${a.call_count}</a></td>
            <td style="white-space:nowrap">
              <button class="btn small primary" data-call="${a.id}">Call</button>
              <a class="btn small" href="#/agents/${a.id}/edit">Edit</a>
              <button class="btn small danger" data-del="${a.id}">Delete</button>
            </td>
          </tr>`).join("")}
        </tbody>
      </table>`}
    </div>`;
  app.querySelector("#tpl").onchange = (e) => {
    if (!e.target.value) return;
    sessionStorage.setItem("vap-template", e.target.value);
    location.hash = "#/agents/new";
  };
  app.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
    const a = agents.find((x) => x.id === b.dataset.del);
    if (!confirm(`Delete agent "${a.name}"? Its past calls stay in the call log.`)) return;
    await api(`/api/agents/${a.id}`, { method: "DELETE" });
    toast("Agent deleted");
    agentsPage();
  }));
  app.querySelectorAll("[data-call]").forEach((b) => (b.onclick = () => startCallModal(agents.find((x) => x.id === b.dataset.call))));
}

// ---------- start call modal ----------
function placeholdersOf(agent) {
  const set = new Set();
  // {{tool.field}} placeholders are filled from run_at_start tools, not by the caller
  const startArgs = (agent.tools || []).filter((t) => t.run_at_start).flatMap((t) => Object.values(t.start_arguments || {}).filter((v) => typeof v === "string"));
  for (const t of [agent.system_prompt, agent.first_message, agent.voicemail_message, ...(agent.end_call_conditions || []), ...startArgs])
    for (const m of (t || "").matchAll(/\{\{\s*([a-zA-Z_]\w*)\s*\}\}/g)) set.add(m[1]);
  return [...set];
}

function startCallModal(agent) {
  const vars = placeholdersOf(agent);
  const root = document.getElementById("modal-root");
  root.innerHTML = `
    <div class="modal-backdrop"><div class="modal">
      <h2>Start a call — ${esc(agent.name)}</h2>
      <label class="field"><span>Phone number <small>E.164, e.g. +919876543210</small></span>
        <input type="tel" id="m-number" placeholder="+91…" ${window.TELEPHONY ? "" : "disabled"}></label>
      ${vars.length ? `<h3>Call variables</h3>${vars.map((v) => `
        <label class="field"><span class="mono">{{${esc(v)}}}</span><input type="text" data-var="${esc(v)}"></label>`).join("")}` : ""}
      <div id="m-err"></div>
      <div class="actions">
        <button class="btn" id="m-cancel">Cancel</button>
        <button class="btn" id="m-web">Test in browser</button>
        <button class="btn primary" id="m-phone" ${window.TELEPHONY ? "" : "disabled title='SIP_TRUNK_ID not configured'"}>Call phone</button>
      </div>
    </div></div>`;
  const close = () => (root.innerHTML = "");
  const variables = () => Object.fromEntries([...root.querySelectorAll("[data-var]")].filter((i) => i.value.trim()).map((i) => [i.dataset.var, i.value.trim()]));
  const fail = (e) => (root.querySelector("#m-err").innerHTML = `<div class="errors">${esc(e.message)}</div>`);
  root.querySelector("#m-cancel").onclick = close;
  root.querySelector(".modal-backdrop").onclick = (e) => e.target === e.currentTarget && close();
  root.querySelector("#m-phone").onclick = async () => {
    try {
      const r = await api("/api/calls/phone", { method: "POST", body: { agent_id: agent.id, to_number: root.querySelector("#m-number").value, variables: variables() } });
      close();
      toast("Dialing…");
      location.hash = `#/calls/${r.call_id}`;
    } catch (e) { fail(e); }
  };
  root.querySelector("#m-web").onclick = async () => {
    try {
      const r = await api("/api/calls/web", { method: "POST", body: { agent_id: agent.id, variables: variables() } });
      close();
      sessionStorage.setItem(`vap-web-${r.call_id}`, JSON.stringify(r));
      location.hash = `#/test/${r.call_id}`;
    } catch (e) { fail(e); }
  };
}

// ---------- browser test call (WebRTC) ----------
async function testCallPage(callId) {
  const info = JSON.parse(sessionStorage.getItem(`vap-web-${callId}`) || "null");
  if (!info) { location.hash = `#/calls/${callId}`; return; }
  app.innerHTML = `
    <div class="page-head"><h1>Browser test call</h1><div class="spacer"></div>
      <a class="btn" href="#/calls/${callId}">Call details</a></div>
    <div class="card">
      <p id="tc-status"><span class="live-dot"></span>Connecting…</p>
      <p class="muted">Speak into your microphone. You can interrupt the agent at any time.</p>
      <button class="btn danger" id="tc-hangup">Hang up</button>
    </div>
    <div class="card"><h2>Live transcript</h2><div class="transcript" id="tc-transcript"><p class="muted">Waiting for the agent…</p></div></div>
    <div id="tc-audio" hidden></div>`;
  const statusEl = app.querySelector("#tc-status");
  const tEl = app.querySelector("#tc-transcript");
  const LK = window.LivekitClient;
  if (!LK) { statusEl.textContent = "livekit-client failed to load (check network)."; return; }

  const room = new LK.Room({ adaptiveStream: true, dynacast: true });
  activeRoom = room;
  const segments = new Map();
  const renderTranscript = () => {
    tEl.innerHTML = [...segments.values()].map((s) => `
      <div class="turn ${s.agent ? "agent" : "user"}"><div class="ts"></div>
      <div class="bubble"><span class="speaker">${s.agent ? "Agent" : "You"}</span>${esc(s.text)}</div></div>`).join("") || `<p class="muted">…</p>`;
  };
  room.on(LK.RoomEvent.TrackSubscribed, (track) => {
    if (track.kind === "audio") app.querySelector("#tc-audio").appendChild(track.attach());
  });
  room.on(LK.RoomEvent.Disconnected, () => {
    statusEl.innerHTML = `Call ended. <a href="#/calls/${callId}">View transcript, extraction and latency →</a>`;
    activeRoom = null;
  });
  if (room.registerTextStreamHandler) {
    room.registerTextStreamHandler("lk.transcription", async (reader, participant) => {
      const id = reader.info.attributes?.["lk.segment_id"] || reader.info.id;
      const agent = participant.identity !== room.localParticipant.identity;
      for await (const chunk of reader) {
        const s = segments.get(id) || { agent, text: "" };
        s.text += chunk;
        segments.set(id, s);
        renderTranscript();
      }
    });
  }
  app.querySelector("#tc-hangup").onclick = async () => {
    await api(`/api/calls/${callId}/hangup`, { method: "POST" }).catch(() => {});
    room.disconnect();
  };
  try {
    await room.connect(info.livekit_url, info.token);
    await room.localParticipant.setMicrophoneEnabled(true);
    statusEl.innerHTML = `<span class="live-dot"></span>Live — connected to room <span class="mono">${esc(info.room)}</span>`;
  } catch (e) {
    statusEl.textContent = `Could not connect: ${e.message}`;
  }
}

// ---------- agent form ----------
const DEFAULTS = {
  name: "", system_prompt: "", first_message: "",
  llm_provider: "google", llm_model: "gemini-2.5-flash", llm_temperature: 0.4,
  tts_provider: "sarvam", tts_model: "bulbul:v3", tts_voice: "simran",
  language: "en-IN", stt_model: "nova-3", max_call_duration_seconds: 180,
  end_call_conditions: [], extraction_schema: [], tools: [],
  outcome_labels: [
    { label: "goal_achieved", description: "The call's goal was fully met." },
    { label: "partially_achieved", description: "Some progress, but the goal was not fully met." },
    { label: "not_achieved", description: "The caller engaged but the goal was not met (declined, refused, etc.)." },
    { label: "callback_requested", description: "The caller asked to be contacted again later." },
    { label: "wrong_person", description: "Wrong number or not the intended person." },
    { label: "voicemail", description: "Reached voicemail or an answering machine." },
  ],
  post_call_webhook_url: "", post_call_webhook_secret: "",
  voicemail_detection: false, voicemail_action: "hang_up", voicemail_message: "",
};

const MODEL_HINTS = {
  google: ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"],
  openai: ["gpt-4.1-mini", "gpt-4o-mini", "gpt-4.1"],
  inference: ["openai/gpt-4.1-mini", "google/gemini-2.5-flash", "openai/gpt-4o-mini"],
};
const VOICE_HINTS = {
  sarvam: { models: ["bulbul:v3", "bulbul:v2"], voices: [] }, // filled from /api/voices per model
  deepgram: { models: ["aura-2"], voices: ["aura-2-thalia-en", "aura-2-andromeda-en", "aura-2-apollo-en", "aura-2-helena-en"] },
  inference: { models: ["cartesia/sonic-2", "elevenlabs/eleven_flash_v2_5"], voices: [] },
};

async function agentForm(agentId) {
  let a;
  if (agentId) a = await api(`/api/agents/${agentId}`);
  else {
    const tpl = sessionStorage.getItem("vap-template");
    sessionStorage.removeItem("vap-template");
    const template = tpl ? (await loadTemplates()).find((t) => t.id === tpl) : null;
    a = structuredClone(template ? { ...DEFAULTS, ...template.config } : DEFAULTS);
  }
  a.post_call_webhook_url ??= "";
  a.post_call_webhook_secret ??= "";

  app.innerHTML = `
    <div class="page-head"><a href="#/agents">← Agents</a><h1>${agentId ? "Edit agent" : "New agent"}</h1><div class="spacer"></div>
      ${agentId ? `<button class="btn" id="f-call">Start call</button>` : ""}
      <button class="btn primary" id="f-save">Save agent</button></div>
    <div id="f-errors"></div>
    <form id="agent-form" onsubmit="return false">
      <div class="card">
        <h2>Basics</h2>
        <div class="grid two">
          <label class="field"><span>Name</span><input type="text" name="name" value="${esc(a.name)}" required></label>
          <label class="field"><span>Language</span>
            <select name="language">${[["en-IN", "English (India)"], ["en-US", "English (US)"], ["en-GB", "English (UK)"], ["hi-IN", "Hindi"], ["multi", "Hinglish / code-switched"]]
              .map(([v, l]) => `<option value="${v}" ${a.language === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        </div>
        <label class="field"><span>First message <small>— spoken as soon as the call connects. Supports {{variables}}. Leave empty to let the LLM open.</small></span>
          <input type="text" name="first_message" value="${esc(a.first_message)}"></label>
        <label class="field"><span>System prompt <small>— supports {{variables}}, filled per call</small></span>
          <textarea name="system_prompt" rows="12">${esc(a.system_prompt)}</textarea></label>
      </div>

      <div class="card">
        <h2>Models</h2>
        <div class="grid three">
          <label class="field"><span>LLM provider</span>
            <select name="llm_provider">${["google", "openai", "inference"].map((p) => `<option ${a.llm_provider === p ? "selected" : ""}>${p}</option>`).join("")}</select></label>
          <label class="field"><span>LLM model</span><input type="text" name="llm_model" list="llm-models" value="${esc(a.llm_model)}"></label>
          <label class="field"><span>Temperature</span><input type="number" step="0.1" min="0" max="2" name="llm_temperature" value="${esc(a.llm_temperature)}"></label>
          <label class="field"><span>TTS provider</span>
            <select name="tts_provider">${["sarvam", "deepgram", "inference"].map((p) => `<option ${a.tts_provider === p ? "selected" : ""}>${p}</option>`).join("")}</select></label>
          <label class="field"><span>TTS model</span><input type="text" name="tts_model" list="tts-models" value="${esc(a.tts_model)}"></label>
          <label class="field"><span>TTS voice</span><input type="text" name="tts_voice" list="tts-voices" value="${esc(a.tts_voice)}"></label>
          <label class="field"><span>STT model <small>(Deepgram)</small></span><input type="text" name="stt_model" value="${esc(a.stt_model)}"></label>
        </div>
        <datalist id="llm-models"></datalist><datalist id="tts-models"></datalist><datalist id="tts-voices"></datalist>
      </div>

      <div class="card">
        <h2>Call control</h2>
        <label class="field" style="max-width:260px"><span>Max call duration (seconds)</span>
          <input type="number" min="15" max="3600" name="max_call_duration_seconds" value="${esc(a.max_call_duration_seconds)}"></label>
        <h3>End-call conditions</h3>
        <p class="hint">The agent hangs up (via its built-in <span class="mono">end_call</span> tool) when its goal is met or any of these is true.</p>
        <div id="conditions"></div>
        <button type="button" class="btn small" id="add-cond">+ Add condition</button>
      </div>

      <div class="card">
        <h2>Extraction schema</h2>
        <p class="hint">Fields pulled out of every call after it ends, validated by type and saved as JSON.</p>
        <div id="fields"></div>
        <button type="button" class="btn small" id="add-field">+ Add field</button>
        <h3 style="margin-top:18px">Outcome labels</h3>
        <p class="hint">Post-call analysis assigns exactly one of these to each connected call.</p>
        <div id="outcomes"></div>
        <button type="button" class="btn small" id="add-outcome">+ Add outcome</button>
      </div>

      <div class="card">
        <h2>Webhook tools</h2>
        <p class="hint">Functions the agent can call mid-conversation. The platform POSTs <span class="mono">{"tool","arguments","call_id","agent_id"}</span> to the URL and hands the response to the LLM; failures and timeouts are returned to the LLM as <span class="mono">{"ok": false}</span> so it can recover.</p>
        <div id="tools"></div>
        <button type="button" class="btn small" id="add-tool">+ Add tool</button>
      </div>

      <div class="card">
        <h2>Voicemail</h2>
        <label class="checkbox" style="padding:0 0 10px"><input type="checkbox" name="voicemail_detection" ${a.voicemail_detection ? "checked" : ""}>
          Detect answering machines <small class="hint">— listens briefly after pickup before speaking; a long unbroken greeting is treated as voicemail</small></label>
        <div class="grid two">
          <label class="field"><span>When voicemail is detected</span>
            <select name="voicemail_action">${[["hang_up", "Hang up"], ["leave_message", "Leave a message"]].map(([v, l]) => `<option value="${v}" ${a.voicemail_action === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
          <label class="field"><span>Message <small>— spoken after the beep; supports {{variables}}</small></span>
            <input type="text" name="voicemail_message" value="${esc(a.voicemail_message)}"></label>
        </div>
      </div>

      <div class="card">
        <h2>Post-call webhook</h2>
        <div class="grid two">
          <label class="field"><span>URL <small>— receives call.completed once per call</small></span>
            <input type="url" name="post_call_webhook_url" value="${esc(a.post_call_webhook_url)}" placeholder="https://…"></label>
          <label class="field"><span>Signing secret <small>— HMAC-SHA256 in X-VAP-Signature</small></span>
            <input type="text" name="post_call_webhook_secret" value="${esc(a.post_call_webhook_secret)}"></label>
        </div>
        <p class="hint">Tip: <span class="mono">${esc(location.origin)}/demo/webhook-receiver</span> is a built-in receiver (see Webhook inbox).</p>
      </div>
    </form>`;

  const form = app.querySelector("#agent-form");
  const setHints = () => {
    const lp = form.llm_provider.value, tp = form.tts_provider.value;
    app.querySelector("#llm-models").innerHTML = (MODEL_HINTS[lp] || []).map((m) => `<option value="${m}">`).join("");
    app.querySelector("#tts-models").innerHTML = (VOICE_HINTS[tp]?.models || []).map((m) => `<option value="${m}">`).join("");
    const voices = tp === "sarvam" ? (window.VOICES?.sarvam?.[form.tts_model.value] || []) : VOICE_HINTS[tp]?.voices || [];
    app.querySelector("#tts-voices").innerHTML = voices.map((m) => `<option value="${m}">`).join("");
  };
  window.VOICES ??= await api("/api/voices").catch(() => ({}));
  form.tts_model.oninput = setHints;
  form.llm_provider.onchange = setHints;
  form.tts_provider.onchange = setHints;
  setHints();

  // dynamic lists
  const condEl = app.querySelector("#conditions");
  const addCond = (c = "") => {
    const d = document.createElement("div");
    d.className = "list-row cond-row";
    d.innerHTML = `<input type="text" data-k="cond" value="${esc(c)}" placeholder="e.g. The caller confirms a delivery date"><button type="button" class="btn small danger">Remove</button>`;
    d.querySelector("button").onclick = () => d.remove();
    condEl.appendChild(d);
  };
  a.end_call_conditions.forEach(addCond);
  app.querySelector("#add-cond").onclick = () => addCond();

  const fieldsEl = app.querySelector("#fields");
  const addField = (f = { name: "", type: "string", description: "", required: false, options: [], minimum: null, maximum: null }) => {
    const d = document.createElement("div");
    d.className = "list-row extract-row";
    d.innerHTML = `
      <input type="text" data-k="name" value="${esc(f.name)}" placeholder="field_name" class="mono">
      <select data-k="type">${["string", "string[]", "number", "integer", "boolean", "date", "enum"].map((t) => `<option ${f.type === t ? "selected" : ""}>${t}</option>`).join("")}</select>
      <div>
        <input type="text" data-k="options" value="${esc((f.options || []).join(", "))}" placeholder="enum options, comma-separated" ${f.type === "enum" ? "" : "hidden"}>
        <div class="row" data-k="bounds" ${["integer", "number"].includes(f.type) ? "" : "hidden"}>
          <input type="number" data-k="minimum" value="${esc(f.minimum ?? "")}" placeholder="min">
          <input type="number" data-k="maximum" value="${esc(f.maximum ?? "")}" placeholder="max">
        </div>
      </div>
      <input type="text" data-k="description" value="${esc(f.description)}" placeholder="What to extract">
      <label class="checkbox"><input type="checkbox" data-k="required" ${f.required ? "checked" : ""}> required</label>
      <button type="button" class="btn small danger">✕</button>`;
    const typeSel = d.querySelector('[data-k="type"]');
    typeSel.onchange = () => {
      d.querySelector('[data-k="options"]').hidden = typeSel.value !== "enum";
      d.querySelector('[data-k="bounds"]').hidden = !["integer", "number"].includes(typeSel.value);
    };
    d.querySelector("button").onclick = () => d.remove();
    fieldsEl.appendChild(d);
  };
  a.extraction_schema.forEach(addField);
  app.querySelector("#add-field").onclick = () => addField();

  const outEl = app.querySelector("#outcomes");
  const addOutcome = (o = { label: "", description: "" }) => {
    const d = document.createElement("div");
    d.className = "list-row outcome-row";
    d.innerHTML = `<input type="text" data-k="label" class="mono" value="${esc(o.label)}" placeholder="snake_case_label">
      <input type="text" data-k="description" value="${esc(o.description)}" placeholder="When this outcome applies">
      <button type="button" class="btn small danger">✕</button>`;
    d.querySelector("button").onclick = () => d.remove();
    outEl.appendChild(d);
  };
  a.outcome_labels.forEach(addOutcome);
  app.querySelector("#add-outcome").onclick = () => addOutcome();

  const toolsEl = app.querySelector("#tools");
  const addTool = (t = { name: "", description: "", url: "", method: "POST", timeout_seconds: 5, headers: {}, parameters: { type: "object", properties: {}, required: [] } }) => {
    const d = document.createElement("div");
    d.className = "tool-card";
    d.innerHTML = `
      <div class="grid three">
        <label class="field"><span>Name</span><input type="text" data-k="name" class="mono" value="${esc(t.name)}" placeholder="lookup_order"></label>
        <label class="field"><span>Method</span><select data-k="method">${["POST", "GET"].map((m) => `<option ${t.method === m ? "selected" : ""}>${m}</option>`).join("")}</select></label>
        <label class="field"><span>Timeout (s)</span><input type="number" step="0.5" min="0.5" max="30" data-k="timeout_seconds" value="${esc(t.timeout_seconds)}"></label>
      </div>
      <label class="field"><span>Description <small>— tells the LLM when to use it</small></span><input type="text" data-k="description" value="${esc(t.description)}"></label>
      <label class="field"><span>URL</span><input type="url" data-k="url" value="${esc(t.url)}" placeholder="https://…"></label>
      <label class="checkbox" style="padding:0 0 10px"><input type="checkbox" data-k="run_at_start" ${t.run_at_start ? "checked" : ""}>
        Call automatically at call start <small class="hint">— runs while the phone rings; result is given to the LLM, and its fields can be used as <span class="mono">{{${esc(t.name || "tool")}.field}}</span> in the first message</small></label>
      <label class="field" data-k="start-args-wrap" ${t.run_at_start ? "" : "hidden"}><span>Start arguments <small>— JSON; values may use {{variables}}</small></span>
        <textarea class="code" rows="3" data-k="start_arguments">${esc(JSON.stringify(t.start_arguments || {}, null, 2))}</textarea></label>
      <div class="grid two">
        <label class="field"><span>Parameters <small>— JSON Schema</small></span><textarea class="code" rows="8" data-k="parameters">${esc(JSON.stringify(t.parameters, null, 2))}</textarea></label>
        <label class="field"><span>Headers <small>— JSON object, optional</small></span><textarea class="code" rows="8" data-k="headers">${esc(JSON.stringify(t.headers || {}, null, 2))}</textarea></label>
      </div>
      <button type="button" class="btn small danger">Remove tool</button>`;
    d.querySelector("button.danger").onclick = () => d.remove();
    const startBox = d.querySelector('[data-k="run_at_start"]');
    startBox.onchange = () => (d.querySelector('[data-k="start-args-wrap"]').hidden = !startBox.checked);
    toolsEl.appendChild(d);
  };
  a.tools.forEach(addTool);
  app.querySelector("#add-tool").onclick = () => addTool();

  const collect = () => {
    const errs = [];
    const val = (k) => form.elements[k].value;
    const num = (v) => (String(v).trim() === "" ? null : Number(v));
    const parseJSON = (txt, label) => { try { return JSON.parse(txt || "{}"); } catch (e) { errs.push(`${label}: invalid JSON (${e.message})`); return {}; } };
    const body = {
      name: val("name").trim(), system_prompt: val("system_prompt"), first_message: val("first_message"),
      language: val("language"), llm_provider: val("llm_provider"), llm_model: val("llm_model").trim(),
      llm_temperature: parseFloat(val("llm_temperature")), tts_provider: val("tts_provider"),
      tts_model: val("tts_model").trim(), tts_voice: val("tts_voice").trim(), stt_model: val("stt_model").trim(),
      max_call_duration_seconds: parseInt(val("max_call_duration_seconds"), 10),
      post_call_webhook_url: val("post_call_webhook_url").trim() || null,
      post_call_webhook_secret: val("post_call_webhook_secret").trim() || null,
      voicemail_detection: form.elements.voicemail_detection.checked,
      voicemail_action: val("voicemail_action"),
      voicemail_message: val("voicemail_message"),
      end_call_conditions: [...condEl.querySelectorAll('[data-k="cond"]')].map((i) => i.value.trim()).filter(Boolean),
      extraction_schema: [...fieldsEl.children].map((r) => ({
        name: r.querySelector('[data-k="name"]').value.trim(),
        type: r.querySelector('[data-k="type"]').value,
        description: r.querySelector('[data-k="description"]').value.trim(),
        required: r.querySelector('[data-k="required"]').checked,
        options: r.querySelector('[data-k="options"]').value.split(",").map((s) => s.trim()).filter(Boolean),
        minimum: num(r.querySelector('[data-k="minimum"]').value),
        maximum: num(r.querySelector('[data-k="maximum"]').value),
      })),
      outcome_labels: [...outEl.children].map((r) => ({ label: r.querySelector('[data-k="label"]').value.trim(), description: r.querySelector('[data-k="description"]').value.trim() })),
      tools: [...toolsEl.children].map((r, i) => {
        const g = (k) => r.querySelector(`[data-k="${k}"]`).value;
        return {
          name: g("name").trim(), description: g("description").trim(), url: g("url").trim(), method: g("method"),
          timeout_seconds: parseFloat(g("timeout_seconds")),
          parameters: parseJSON(g("parameters"), `tools[${i}].parameters`),
          headers: parseJSON(g("headers"), `tools[${i}].headers`),
          run_at_start: r.querySelector('[data-k="run_at_start"]').checked,
          start_arguments: parseJSON(g("start_arguments"), `tools[${i}].start_arguments`),
        };
      }),
    };
    return { body, errs };
  };

  const save = async () => {
    const { body, errs } = collect();
    const errEl = app.querySelector("#f-errors");
    if (errs.length) { errEl.innerHTML = `<div class="errors"><b>Fix these:</b><ul>${errs.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></div>`; return null; }
    try {
      const saved = agentId ? await api(`/api/agents/${agentId}`, { method: "PUT", body }) : await api("/api/agents", { method: "POST", body });
      errEl.innerHTML = "";
      toast("Agent saved");
      return saved;
    } catch (e) {
      errEl.innerHTML = `<div class="errors"><b>Could not save:</b><ul>${e.message.split("\n").map((l) => `<li>${esc(l)}</li>`).join("")}</ul></div>`;
      window.scrollTo({ top: 0, behavior: "smooth" });
      return null;
    }
  };
  app.querySelector("#f-save").onclick = async () => {
    const saved = await save();
    if (saved && !agentId) location.hash = `#/agents/${saved.id}/edit`;
  };
  const callBtn = app.querySelector("#f-call");
  if (callBtn) callBtn.onclick = async () => { const saved = await save(); if (saved) startCallModal(saved); };
}

// ---------- call log ----------
async function callsPage() {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const [agents, calls] = await Promise.all([
    api("/api/agents"),
    api(`/api/calls?${new URLSearchParams([...params].filter(([, v]) => v).map(([k, v]) => [k === "agent" ? "agent_id" : k, v]))}`),
  ]);
  const agentFilter = params.get("agent") || "", statusFilter = params.get("status") || "";
  const done = calls.filter((c) => c.avg_v2v_ms != null);
  const avgAll = done.length ? done.reduce((s, c) => s + c.avg_v2v_ms, 0) / done.length : null;
  const completed = calls.filter((c) => c.status === "completed");

  app.innerHTML = `
    <div class="page-head"><h1>Call log</h1><div class="spacer"></div>
      <select id="f-agent" style="width:auto"><option value="">All agents</option>${agents.map((a) => `<option value="${a.id}" ${a.id === agentFilter ? "selected" : ""}>${esc(a.name)}</option>`).join("")}</select>
      <select id="f-status" style="width:auto"><option value="">All statuses</option>${["completed", "in_progress", "dialing", "no_answer", "busy", "rejected", "failed"].map((s) => `<option ${s === statusFilter ? "selected" : ""}>${s}</option>`).join("")}</select>
    </div>
    <div class="grid stats" style="margin-bottom:16px">
      <div class="stat"><div class="label">Calls shown</div><div class="value">${calls.length}</div></div>
      <div class="stat"><div class="label">Completed</div><div class="value">${completed.length}</div></div>
      <div class="stat"><div class="label">Avg talk time</div><div class="value">${fmtDur(completed.length ? completed.reduce((s, c) => s + (c.duration_seconds || 0), 0) / completed.length : null)}</div></div>
      <div class="stat"><div class="label">Avg voice-to-voice</div><div class="value">${fmtMs(avgAll)}</div></div>
    </div>
    <div class="card table-wrap">
      ${calls.length === 0 ? `<div class="empty">No calls yet. Start one from the Agents page.</div>` : `
      <table>
        <thead><tr><th>Started</th><th>Agent</th><th>To</th><th>Status</th><th class="num">Duration</th><th>Outcome</th><th class="num">Avg V2V</th></tr></thead>
        <tbody>${calls.map((c) => `
          <tr class="clickable" data-id="${c.id}">
            <td>${fmtTime(c.created_at)}</td>
            <td>${esc(c.agent_name)}</td>
            <td>${c.mode === "web" ? `<span class="badge">browser</span>` : `<span class="mono">${esc(c.to_number)}</span>`}</td>
            <td>${statusBadge(c.status)}</td>
            <td class="num">${fmtDur(c.duration_seconds)}</td>
            <td>${outcomeBadge(c.outcome, c.postcall_status)}</td>
            <td class="num"><span class="badge ${latencyClass(c.avg_v2v_ms)}">${fmtMs(c.avg_v2v_ms)}</span></td>
          </tr>`).join("")}</tbody>
      </table>`}
    </div>`;
  const nav = () => {
    const p = new URLSearchParams();
    if (app.querySelector("#f-agent").value) p.set("agent", app.querySelector("#f-agent").value);
    if (app.querySelector("#f-status").value) p.set("status", app.querySelector("#f-status").value);
    location.hash = `#/calls${p.toString() ? "?" + p : ""}`;
  };
  app.querySelector("#f-agent").onchange = nav;
  app.querySelector("#f-status").onchange = nav;
  app.querySelectorAll("tr[data-id]").forEach((tr) => (tr.onclick = () => (location.hash = `#/calls/${tr.dataset.id}`)));
  if (calls.some((c) => ["dialing", "in_progress", "queued"].includes(c.status) || c.postcall_status === "pending" || c.postcall_status === "processing"))
    pollTimer = setInterval(() => location.hash.startsWith("#/calls") && !location.hash.match(/#\/calls\/[\w-]+$/) && callsPage(), 4000);
}

// ---------- call detail ----------
async function callDetailPage(callId) {
  const c = await api(`/api/calls/${callId}`);
  const base = c.answered_at || c.turns[0]?.started_at || c.created_at;
  const live = ["dialing", "in_progress", "queued"].includes(c.status);

  // interleave tool calls with transcript turns by time
  const timeline = [
    ...c.turns.map((t) => ({ kind: t.speaker, at: t.started_at, t })),
    ...c.tool_invocations.map((i) => ({ kind: "tool", at: i.started_at, i })),
  ].sort((x, y) => (x.at || 0) - (y.at || 0));

  const agentTurns = c.turns.filter((t) => t.speaker === "agent");
  const maxLat = Math.max(1, ...agentTurns.map((t) => t.v2v_ms || 0));
  const fields = c.extraction_schema || [];
  const data = c.extracted_data || {};

  app.innerHTML = `
    <div class="page-head"><a href="#/calls">← Call log</a><h1>${esc(c.agent_name)}</h1>${statusBadge(c.status)}
      ${live ? `<span class="muted"><span class="live-dot"></span>live — refreshing</span>` : ""}
      <div class="spacer"></div>
      ${live ? "" : `<a class="btn" href="/api/calls/${esc(c.id)}/export" download title="Transcript, extraction, tool calls and webhook payload">Download JSON</a>`}
      ${live ? `<button class="btn danger" id="d-hangup">Hang up</button>` : ""}
      ${c.postcall_status === "failed" ? `<button class="btn" id="d-reprocess">Retry post-call processing</button>` : ""}
    </div>
    <div class="grid stats" style="margin-bottom:16px">
      <div class="stat"><div class="label">Outcome</div><div class="value" style="font-size:15px">${outcomeBadge(c.outcome, c.postcall_status)}</div></div>
      <div class="stat"><div class="label">Duration</div><div class="value">${fmtDur(c.duration_seconds)}</div></div>
      <div class="stat"><div class="label">Avg voice-to-voice</div><div class="value"><span class="badge ${latencyClass(c.avg_v2v_ms)}" style="font-size:18px">${fmtMs(c.avg_v2v_ms)}</span></div></div>
      <div class="stat"><div class="label">Worst turn</div><div class="value">${fmtMs(c.max_v2v_ms)}</div></div>
      <div class="stat"><div class="label">Turns</div><div class="value">${c.turns.length}</div></div>
    </div>

    <div class="grid two">
      <div class="card">
        <h2>Summary</h2>
        ${c.summary ? `<p style="margin:0">${esc(c.summary)}</p>` : `<p class="muted">${c.postcall_status === "failed" ? "Post-call processing failed: " + esc(c.postcall_error || "") : live ? "Available after the call ends." : "Processing…"}</p>`}
        <h3 style="margin-top:16px">Call</h3>
        <dl class="kv">
          <dt>Mode</dt><dd>${c.mode === "web" ? "Browser test call" : "Phone · " + esc(c.to_number)}</dd>
          <dt>Created</dt><dd>${fmtTime(c.created_at)}</dd>
          <dt>Answered</dt><dd>${fmtTime(c.answered_at)}</dd>
          <dt>Ended</dt><dd>${fmtTime(c.ended_at)}</dd>
          ${c.answered_by ? `<dt>Answered by</dt><dd>${c.answered_by === "machine" ? `<span class="badge warn">voicemail / machine</span>` : "person"}</dd>` : ""}
          ${c.campaign_contact_id ? `<dt>Campaign</dt><dd>contact #${c.campaign_contact_id}</dd>` : ""}
          <dt>End reason</dt><dd class="mono">${esc(c.end_reason || "—")}${c.sip_status_code ? ` (SIP ${c.sip_status_code})` : ""}</dd>
          ${Object.keys(c.variables || {}).length ? `<dt>Variables</dt><dd class="mono">${Object.entries(c.variables).map(([k, v]) => `${esc(k)}=${esc(v)}`).join("<br>")}</dd>` : ""}
          <dt>Call ID</dt><dd class="mono">${esc(c.id)}</dd>
        </dl>
      </div>
      <div class="card">
        <h2>Extracted data</h2>
        ${fields.length === 0 ? `<p class="muted">This agent has no extraction schema.</p>` : `
        <table><thead><tr><th>Field</th><th>Type</th><th>Value</th></tr></thead><tbody>
          ${fields.map((f) => `<tr><td class="mono">${esc(f.name)}${f.required ? " *" : ""}</td><td class="muted">${esc(f.type)}</td>
            <td>${data[f.name] == null ? `<span class="muted">null</span>` : esc(typeof data[f.name] === "object" ? JSON.stringify(data[f.name]) : String(data[f.name]))}</td></tr>`).join("")}
        </tbody></table>`}
        ${c.extraction_errors.length ? `<div class="errors" style="margin-top:12px"><b>Validation issues</b><ul>${c.extraction_errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></div>` : ""}
        ${c.extracted_data ? `<details style="margin-top:12px"><summary class="muted">JSON</summary><pre>${esc(JSON.stringify(c.extracted_data, null, 2))}</pre></details>` : ""}
      </div>
    </div>

    <div class="card">
      <h2>Transcript</h2>
      ${timeline.length === 0 ? `<p class="muted">${live ? "The transcript is saved when the call ends." : "No speech was recorded."}</p>` : `
      <div class="transcript">${timeline.map((e) => e.kind === "tool" ? `
        <div class="turn tool"><div class="ts">${offset(e.at, base)}</div><div class="bubble">
          <span class="speaker">Tool · ${esc(e.i.tool_name)}</span>${e.i.at_start ? `<span class="badge info">at call start</span> ` : ""}
          <span class="badge ${e.i.status === "ok" ? "ok" : "bad"}">${esc(e.i.status)}${e.i.http_status ? " " + e.i.http_status : ""}</span>
          <span class="meta">${fmtMs(e.i.latency_ms)}</span>
          <div class="mono muted" style="margin-top:4px">args ${esc(JSON.stringify(e.i.arguments))}</div>
          ${e.i.error ? `<div class="mono" style="color:var(--bad)">${esc(e.i.error)}</div>` : `<div class="mono muted">→ ${esc((e.i.response_excerpt || "").slice(0, 300))}</div>`}
        </div></div>` : `
        <div class="turn ${e.kind}"><div class="ts">${offset(e.at, base)}</div><div class="bubble">
          <span class="speaker">${e.kind === "agent" ? "Agent" : "User"}</span>${esc(e.t.text)}
          ${e.t.interrupted ? `<span class="badge warn">interrupted</span>` : ""}
          ${e.kind === "agent" && e.t.v2v_ms != null ? `<span class="meta">· ${fmtMs(e.t.v2v_ms)}</span>` : ""}
        </div></div>`).join("")}
      </div>`}
    </div>

    <div class="card table-wrap">
      <h2>Per-turn latency</h2>
      <div class="legend"><span><i style="background:var(--user);opacity:.55"></i>end-of-turn detection</span><span><i style="background:var(--agent)"></i>LLM first token</span><span><i style="background:var(--ok)"></i>TTS first audio</span>
        <span>Voice-to-voice = end of user speech → first agent audio (from LiveKit speech timestamps; check-ins the platform triggers itself are excluded)</span></div>
      ${agentTurns.length === 0 ? `<p class="muted">No agent turns yet.</p>` : `
      <table><thead><tr><th>#</th><th>Agent said</th><th class="num">End of turn</th><th class="num">STT</th><th class="num">LLM TTFT</th><th class="num">TTS TTFB</th><th class="num">Voice-to-voice</th><th>Breakdown</th></tr></thead><tbody>
      ${c.turns.map((t, i) => {
        if (t.speaker !== "agent") return "";
        const prevUser = [...c.turns.slice(0, i)].reverse().find((x) => x.speaker === "user");
        const eot = t.v2v_ms != null ? prevUser?.end_of_turn_ms : null;
        const w = (v) => (v ? Math.max(1, (v / maxLat) * 100) : 0);
        // A tool call between the user's turn and this reply adds its round
        // trip (and a second LLM pass) to this turn's voice-to-voice time.
        const usedTool = prevUser && c.tool_invocations.some((iv) => iv.started_at >= prevUser.started_at && iv.started_at <= t.started_at);
        return `<tr><td class="muted">${t.idx + 1}</td><td>${esc(t.text.slice(0, 70))}${t.text.length > 70 ? "…" : ""}${usedTool ? ` <span class="badge warn">incl. tool call</span>` : ""}</td>
          <td class="num">${fmtMs(eot)}</td><td class="num">${fmtMs(t.v2v_ms != null ? prevUser?.transcription_ms : null)}</td>
          <td class="num">${fmtMs(t.llm_ttft_ms)}</td><td class="num">${fmtMs(t.tts_ttfb_ms)}</td>
          <td class="num"><span class="badge ${latencyClass(t.v2v_ms)}">${t.v2v_ms == null ? (t.idx === 0 ? "greeting" : "—") : fmtMs(t.v2v_ms)}</span></td>
          <td>${t.v2v_ms != null ? `<div class="latbar" style="width:${w(t.v2v_ms)}%"><span class="eot" style="flex:${eot || 0}"></span><span class="llm" style="flex:${t.llm_ttft_ms || 0}"></span><span class="tts" style="flex:${t.tts_ttfb_ms || 0}"></span></div>` : ""}</td></tr>`;
      }).join("")}
      </tbody></table>`}
    </div>

    <div class="card table-wrap">
      <h2>Post-call webhook</h2>
      ${c.webhook_deliveries.length === 0 ? `<p class="muted">${c.postcall_status === "done" ? "No webhook URL configured on this agent." : "Sent once post-call processing finishes."}</p>` : `
      <table><thead><tr><th>Event</th><th>URL</th><th>Status</th><th class="num">Attempts</th><th>Last result</th><th></th></tr></thead><tbody>
      ${c.webhook_deliveries.map((w) => `<tr><td class="mono">${esc(w.event_type)}<div class="muted" style="font-size:11px">${esc(w.event_id)}</div></td>
        <td class="mono" style="word-break:break-all">${esc(w.url)}</td>
        <td><span class="badge ${w.status === "delivered" ? "ok" : w.status === "failed" ? "bad" : "info"}">${esc(w.status)}</span></td>
        <td class="num">${w.attempts}</td><td class="mono">${esc(w.last_error || (w.last_status_code ? "HTTP " + w.last_status_code : "—"))}</td>
        <td><button class="btn small" data-resend="${w.id}" title="Resends the same event id — receivers dedupe on Idempotency-Key">Resend</button></td></tr>`).join("")}
      </tbody></table>`}
    </div>`;

  app.querySelector("#d-hangup")?.addEventListener("click", async () => { await api(`/api/calls/${callId}/hangup`, { method: "POST" }); toast("Hanging up…"); });
  app.querySelector("#d-reprocess")?.addEventListener("click", async () => { await api(`/api/calls/${callId}/reprocess`, { method: "POST" }); toast("Reprocessing…"); setTimeout(() => callDetailPage(callId), 1500); });
  app.querySelectorAll("[data-resend]").forEach((b) => (b.onclick = async () => { await api(`/api/webhook-deliveries/${b.dataset.resend}/resend`, { method: "POST" }); toast("Resending same event"); setTimeout(() => callDetailPage(callId), 1500); }));

  const pending = live || ["pending", "processing"].includes(c.postcall_status) || c.webhook_deliveries.some((w) => w.status === "pending");
  if (pending) pollTimer = setInterval(() => location.hash === `#/calls/${callId}` && (clearInterval(pollTimer), callDetailPage(callId)), 3000);
}

// ---------- demo webhook inbox ----------
async function webhookInboxPage() {
  const events = await api("/demo/webhook-receiver");
  app.innerHTML = `
    <div class="page-head"><h1>Webhook inbox</h1><span class="muted">Built-in demo receiver at <span class="mono">${esc(location.origin)}/demo/webhook-receiver</span> (in-memory, last 50)</span></div>
    <div class="card">${events.length === 0 ? `<div class="empty">Nothing received yet. Set an agent's post-call webhook URL to the address above.</div>` :
      events.map((e) => `<div style="margin-bottom:14px"><div class="row" style="align-items:center;margin-bottom:6px">
        <b class="shrink">${fmtTime(e.received_at)}</b><span class="mono muted">${esc(e.event_id)}</span>
        <span class="shrink">${e.duplicate ? `<span class="badge warn">duplicate (deduped by receiver)</span>` : `<span class="badge ok">new event</span>`}</span>
        <a class="shrink" href="#/calls/${esc(e.body?.call?.id)}">call →</a></div>
        <details><summary class="muted">payload</summary><pre>${esc(JSON.stringify(e.body, null, 2))}</pre></details></div>`).join("")}</div>`;
  pollTimer = setInterval(() => location.hash === "#/webhooks" && webhookInboxPage(), 5000);
}

// ---------- campaigns ----------
async function campaignsPage() {
  const list = await api("/api/campaigns");
  app.innerHTML = `
    <div class="page-head"><h1>Campaigns</h1><div class="spacer"></div>
      <a class="btn primary" href="#/campaigns/new" ${window.TELEPHONY === false ? "title='Needs SIP_TRUNK_ID'" : ""}>+ New campaign</a></div>
    <div class="card table-wrap">${list.length === 0 ? `<div class="empty">No campaigns yet. Upload a CSV of contacts to call them with an agent.</div>` : `
      <table><thead><tr><th>Name</th><th>Agent</th><th>Status</th><th class="num">Contacts</th><th class="num">Pending</th><th class="num">Calling</th><th class="num">Done</th><th class="num">Exhausted</th><th>Created</th></tr></thead>
      <tbody>${list.map((c) => `<tr class="clickable" data-id="${c.id}"><td><b>${esc(c.name)}</b></td><td>${esc(c.agent_name)}</td><td>${statusBadge(c.status)}</td>
        <td class="num">${c.total}</td><td class="num">${c.counts.pending}</td><td class="num">${c.counts.calling}</td><td class="num">${c.counts.done}</td><td class="num">${c.counts.exhausted}</td><td>${fmtTime(c.created_at)}</td></tr>`).join("")}</tbody></table>`}
    </div>`;
  app.querySelectorAll("tr[data-id]").forEach((tr) => (tr.onclick = () => (location.hash = `#/campaigns/${tr.dataset.id}`)));
  if (list.some((c) => c.status === "running")) pollTimer = setInterval(() => location.hash === "#/campaigns" && campaignsPage(), 4000);
}

async function newCampaignPage() {
  const agents = await api("/api/agents");
  app.innerHTML = `
    <div class="page-head"><a href="#/campaigns">← Campaigns</a><h1>New campaign</h1></div>
    <div id="c-errors"></div>
    <div class="card">
      <div class="grid two">
        <label class="field"><span>Name</span><input type="text" id="c-name" placeholder="e.g. October references"></label>
        <label class="field"><span>Agent</span><select id="c-agent">${agents.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("")}</select></label>
      </div>
      <div class="grid three">
        <label class="field"><span>Concurrent calls</span><input type="number" id="c-conc" min="1" max="20" value="2"></label>
        <label class="field"><span>Max attempts per contact <small>— no answer / busy / voicemail count</small></span><input type="number" id="c-att" min="1" max="10" value="3"></label>
        <label class="field"><span>Retry after (minutes)</span><input type="number" id="c-delay" min="0" step="1" value="60"></label>
      </div>
      <p class="hint">Infrastructure failures (agent didn't join, trunk/carrier errors) are retried on a separate budget and never use up a contact's attempts.</p>
      <label class="field"><span>Contacts CSV <small>— a <span class="mono">phone_number</span> column plus one column per call variable</small></span>
        <input type="file" id="c-file" accept=".csv,text/csv"></label>
      <div id="c-vars" class="hint"></div>
      <textarea id="c-csv" class="code" rows="8" placeholder="phone_number,reference_name,candidate_id&#10;+919800000001,Dr. Meera Iyer,C-1001"></textarea>
      <div style="margin-top:12px"><button class="btn primary" id="c-start">Start campaign</button></div>
    </div>`;
  const showVars = () => {
    const a = agents.find((x) => x.id === app.querySelector("#c-agent").value);
    const vars = a ? placeholdersOf(a) : [];
    app.querySelector("#c-vars").innerHTML = vars.length ? `Columns this agent needs: <span class="mono">phone_number, ${vars.map(esc).join(", ")}</span>` : "This agent needs only a phone_number column.";
  };
  app.querySelector("#c-agent").onchange = showVars;
  showVars();
  app.querySelector("#c-file").onchange = async (e) => { const f = e.target.files[0]; if (f) app.querySelector("#c-csv").value = await f.text(); };
  app.querySelector("#c-start").onclick = async () => {
    const errEl = app.querySelector("#c-errors");
    try {
      const r = await api("/api/campaigns", { method: "POST", body: {
        name: app.querySelector("#c-name").value.trim() || "Untitled campaign",
        agent_id: app.querySelector("#c-agent").value,
        csv: app.querySelector("#c-csv").value,
        concurrency: parseInt(app.querySelector("#c-conc").value, 10),
        max_attempts: parseInt(app.querySelector("#c-att").value, 10),
        retry_delay_minutes: parseFloat(app.querySelector("#c-delay").value),
      } });
      if (r.rejected.length) sessionStorage.setItem(`vap-rejected-${r.id}`, JSON.stringify(r.rejected));
      toast(`Campaign started with ${r.accepted} contact(s)`);
      location.hash = `#/campaigns/${r.id}`;
    } catch (e) {
      errEl.innerHTML = `<div class="errors">${esc(e.message)}</div>`;
    }
  };
}

async function campaignDetailPage(id) {
  const c = await api(`/api/campaigns/${id}`);
  const rejected = JSON.parse(sessionStorage.getItem(`vap-rejected-${id}`) || "[]");
  app.innerHTML = `
    <div class="page-head"><a href="#/campaigns">← Campaigns</a><h1>${esc(c.name)}</h1>${statusBadge(c.status)}<div class="spacer"></div>
      ${c.status === "running" ? `<button class="btn" data-act="pause">Pause</button>` : c.status === "paused" ? `<button class="btn primary" data-act="resume">Resume</button>` : ""}</div>
    <div class="grid stats" style="margin-bottom:16px">
      <div class="stat"><div class="label">Agent</div><div class="value" style="font-size:15px">${esc(c.agent_name)}</div></div>
      <div class="stat"><div class="label">Contacts</div><div class="value">${c.total}</div></div>
      <div class="stat"><div class="label">Pending / calling</div><div class="value">${c.counts.pending} / ${c.counts.calling}</div></div>
      <div class="stat"><div class="label">Done</div><div class="value">${c.counts.done}</div></div>
      <div class="stat"><div class="label">Exhausted</div><div class="value">${c.counts.exhausted}</div></div>
      <div class="stat"><div class="label">Policy</div><div class="value" style="font-size:13px">${c.concurrency} at a time · ${c.max_attempts} attempts · retry ${c.retry_delay_minutes}m · infra ${c.max_infra_attempts}</div></div>
    </div>
    ${rejected.length ? `<div class="errors"><b>${rejected.length} CSV row(s) skipped</b><ul>${rejected.map((r) => `<li>line ${r.line}: ${esc(r.reason)}</li>`).join("")}</ul></div>` : ""}
    <div class="card table-wrap"><table>
      <thead><tr><th>Number</th><th>Variables</th><th>Status</th><th class="num">Attempts</th><th class="num">Infra retries</th><th>Last result</th><th>Outcome</th><th>Next attempt</th><th></th></tr></thead>
      <tbody>${c.contacts.map((x) => `<tr>
        <td class="mono">${esc(x.phone_number)}</td>
        <td class="mono" style="font-size:12px">${Object.entries(x.variables).map(([k, v]) => `${esc(k)}=${esc(v)}`).join("<br>")}</td>
        <td>${statusBadge(x.status)}</td><td class="num">${x.attempts}/${c.max_attempts}</td><td class="num">${x.infra_attempts}</td>
        <td>${x.last_result ? statusBadge(x.last_result) : "—"}</td><td>${x.last_outcome ? outcomeBadge(x.last_outcome, "done") : "—"}</td>
        <td>${x.next_attempt_at ? fmtTime(x.next_attempt_at) : "—"}</td>
        <td>${x.last_call_id ? `<a href="#/calls/${x.last_call_id}">call →</a>` : ""}</td></tr>`).join("")}</tbody>
    </table></div>`;
  app.querySelectorAll("[data-act]").forEach((b) => (b.onclick = async () => { await api(`/api/campaigns/${id}/${b.dataset.act}`, { method: "POST" }); campaignDetailPage(id); }));
  if (["running", "paused"].includes(c.status)) pollTimer = setInterval(() => location.hash === `#/campaigns/${id}` && (clearInterval(pollTimer), campaignDetailPage(id)), 3000);
}
