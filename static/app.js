/* Probe — client. Vanilla JS, no build step, no CDN. */

const $ = (id) => document.getElementById(id);

const state = {
  config: null,
  chat: [],
  chatBusy: false,
  singleBusy: false,
  load: {
    running: false,
    id: null,
    total: 0,
    completed: 0,
    ok: 0,
    fail: 0,
    tokens: 0,
    latencies: [],
    failures: 0,
    startedAt: 0,
    timer: null,
  },
};

let noticeTimer = null;

/* ---------- formatting ------------------------------------------------ */

const fmtMs = (v) =>
  v === null || v === undefined || Number.isNaN(v)
    ? "—"
    : v >= 1000
      ? (v / 1000).toFixed(2) + " s"
      : (v >= 100 ? Math.round(v) : v.toFixed(1)) + " ms";

const fmtNum = (v, digits = 0) =>
  v === null || v === undefined || Number.isNaN(v)
    ? "—"
    : Number(v).toLocaleString(undefined, {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      });

const fmtSec = (ms) =>
  ms >= 10000 ? (ms / 1000).toFixed(1) + " s" : (ms / 1000).toFixed(2) + " s";

function percentile(values, p) {
  if (!values.length) return null;
  const ordered = [...values].sort((a, b) => a - b);
  if (ordered.length === 1) return ordered[0];
  const rank = (p / 100) * (ordered.length - 1);
  const low = Math.floor(rank);
  const high = Math.min(low + 1, ordered.length - 1);
  return ordered[low] + (ordered[high] - ordered[low]) * (rank - low);
}

/* ---------- notices --------------------------------------------------- */

function flash(message, bad = false) {
  const el = $("notice");
  el.textContent = message;
  el.hidden = false;
  el.style.borderColor = bad ? "var(--orange)" : "var(--teal)";
  el.style.background = bad ? "#f9e6e0" : "#e3efeb";
  el.style.color = bad ? "#8c2c10" : "#0a4f46";
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => {
    el.hidden = true;
  }, 7000);
}

/* ---------- tabs ------------------------------------------------------ */

function setTab(name) {
  document.querySelectorAll(".rail-tab").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.hidden = panel.id !== "tab-" + name;
  });
}

document.querySelectorAll(".rail-tab").forEach((btn) => {
  btn.addEventListener("click", () => setTab(btn.dataset.tab));
});

/* ---------- config ---------------------------------------------------- */

async function loadConfig() {
  const dot = $("conn-dot");
  const label = $("conn-label");
  dot.className = "conn-dot";
  label.textContent = "Connecting…";
  $("refresh").disabled = true;

  try {
    const response = await fetch("/api/config");
    if (!response.ok) throw new Error(`Config request failed: HTTP ${response.status}`);
    const config = await response.json();
    state.config = config;

    $("rail-endpoint").textContent = config.base_url;
    $("rail-model").textContent = config.model || "not set";
    $("rail-key").textContent = config.api_key_set ? "set" : "none";
    $("model-chip").textContent = config.model || "no model";
    $("model-chip").title = config.model || "";

    const count = (config.models || []).length;
    if (count > 0) {
      dot.classList.add("is-up");
      label.textContent = `Connected · ${count} model${count === 1 ? "" : "s"} listed`;
    } else {
      dot.classList.add("is-down");
      label.textContent = "Endpoint set · model list unavailable";
    }
  } catch (error) {
    dot.classList.add("is-down");
    label.textContent = "Can't reach the app server";
    flash(error.message, true);
  } finally {
    $("refresh").disabled = false;
  }
}

$("refresh").addEventListener("click", loadConfig);

/* ---------- SSE over fetch ------------------------------------------- */

async function streamSSE(url, { method = "GET", body = null, signal } = {}, onEvent) {
  const response = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    signal,
  });

  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const payload = await response.json();
      message = payload.error || message;
    } catch {
      /* keep the status line */
    }
    throw new Error(message);
  }
  if (!response.body) throw new Error("This browser cannot read streamed responses.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let split;
    while ((split = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);

      let name = "message";
      const dataLines = [];
      for (const raw of block.split("\n")) {
        const line = raw.replace(/\r$/, "");
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (!dataLines.length) continue;
      let data;
      try {
        data = JSON.parse(dataLines.join("\n"));
      } catch {
        continue;
      }
      onEvent(name, data);
    }
  }
}

/* ---------- chat ------------------------------------------------------ */

const chatThread = $("chat-thread");
const emptyTemplate = $("chat-empty").cloneNode(true);

function scrollThread() {
  chatThread.scrollTop = chatThread.scrollHeight;
}

function addMessage(role, text) {
  const empty = chatThread.querySelector(".empty");
  if (empty) empty.remove();

  const article = document.createElement("div");
  article.className = "msg " + (role === "user" ? "is-user" : "is-assistant");

  const roleEl = document.createElement("span");
  roleEl.className = "msg-role";
  roleEl.textContent = role === "user" ? "You" : "Model";

  const body = document.createElement("div");
  body.className = "msg-body";
  const content = document.createElement("div");
  content.className = "msg-content";
  content.textContent = text;
  body.append(content);

  article.append(roleEl, body);
  chatThread.append(article);
  scrollThread();

  return { article, body, content };
}

function addMeta(parent, data) {
  const meta = document.createElement("div");
  meta.className = "msg-meta";
  const bits = [];
  if (data.ttft_ms !== null && data.ttft_ms !== undefined) bits.push(`TTFT ${fmtMs(data.ttft_ms)}`);
  bits.push(`Total ${fmtMs(data.latency_ms)}`);
  bits.push(`${fmtNum(data.prompt_tokens)} in / ${fmtNum(data.completion_tokens)} out`);
  if (data.tokens_per_sec !== null && data.tokens_per_sec !== undefined) {
    bits.push(`${fmtNum(data.tokens_per_sec, 1)} tok/s`);
  }
  for (const bit of bits) {
    const span = document.createElement("span");
    span.textContent = bit;
    meta.append(span);
  }
  parent.append(meta);
}

async function sendChat(event) {
  event.preventDefault();
  const input = $("chat-input");
  const text = input.value.trim();
  if (!text || state.chatBusy) return;

  state.chatBusy = true;
  $("chat-send").disabled = true;
  $("chat-send").textContent = "Sending…";
  input.value = "";

  const history = [...state.chat, { role: "user", content: text }];
  state.chat.push({ role: "user", content: text });
  addMessage("user", text);

  const assistant = addMessage("assistant", "");
  assistant.content.classList.add("cursor");

  const system = $("chat-system").value.trim();
  const messages = system ? [{ role: "system", content: system }, ...history] : history;

  let content = "";
  try {
    await streamSSE(
      "/api/chat/stream",
      {
        method: "POST",
        body: {
          messages,
          stream: true,
          max_tokens: Number($("chat-max").value) || 512,
          temperature: Number($("chat-temperature").value) || 0,
        },
      },
      (name, data) => {
        if (name === "delta") {
          content += data.text;
          assistant.content.textContent = content;
          scrollThread();
        } else if (name === "done") {
          assistant.content.classList.remove("cursor");
          if (!data.ok) {
            assistant.article.classList.add("is-error");
            assistant.content.textContent = data.error || "Request failed";
            addMeta(assistant.body, data);
            return;
          }
          if (!content) {
            content = data.content || "";
            assistant.content.textContent = content || "(empty reply)";
          }
          state.chat.push({ role: "assistant", content });
          addMeta(assistant.body, data);
          scrollThread();
        } else if (name === "error") {
          throw new Error(data.message);
        }
      }
    );
  } catch (error) {
    assistant.content.classList.remove("cursor");
    assistant.article.classList.add("is-error");
    assistant.content.textContent = error.message;
    flash(error.message, true);
  } finally {
    state.chatBusy = false;
    $("chat-send").disabled = false;
    $("chat-send").textContent = "Send";
    input.focus();
  }
}

$("chat-form").addEventListener("submit", sendChat);

$("chat-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("chat-form").requestSubmit();
  }
});

$("chat-clear").addEventListener("click", () => {
  if (state.chatBusy) return;
  state.chat = [];
  chatThread.replaceChildren(emptyTemplate.cloneNode(true));
});

/* ---------- single call ----------------------------------------------- */

const singleOutput = $("single-output");

function resetSingleReadout() {
  for (const id of ["single-ttft", "single-latency", "single-tokens", "single-tps"]) {
    const el = $(id);
    el.textContent = "—";
    el.classList.remove("is-good", "is-bad");
  }
}

async function sendSingle(event) {
  event.preventDefault();
  if (state.singleBusy) return;

  const prompt = $("single-prompt").value.trim();
  if (!prompt) {
    flash("Write a prompt first.", true);
    return;
  }

  state.singleBusy = true;
  $("single-send").disabled = true;
  $("single-send").textContent = "Waiting…";
  resetSingleReadout();
  $("single-raw-wrap").hidden = true;
  singleOutput.replaceChildren();
  const placeholder = document.createElement("p");
  placeholder.className = "placeholder";
  placeholder.textContent = "Waiting for the server…";
  singleOutput.append(placeholder);

  const stream = $("single-stream").checked;
  let content = "";

  try {
    await streamSSE(
      "/api/chat/stream",
      {
        method: "POST",
        body: {
          messages: [{ role: "user", content: prompt }],
          stream,
          max_tokens: Number($("single-max").value) || 256,
          temperature: Number($("single-temperature").value) || 0,
        },
      },
      (name, data) => {
        if (name === "delta") {
          content += data.text;
          singleOutput.textContent = content;
        } else if (name === "done") {
          if (!data.ok) {
            singleOutput.textContent = data.error || "Request failed";
            singleOutput.classList.add("is-bad");
            $("single-latency").textContent = fmtMs(data.latency_ms);
            $("single-latency").classList.add("is-bad");
            $("single-raw").textContent = JSON.stringify(data, null, 2);
            $("single-raw-wrap").hidden = false;
            return;
          }
          singleOutput.classList.remove("is-bad");
          content = content || data.content || "";
          singleOutput.textContent = content || "(empty response)";
          $("single-ttft").textContent = fmtMs(data.ttft_ms);
          $("single-ttft").classList.add("is-good");
          $("single-latency").textContent = fmtMs(data.latency_ms);
          $("single-tokens").textContent = `${fmtNum(data.prompt_tokens)} / ${fmtNum(data.completion_tokens)}`;
          $("single-tps").textContent =
            data.tokens_per_sec === null ? "—" : `${fmtNum(data.tokens_per_sec, 1)} tok/s`;
          $("single-tps").classList.add("is-good");
          $("single-raw").textContent = JSON.stringify({ ...data, content }, null, 2);
          $("single-raw-wrap").hidden = false;
        } else if (name === "error") {
          throw new Error(data.message);
        }
      }
    );
  } catch (error) {
    singleOutput.classList.add("is-bad");
    singleOutput.textContent = error.message;
    flash(error.message, true);
  } finally {
    state.singleBusy = false;
    $("single-send").disabled = false;
    $("single-send").textContent = "Send request";
  }
}

$("single-form").addEventListener("submit", sendSingle);

/* ---------- load test ------------------------------------------------- */

const loadReadout = {
  wall: $("live-wall"),
  done: $("live-done"),
  rps: $("live-rps"),
  tps: $("live-tps"),
  p95: $("live-p95"),
};

const outputsGrid = $("load-outputs");
const outputsEmpty = $("outputs-empty");
const outputBlocks = new Map();

function resetOutputs() {
  outputBlocks.clear();
  outputsGrid.replaceChildren(outputsEmpty);
  outputsEmpty.hidden = false;
  $("outputs-count").textContent = "0 requests";
  $("load-outputs-wrap").hidden = true;
}

function updateOutputsCount() {
  const count = outputBlocks.size;
  $("outputs-count").textContent = `${count} request${count === 1 ? "" : "s"}`;
}

function ensureOutputBlock(index) {
  const existing = outputBlocks.get(index);
  if (existing) return existing;

  outputsEmpty.hidden = true;
  const article = document.createElement("article");
  article.className = "out-block is-streaming";

  const head = document.createElement("div");
  head.className = "out-head";
  const indexEl = document.createElement("span");
  indexEl.className = "out-index";
  indexEl.textContent = "#" + String(index + 1).padStart(2, "0");
  const stateEl = document.createElement("span");
  stateEl.className = "out-state";
  stateEl.textContent = "streaming";
  const timeEl = document.createElement("span");
  timeEl.className = "out-time";
  head.append(indexEl, stateEl, timeEl);

  const text = document.createElement("div");
  text.className = "out-text";
  article.append(head, text);

  outputsGrid.append(article);
  const block = { article, stateEl, timeEl, text };
  outputBlocks.set(index, block);
  updateOutputsCount();
  outputsGrid.scrollTop = outputsGrid.scrollHeight;
  return block;
}

function appendOutputDelta(index, chunk) {
  const block = ensureOutputBlock(index);
  block.text.textContent += chunk;
}

function settleOutputBlock(result) {
  const block = ensureOutputBlock(result.index);
  block.article.classList.remove("is-streaming");
  block.article.classList.add(result.ok ? "is-ok" : "is-fail");
  block.stateEl.textContent = result.ok ? "ok" : "failed";
  block.timeEl.textContent = fmtMs(result.latency_ms);
  if (!block.text.textContent) {
    block.text.textContent = result.content || result.error || "(empty response)";
  }
}


function setLoadRunning(running) {
  $("load-run").disabled = running;
  $("load-run").textContent = running ? "Running…" : "Run test";
  $("load-stop").hidden = !running;
  $("load-stop").disabled = false;
  $("load-stop").textContent = "Stop";
  document.querySelectorAll("#load-form input, #load-form textarea, #load-form button").forEach((el) => {
    if (el.id !== "load-run") el.disabled = running;
  });
}

function resetLoadView() {
  $("load-rows").replaceChildren();
  $("strip-ticks").replaceChildren();
  $("strip-empty").hidden = false;
  $("strip-p95").hidden = true;
  $("strip-legend").hidden = false;
  $("load-summary").hidden = true;
  $("load-summary").replaceChildren();
  $("load-empty").hidden = false;
  resetOutputs();
  loadReadout.wall.textContent = "—";
  loadReadout.done.textContent = `0 / ${state.load.total || 0}`;
  loadReadout.rps.textContent = "—";
  loadReadout.tps.textContent = "—";
  loadReadout.p95.textContent = "—";
}

function addRow(result) {
  $("load-empty").hidden = true;
  const row = document.createElement("tr");

  const index = document.createElement("td");
  index.textContent = result.index + 1;

  const status = document.createElement("td");
  const badge = document.createElement("span");
  badge.className = "badge " + (result.ok ? "ok" : "fail");
  badge.textContent = result.ok ? "ok" : "fail";
  status.append(badge);

  const ttft = document.createElement("td");
  ttft.textContent = fmtMs(result.ttft_ms);
  const latency = document.createElement("td");
  latency.textContent = fmtMs(result.latency_ms);
  const tokens = document.createElement("td");
  tokens.textContent = fmtNum(result.completion_tokens);
  const speed = document.createElement("td");
  speed.textContent = result.tokens_per_sec === null ? "—" : fmtNum(result.tokens_per_sec, 1);
  const error = document.createElement("td");
  error.className = "err";
  error.textContent = result.error || "";
  if (result.error) error.title = result.error;

  row.append(index, status, ttft, latency, tokens, speed, error);
  $("load-rows").append(row);
}

function addTick(result) {
  const tick = document.createElement("div");
  tick.className = "tick" + (result.ok ? "" : " is-fail");
  tick.dataset.latency = String(result.latency_ms);
  tick.title = `#${result.index + 1} · ${fmtMs(result.latency_ms)}${result.ok ? "" : " · failed"}`;
  $("strip-ticks").append(tick);
  $("strip-empty").hidden = true;
}

function rescaleStrip() {
  const ticks = [...$("strip-ticks").children];
  if (!ticks.length) return;
  const peak = Math.max(...ticks.map((t) => Number(t.dataset.latency)), 20);
  for (const tick of ticks) {
    const value = Number(tick.dataset.latency);
    tick.style.height = Math.max(2, (value / peak) * 100) + "%";
  }

  const p95 = percentile(state.load.latencies, 95);
  if (p95 !== null) {
    $("strip-p95").hidden = false;
    $("strip-p95").style.top = Math.max(0, 100 - (p95 / peak) * 100) + "%";
    $("strip-p95-label").textContent = `p95 ${fmtMs(p95)}`;
  }
}

function updateLive() {
  const { completed, total, ok, tokens, startedAt, latencies } = state.load;
  const elapsed = Math.max(1, performance.now() - startedAt);
  loadReadout.wall.textContent = fmtSec(elapsed);
  loadReadout.done.textContent = `${completed} / ${total}`;
  loadReadout.rps.textContent = fmtNum((ok / elapsed) * 1000, 2);
  loadReadout.tps.textContent = fmtNum((tokens / elapsed) * 1000, 1);
  const p95 = percentile(latencies, 95);
  loadReadout.p95.textContent = p95 === null ? "—" : fmtMs(p95);
}

function renderSummary(summary) {
  const stats = [
    ["Requests", fmtNum(summary.total)],
    ["Failed", fmtNum(summary.failed), summary.failed > 0],
    ["Wall time", fmtMs(summary.wall_ms)],
    ["Requests / s", fmtNum(summary.throughput_rps, 2)],
    ["Output tok / s", fmtNum(summary.output_tps, 1)],
    ["Output tokens", fmtNum(summary.output_tokens)],
    ["Latency p50", summary.latency ? fmtMs(summary.latency.p50) : "—"],
    ["Latency p95", summary.latency ? fmtMs(summary.latency.p95) : "—"],
    ["Latency p99", summary.latency ? fmtMs(summary.latency.p99) : "—"],
    ["TTFT p50", summary.ttft ? fmtMs(summary.ttft.p50) : "—"],
    ["TTFT p95", summary.ttft ? fmtMs(summary.ttft.p95) : "—"],
    ["Slowest", summary.latency ? fmtMs(summary.latency.max) : "—"],
  ];

  const grid = $("load-summary");
  grid.replaceChildren();
  for (const [label, value, bad] of stats) {
    const stat = document.createElement("div");
    stat.className = "stat";
    const labelEl = document.createElement("span");
    labelEl.className = "stat-label";
    labelEl.textContent = label;
    const valueEl = document.createElement("span");
    valueEl.className = "stat-value" + (bad ? " is-bad" : "");
    valueEl.textContent = value;
    stat.append(labelEl, valueEl);
    grid.append(stat);
  }
  grid.hidden = false;
}

function finishLoad(summary) {
  clearInterval(state.load.timer);
  state.load.timer = null;
  loadReadout.wall.textContent = fmtMs(summary.wall_ms);
  loadReadout.done.textContent = `${summary.total} / ${state.load.total}`;
  loadReadout.rps.textContent = fmtNum(summary.throughput_rps, 2);
  loadReadout.tps.textContent = fmtNum(summary.output_tps, 1);
  loadReadout.p95.textContent = summary.latency ? fmtMs(summary.latency.p95) : "—";
  const p95 = summary.latency ? summary.latency.p95 : null;
  if (p95 !== null) {
    const peak = Math.max(...state.load.latencies, p95, 20);
    $("strip-p95").hidden = false;
    $("strip-p95").style.top = Math.max(0, 100 - (p95 / peak) * 100) + "%";
    $("strip-p95-label").textContent = `p95 ${fmtMs(p95)}`;
  }
  renderSummary(summary);
  if (summary.failed > 0) {
    flash(`${summary.failed} of ${summary.total} requests failed. See the table for details.`, true);
  }
}

async function startLoad(event) {
  event.preventDefault();
  if (state.load.running) return;

  const prompt = $("load-prompt").value.trim();
  if (!prompt) {
    flash("Write a prompt first.", true);
    return;
  }

  const count = Math.max(1, Math.min(1000, Number($("load-count").value) || 1));
  const concurrency = Math.max(1, Math.min(200, Number($("load-concurrency").value) || 1));

  state.load = {
    running: true,
    id: null,
    total: count,
    completed: 0,
    ok: 0,
    fail: 0,
    tokens: 0,
    latencies: [],
    failures: 0,
    startedAt: performance.now(),
    timer: null,
  };

  resetLoadView();
  setLoadRunning(true);
  $("load-outputs-wrap").hidden = false;

  try {
    const response = await fetch("/api/loadtest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt,
        count,
        concurrency,
        stream: $("load-stream").checked,
        max_tokens: Number($("load-max").value) || 128,
        temperature: Number($("load-temperature").value) || 0,
        system_prompt: $("load-system").value.trim(),
      }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || `Could not start test (HTTP ${response.status})`);
    }
    const { id } = await response.json();
    state.load.id = id;
    state.load.startedAt = performance.now();
    state.load.timer = setInterval(updateLive, 120);

    await streamSSE(`/api/loadtest/${id}/stream`, {}, (name, data) => {
      if (name === "start") {
        loadReadout.done.textContent = `0 / ${data.count}`;
      } else if (name === "delta") {
        appendOutputDelta(data.index, data.text);
      } else if (name === "result") {
        state.load.completed += 1;
        if (data.ok) {
          state.load.ok += 1;
          state.load.latencies.push(data.latency_ms);
        } else {
          state.load.fail += 1;
        }
        state.load.tokens += data.completion_tokens || 0;
        addRow(data);
        addTick(data);
        settleOutputBlock(data);
        rescaleStrip();
        updateLive();
      } else if (name === "done") {
        finishLoad(data);
      } else if (name === "error") {
        throw new Error(data.message);
      }
    });
  } catch (error) {
    flash(error.message, true);
    clearInterval(state.load.timer);
  } finally {
    state.load.running = false;
    clearInterval(state.load.timer);
    state.load.timer = null;
    setLoadRunning(false);
  }
}

$("load-form").addEventListener("submit", startLoad);

$("load-stop").addEventListener("click", async () => {
  if (!state.load.id) return;
  $("load-stop").disabled = true;
  $("load-stop").textContent = "Stopping…";
  try {
    await fetch(`/api/loadtest/${state.load.id}/stop`, { method: "POST" });
  } catch (error) {
    flash(error.message, true);
  }
});

/* ---------- boot ------------------------------------------------------ */

setTab("chat");
resetSingleReadout();
loadConfig();
