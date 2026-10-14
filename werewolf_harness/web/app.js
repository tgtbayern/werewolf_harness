/* The page is a thin renderer over two calls: GET the seat's state, POST an action.
 *
 * It never decides anything. What may be submitted comes from the server as the
 * turn's tool list -- the same object the environment renders into a model's
 * prompt -- so the buttons a person sees and the check the environment runs are
 * the same list. A submission carries the turn's sequence number, so a retry or
 * a double click lands once.
 */
const $ = (s, r = document) => r.querySelector(s);
const el = (tag, cls, txt) => { const n = document.createElement(tag); if (cls) n.className = cls; if (txt != null) n.textContent = txt; return n; };
/* Who you are is a guest token (localStorage); which seat you hold in a game is a
 * seat token the server hands out on /join. A seat's private view and its moves
 * need the seat token -- the server no longer trusts `?seat=N` on its own. */
const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* private mode: keep it in memory */ } },
};
const seatKey = (g, s) => `wh.seat.${g}.${s}`;
const authFor = () => (state.game && state.seat && store.get(seatKey(state.game, state.seat))) || store.get("wh.user");
const api = async (path, opts) => {
  const headers = { "Content-Type": "application/json" };
  const tok = (opts && opts.token) || authFor();
  if (tok) headers.Authorization = `Bearer ${tok}`;
  const r = await fetch(path, { headers, ...opts });
  return r.json();
};

async function ensureUser() {
  if (store.get("wh.user")) return;
  const r = await fetch("/api/auth/guest", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  const j = await r.json();
  if (j.token) store.set("wh.user", j.token);
}

async function ensureSeat() {
  if (!state.seat || store.get(seatKey(state.game, state.seat))) return;
  const r = await api(`/api/games/${state.game}/join`, { method: "POST", token: store.get("wh.user"),
    body: JSON.stringify({ seat: state.seat }) });
  if (r.seat_token) store.set(seatKey(state.game, state.seat), r.seat_token);
  else { state.notice = r.detail || "这个座位已经有人了，你在观战。"; state.seat = null; }
}

const state = {
  meta: null, games: [], filter: "all",
  form: { xray: false, variant: "8p", models: ["deepseek-v4-flash", "deepseek-v4-pro", "glm-5.3", "kimi-k3",
            "claude-sonnet-5", "claude-opus-5"], guard: "L1+L2+L3", seat: 3, clock: 300 },
  game: null, seat: null, picked: null, draft: "", tool: null, args: {},
  es: null, ws: null, conn: "—", sending: false, feedTab: "say", log: null, lastEv: -1, acks: {}, retry: 0,
  reviewSeen: false, sayRound: null, voteRound: null,
};

/* ─────────────────────────────────────────────── routing */
function route() {
  const m = location.hash.match(/^#\/g\/([^/?]+)(?:\?seat=(\d+))?/);
  if (m) { state.game = m[1]; state.seat = m[2] ? +m[2] : null; openTable(); }
  else { state.game = null; state.seat = null; closeLive(); openLobby(); }
}
window.addEventListener("hashchange", route);

/* ─────────────────────────────────────────────── lobby */
const FILTERS = [["all", "全部"], ["running", "进行中"], ["waiting", "等待入座"], ["over", "已结束"]];
const CLOCKS = [[120, "2 分钟"], [300, "5 分钟"], [600, "10 分钟"], [0, "不限"]];
const GUARD_WHY = {
  "无": "不装防护：别人的发言原样进入 AI 的上下文。作对照组用。",
  "L1": "围栏：别人的话被标成数据，不过滤内容。",
  "L1+L2": "围栏 + 过滤：带指令的句子整句删掉，再围栏。",
  "L1+L2+L3": "围栏 + 过滤 + 出口校验：AI 的票和它自己刚说的话不一致会被退回重投。",
};
const SHORT = {
  "gemini-3.8-flash": "gemini", "gpt-5.6-terra": "terra", "gpt-5.6-sol": "sol",
  "claude-sonnet-5": "sonnet", "claude-opus-5": "opus", mock: "mock", script: "script",
  "claude-cli:sonnet": "cli-sonnet", "claude-cli:haiku": "cli-haiku", "claude-cli:opus": "cli-opus", "claude-cli": "cli",
  "deepseek-v4-flash": "ds-flash", "deepseek-v4-pro": "ds-pro",
  "kimi-k3": "kimi-k3", "kimi-k2.6": "kimi-k2.6", "glm-5.3": "glm-5.3",
};
// 12 次实测（2026-09-03，最大的那个 prompt）。数字在 providers/__init__.py 里，
// 这里只是同一份测量结果的人话版本。
const MODEL_GROUPS = [
  ["cli", "本机 Claude Code · claude -p（不要 key，用 CLI 登录的账号）"],
  ["relay", "API · 中转站"],
  ["direct", "API · 厂商直连"],
  ["local", "本地模拟（不调用模型，测试用）"],
];
function modelGroup(m, meta) {
  if (m === "claude-cli" || m.startsWith("claude-cli:")) return "cli";
  if (m === "mock" || m === "script" || m.startsWith("sim")) return "local";
  if (meta.profiles && meta.profiles[m]) return "relay";
  return "direct";
}
const MODEL_WHY = {
  "claude-cli:sonnet": "本机 Claude Code（claude -p），用 CLI 登录的账号，不要 API key。每次调用多 1–2 秒起进程。",
  "claude-cli:haiku": "本机 Claude Code 的 haiku：最快、最省额度。",
  "claude-cli:opus": "本机 Claude Code 的 opus：最强，也最吃订阅额度。",
  "deepseek-v4-flash": "1 秒出手，最便宜。思考模式关掉了——ReAct 循环本来就在逼它一步步想。",
  "deepseek-v4-pro": "1.4 秒，同一家的旗舰。想看便宜档和贵档差多少就摆一起。",
  "glm-5.3": "4 秒。始终思考关不掉，已把 reasoning_effort 压到 low 控成本。",
  "kimi-k3": "3.7 秒。只吃 temperature 0.6，思考已关。",
  "kimi-k2.6": "1.3 秒，Kimi 的便宜档。",
  "claude-sonnet-5": "中位 7 秒左右（走 JSON 工具协议，网关会丢它的原生工具调用）。",
  "claude-opus-5": "中位 6.8 秒，最慢 10.7 秒。想得最细，速度也够。",
  mock: "离线脚本，秒回，不花钱，也不会真的推理。只用来试界面。",
  script: "随机合法动作，最快，用来看规则怎么走。",
};
const MODEL_OFF = new Set();

async function openLobby() {
  $("#lobby").hidden = false; $("#table").hidden = true;
  $("#nav-lobby").classList.add("on"); $("#nav-table").hidden = true;
  closeLive();
  if (!state.meta) {
    state.meta = await api("/api/meta");
    // Keep only models this server can fill right now; if none of the defaults are,
    // start from the local Claude Code CLI when it is there.
    const ok = new Set(state.meta.models), f = state.form;
    f.models = f.models.filter((m) => ok.has(m));
    if (!f.models.length) {
      const pick = ["claude-cli:haiku", "claude-cli:sonnet"].find((m) => ok.has(m))
        || state.meta.models.find((m) => !["mock", "script"].includes(m) && !m.startsWith("sim")) || "mock";
      f.models = [pick];
    }
  }
  renderForm();
  await refreshGames();
  clearInterval(window._poll);
  window._poll = setInterval(refreshGames, 2500);
}

async function refreshGames() {
  if (state.game) return;
  const r = await api("/api/games");
  state.games = r.games || [];
  renderGames();
}

function renderGames() {
  const box = $("#games"); box.textContent = "";
  const shown = state.games.filter((g) => state.filter === "all" || g.state === state.filter);
  $("#lede").textContent = `${state.games.length} 桌 · ${state.games.filter((g) => g.state === "waiting").length} 桌在等人`;
  $("#filters").textContent = "";
  FILTERS.forEach(([k, label]) => {
    const b = el("button", "chip" + (state.filter === k ? " on" : ""), label);
    b.onclick = () => { state.filter = k; renderGames(); };
    $("#filters").append(b);
  });
  if (!shown.length) { box.append(el("div", "empty", "还没有桌子。右边开一桌。")); return; }
  for (const g of shown) {
    const row = el("div", "lrow");
    row.append(el("span", "mono", g.id));
    const cfg = el("span");
    const name = el("span", null, g.model === "混合" ? `混合 ${(g.models || []).length} 模型` : g.model);
    if (g.xray) name.append(el("span", "xraytag", "测试模式"));
    cfg.append(name,
               el("span", "cfg", (g.model === "混合" ? (g.models || []).map((m) => SHORT[m] || m).join("、") + " · " : "")
                  + `${g.variant} · ${g.guard} · ${g.playbook}`));
    row.append(cfg);
    row.append(el("span", "mono tiny", progressOf(g)));
    const seats = el("span");
    const taken = g.seats - g.human_seats.filter((s) => true).length + g.human_seats.length;
    seats.append(el("span", "mono tiny", `${g.seats} 人 · 你的位 ${g.human_seats.join("、") || "无"}`));
    const bar = el("span", "bar"); const i = el("i"); i.style.width = `${(g.alive.length / g.seats) * 100}%`; bar.append(i); seats.append(bar);
    row.append(seats);
    const h = el("span");
    const ok = g.health.usable;
    const dot = el("span", "dot"); dot.style.background = !g.health.turns ? "#a89a82" : ok ? "var(--good)" : "var(--live)";
    h.append(dot, el("span", "mono tiny", g.health.turns ? `兜底 ${(g.health.forced_rate * 100).toFixed(1)}%` : "未开始"));
    row.append(h);
    const a = el("button", "chip", g.human_seats.length ? "进桌" : "观战");
    a.onclick = () => { location.hash = `#/g/${g.id}` + (g.human_seats.length ? `?seat=${g.human_seats[0]}` : ""); };
    row.append(a);
    box.append(row);
  }
}

const progressOf = (g) => g.state === "over" ? `已结束 · ${g.outcome ? (g.outcome.winner === "wolf" || g.outcome.winner === "werewolf" ? "狼胜" : "村民胜") : "—"}`
  : g.state === "waiting" ? "等待开局"
  : g.state === "aborted" ? "已作废" : g.state === "crashed" ? "崩了"
  : `第 ${g.round} ${g.phase.startsWith("night") || g.phase.startsWith("pack") ? "夜" : "天"}`;

function renderForm() {
  const f = state.form, meta = state.meta;
  const chips = (id, items, key, onPick) => {
    const box = $(id); box.textContent = "";
    for (const [k, label, disabled] of items) {
      const b = el("button", "chip" + (f[key] === k ? " on" : ""), label);
      if (disabled) b.disabled = true;
      b.onclick = () => { f[key] = k; onPick && onPick(); renderForm(); };
      box.append(b);
    }
  };
  chips("#f-variant", Object.keys(meta.variants).map((v) => [v, `${meta.variants[v].seats} 人`]), "variant",
        () => { f.seat = Math.min(f.seat, meta.variants[f.variant].seats); });
  // models is a SET: every one you leave on gets dealt to some seat.
  // Grouped by how the seat reaches its model, so a claude -p seat and an API seat never look alike.
  const mbox = $("#f-model"); mbox.textContent = "";
  for (const [gk, glabel] of MODEL_GROUPS) {
    const ms = meta.models.filter((m) => modelGroup(m, meta) === gk);
    if (!ms.length) continue;
    const row = el("div", "mgroup");
    row.append(el("span", "mgroup-label", glabel));
    const box = el("div", "chips");
    for (const m of ms) {
      const on = f.models.includes(m);
      const b = el("button", "chip" + (on ? " on" : "") + (gk === "cli" ? " cli" : ""),
                   gk === "cli" ? (m.split(":")[1] || "默认") : (SHORT[m] || m));
      b.title = `${m} · ${glabel}`;
      b.onclick = () => {
        if (on && f.models.length > 1) f.models = f.models.filter((x) => x !== m);
        else if (!on) f.models = [...f.models, m];
        renderForm();
      };
      box.append(b);
    }
    row.append(box);
    mbox.append(row);
  }
  chips("#f-guard", meta.guards.map((g) => [g, g]), "guard");
  const n = meta.variants[f.variant].seats;
  chips("#f-seat", Array.from({ length: n }, (_, i) => [i + 1, `${i + 1}`]), "seat");
  chips("#f-clock", CLOCKS, "clock");
  chips("#f-xray", [[false, "正常"], [true, "测试模式"]], "xray");
  $("#xray-hint").textContent = f.xray
    ? "看得到所有人的身份，和每个 AI 每一步的思考、调用了什么工具、被防护挡下过什么。自己玩会剧透，用来调试和演示。"
    : "只看得到你这个座位该知道的。出局之后自动翻牌，结束时全场翻牌。";
  $("#model-hint").textContent = f.models.length === 1
    ? (MODEL_WHY[f.models[0]] || "")
    : `${f.models.length} 个模型混着坐：按 seed 洗牌后轮流发到各个座位，谁坐哪不固定。`;
  $("#guard-hint").textContent = GUARD_WHY[f.guard] || "";
  $("#estimate").textContent =
    `${n} 个座位 · ${n - 1} 个 AI（${f.models.map((m) => SHORT[m] || m).join("、")}）· 你坐 ${f.seat} 号\n`
    + `预计 ${n === 8 ? "60–90" : n === 12 ? "100–130" : "130–170"} 回合 · 本地估算上限 200 万 token，超了自动停局`;
  $("#create").onclick = createGame;
}

async function createGame() {
  const f = state.form;
  $("#create").disabled = true;
  await ensureUser();
  const g = await api("/api/games", { method: "POST", body: JSON.stringify({
    variant: f.variant, models: f.models, guard: f.guard, playbook: "static", xray: f.xray,
    human_seats: [f.seat], turn_seconds: f.clock || null }) });
  $("#create").disabled = false;
  if (g.id) location.hash = `#/g/${g.id}?seat=${f.seat}`;
}

/* ─────────────────────────────────────────────── table */
async function openTable() {
  $("#lobby").hidden = true; $("#table").hidden = false;
  $("#nav-lobby").classList.remove("on"); $("#nav-table").hidden = false; $("#nav-table").classList.add("on");
  $("#nav-table").href = location.hash;
  clearInterval(window._poll);
  await ensureUser();
  await ensureSeat();
  await pull();
  closeLive();
  state.lastEv = -1;
  connectLive();
  window._tick = window._tick || setInterval(tickClock, 1000);
}

/* One WebSocket per table. Heartbeat every hello.heartbeat_s (the first one after a
 * random fraction of it, so a server restart does not bring every tab back in the
 * same second); reconnect with capped exponential backoff and resume from the last
 * event seq, so nothing is missed and nothing is shown twice. */
function connectLive() {
  const gid = state.game; if (!gid) return;
  const tok = state.seat ? store.get(seatKey(gid, state.seat)) : "";
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/games/${gid}?token=${encodeURIComponent(tok || "")}&last_seq=${state.lastEv}`);
  state.ws = ws;
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data);
    if (d.op === "hello") {
      state.conn = "已连"; state.retry = 0; renderHealth();
      const beat = () => { if (state.ws === ws && ws.readyState === 1) ws.send(JSON.stringify({ op: "ping", t: Date.now() })); };
      clearTimeout(state.beatFirst); clearInterval(state.beat);
      state.beatFirst = setTimeout(() => { beat(); state.beat = setInterval(beat, d.heartbeat_s * 1000); }, Math.random() * d.heartbeat_s * 1000);
    } else if (d.op === "event") {
      if (d.seq <= state.lastEv) return;
      state.lastEv = d.seq;
      pull(); if (state.feedTab === "all") loadLog();
    } else if (d.op === "ack" && state.acks[d.request_id]) {
      state.acks[d.request_id](d.result); delete state.acks[d.request_id];
    }
  };
  ws.onclose = (e) => {
    if (state.ws !== ws) return;
    clearTimeout(state.beatFirst); clearInterval(state.beat);
    state.conn = "重连中"; renderHealth();
    if (e.code === 4404) { state.conn = "轮询"; clearInterval(window._poll); window._poll = setInterval(pull, 3000); return; }
    const wait = Math.min(10000, 500 * 2 ** state.retry++) * (0.5 + Math.random());
    setTimeout(() => { if (state.ws === ws && state.game === gid) connectLive(); }, wait);
  };
}

function closeLive() {
  const ws = state.ws; state.ws = null;
  clearTimeout(state.beatFirst); clearInterval(state.beat);
  if (ws) ws.close();
}

function sendAct(seq, action) {
  const request_id = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  const ws = state.ws;
  if (ws && ws.readyState === 1) {
    return new Promise((resolve) => {
      state.acks[request_id] = resolve;
      ws.send(JSON.stringify({ op: "act", seq, action, request_id }));
      setTimeout(() => { if (state.acks[request_id]) { delete state.acks[request_id]; resolve({ status: "timeout" }); } }, 15000);
    });
  }
  return api(`/api/games/${state.game}/act`, { method: "POST", body: JSON.stringify({ seat: state.seat, seq, action, request_id }) });
}

async function pull() {
  if (!state.game) return;
  const s = await api(`/api/games/${state.game}` + (state.seat ? `?seat=${state.seat}` : ""));
  if (s.error) { location.hash = "#/"; return; }
  state.s = s;
  state.pulledAt = Date.now();
  if (state.picked == null && s.view) state.picked = s.view.you || null;
  renderTable();
}

function renderTable() {
  const s = state.s, g = s.game, v = s.view;
  $("#lede").textContent = `${g.id} · ${g.variant} · ${g.model}`
    + (g.xray ? " · 测试模式" : "") + (g.state === "over" ? " · 已结束" : "");
  if (g.state === "waiting") {
    $("#c-label").textContent = "还没开局";
    $("#c-seat").textContent = `${g.id}`;
    $("#c-sub").textContent = "你已入座，点下面开始";
    $("#c-note").textContent = "";
  }
  renderNow(); renderFelt(); renderPrivate(); renderPicked(); renderNews(); renderFeed(); renderTurn(); renderHealth();
}


/* A line that always says what the table is doing: who is on the clock, at what,
 * for how long, and -- when it is a model -- which tools it has reached for so
 * far this turn. Nothing here is private: the actor and the task are public
 * facts, and the tool names are the watcher's, not the seat's. */
function renderNow() {
  const s = state.s, g = s.game, bar = $("#nowbar");
  bar.textContent = ""; bar.className = "nowbar";
  const a = s.acting || g.acting;
  if (g.state === "over") {
    bar.classList.add("idle");
    bar.append(el("span", "dotlive"), el("span", "who", "牌局结束"),
      el("span", "what", `${(g.outcome || {}).winner === "village" ? "村民胜" : "狼胜"} · 第 ${g.round} 轮 · ${g.health.turns} 个回合`));
    return;
  }
  if (g.state === "waiting") {
    bar.classList.add("idle");
    bar.append(el("span", "dotlive"), el("span", "who", "还没开局"), el("span", "what", "点右下角开始"));
    return;
  }
  if (g.state === "aborted" || g.state === "crashed") {
    bar.classList.add("idle", "bad");
    bar.append(el("span", "dotlive"), el("span", "who", g.state === "aborted" ? "这一局作废了" : "这一局崩了"),
      el("span", "what", g.error || "没有给出原因"));
    return;
  }
  if (!a) {
    bar.classList.add("idle");
    bar.append(el("span", "dotlive"), el("span", "who", "结算中"), el("span", "what", "裁判在推进这一步"));
    return;
  }
  if (a.actor == null || a.task === "hidden") {
    // the server keeps night actors private (who moves first would give the seer away)
    bar.append(el("span", "dotlive"), el("span", "who", "夜里"), el("span", "what", "有人在行动，天亮前不公开是谁"));
    bar.append(el("span", "el", `${Math.round(a.seconds || 0)}s`));
    return;
  }
  const mine = a.actor === state.seat;
  bar.append(el("span", "dotlive"));
  bar.append(el("span", "who", mine ? `轮到你（${a.actor} 号）` : `${a.actor} 号`));
  bar.append(el("span", "what", mine ? `正在${VERB[a.task] || a.task}` : `正在${VERB[a.task] || a.task}` + (a.human ? "（真人）" : `　${SHORT[a.model] || a.model}`)));
  if (a.steps && a.steps.length) {
    bar.append(el("span", "tools", "已调用 " + a.steps.filter((x) => x && !x.startsWith("<")).join(" → ")));
  }
  bar.append(el("span", "el", `${Math.round(a.seconds)}s`));
}

const VERB = { speak: "发言", vote: "投票", night: "夜里行动", campaign: "竞选警长", campaign_vote: "投警长",
  last_words: "留遗言", hunter_shoot: "开枪", badge: "移交警徽", pack_chat: "狼频道说话", pack_plan: "定狼队分工" };

function renderFelt() {
  const s = state.s, g = s.game, v = s.view || {};
  const felt = $("#felt");
  [...felt.querySelectorAll(".seat")].forEach((n) => n.remove());
  const pub = v.public || {};
  const alive = new Set(pub.alive || g.alive || []);
  const dead = Object.fromEntries((pub.dead || []).map((d) => [d.player_id, d]));
  const n = g.seats;
  const live = (s.acting || g.acting) || (s.log || []).slice().reverse().find((e) => e.kind === "phase" && e.actor);
  for (let i = 0; i < n; i++) {
    const id = i + 1;
    const a = (i * 2 * Math.PI) / n;
    const node = el("div", "seat");
    node.style.left = `${50 + 43.5 * Math.sin(a) - 6.5}%`;
    node.style.top = `${50 - 43.5 * Math.cos(a) - 6.5}%`;
    const isDead = !alive.has(id) && (pub.alive || g.alive || []).length > 0;
    if (isDead) node.classList.add("dead");
    if (id === state.seat) node.classList.add("you");
    if (live && live.actor === id && !isDead) node.classList.add("live");
    if (id === state.picked) node.classList.add("sel");
    node.append(el("span", "n", id));
    const who = id === state.seat ? "你" : g.human_seats.includes(id) ? "真人"
      : SHORT[(g.seat_models || {})[id]] || "AI";
    const shown = (s.revealed || {})[id] || (s.revealed || {})[String(id)];
    node.append(el("span", "t", shown ? ROLE_CN[shown] || shown : (isDead ? "已出局" : who)));
    if (shown) {
      if (shown === "werewolf") node.classList.add("wolf");
      node.append(el("span", "role", who));
    }
    node.title = (g.seat_models || {})[id] || "";
    node.onclick = () => {
      state.picked = state.picked === id ? null : id;   // click again to put it away
      renderFelt(); renderPicked();
    };
    felt.append(node);
  }
  const cur = live && !$("#turn").dataset.mine;
  if (g.state !== "waiting") {
    $("#c-label").textContent = g.state === "over" ? "结束" : (live ? taskName(live.task) : "进行中");
    $("#c-seat").textContent = g.state === "over"
      ? ((g.outcome || {}).winner === "village" ? "村民胜" : "狼胜")
      : (live ? `${live.actor} 号` : "—");
    $("#c-sub").textContent = g.state === "over" ? `第 ${g.round} 天结束`
      : `第 ${g.round} ${String(g.phase).startsWith("night") || String(g.phase).startsWith("pack") ? "夜" : "天"} · ${alive.size} 人在座`;
    $("#c-note").textContent = live && live.actor === state.seat ? "轮到你" : "";
  }
}

const TASK_CN = { speak: "发言", vote: "投票", night: "夜晚行动", campaign: "竞选警长", campaign_vote: "投警长",
  last_words: "遗言", hunter_shoot: "猎人开枪", badge: "移交警徽", pack_chat: "狼人频道", pack_plan: "狼队分工" };
const taskName = (t) => TASK_CN[t] || t;

function renderPrivate() {
  const v = (state.s.view || {}); const p = v.private || {};
  const box = $("#private"); box.textContent = "";
  if (!state.seat) { box.hidden = true; return; }
  box.hidden = false;
  box.append(el("h3", null, `你是 ${v.you} 号 · ${roleCN(v.role)}`));
  const lines = [];
  if (p.fellow_wolves) lines.push(`狼队友：${p.fellow_wolves.join("、") || "只剩你"}`);
  if (p.my_tactic) lines.push(`你的分工：${p.my_tactic}`);
  if (p.checks) lines.push(...(p.checks.length
    ? p.checks.map((c) => `第 ${c.round} 夜验 ${c.target} 号 · ${c.is_wolf ? "狼" : "好人"}`)
    : ["还没验过人"]));
  if ("antidote_available" in p) lines.push(`解药 ${p.antidote_available ? "还在" : "已用"} · 毒药 ${p.poison_available ? "还在" : "已用"}`);
  if (v.night && v.night.victims_tonight) lines.push(`今晚被刀：${v.night.victims_tonight.join("、") || "还没定"}`);
  if (!lines.length) lines.push("你没有夜间能力，靠发言和投票。");
  for (const l of lines) box.append(el("div", null, l));
}

const ROLE_CN = { werewolf: "狼人", seer: "预言家", witch: "女巫", hunter: "猎人", guard: "守卫", villager: "村民" };
const roleCN = (r) => ROLE_CN[r] || r || "观战";

function renderPicked() {
  const v = state.s.view || {}; const pub = v.public || {};
  const box = $("#picked"); box.textContent = "";
  const id = state.picked;
  if (!id) { box.hidden = true; return; }
  box.hidden = false;
  const shut = el("button", "shut", "×");
  shut.title = "关掉";
  shut.onclick = () => { state.picked = null; renderFelt(); renderPicked(); };
  box.append(shut);
  const dead = (pub.dead || []).find((d) => d.player_id === id);
  const sm = (state.s.game.seat_models || {})[id];
  box.append(el("h3", null, `${id} 号 · ${id === state.seat ? "你" : sm || "真人"} · 公开记录`));
  const said = (pub.speeches || []).filter((sp) => sp.player_id === id);
  const votes = Object.entries(pub.votes || {}).map(([r, b]) => b[id] != null ? `第${r}轮投 ${b[id]}` : null).filter(Boolean);
  const bits = [];
  if (dead) bits.push(`${dead.round} 轮出局（${{ exiled: "被放逐", night: "夜里", shot: "被枪", detonated: "自爆" }[dead.cause] || dead.cause}）`);
  if (pub.sheriff === id) bits.push("警长");
  if (votes.length) bits.push(votes.join("，"));
  bits.push(said.length ? `发言 ${said.length} 次，最后一次：${said[said.length - 1].content.slice(0, 60)}…` : "还没发过言");
  for (const b of bits) box.append(el("div", null, b));

  // The second half of a seat: what he was thinking while he did all that. It is
  // here only when the table is already open to this viewer -- the game is over,
  // this viewer is out of it, or the table is in 测试模式. A living player clicking
  // a rival gets the public half and nothing else; that is the whole game.
  const traces = state.s.traces || null;
  if (!traces) return;
  const rows = traces[id] || traces[String(id)] || [];
  box.append(el("h3", null, `${id} 号在想什么 · 最近 ${rows.length} 步`));
  if (!rows.length) { box.append(el("div", "it", "这个座位还没走过回合。")); return; }
  const wrap = el("div", "think");
  for (const r of rows) wrap.append(thinkStep(r));
  box.append(wrap);
}

/* What the table learned, from the public record only -- who died (never how),
 * what the ballot said, who holds the badge. Built from `view.public`, so a
 * spectator and a seated player read exactly the same list. */
function renderNews() {
  const s = state.s, g = s.game, pub = (s.view || {}).public || {};
  const box = $("#news"); box.textContent = "";
  const night = String(g.phase).startsWith("night") || String(g.phase).startsWith("pack");
  $("#phase-now").textContent = g.state === "over" ? "已结束" : `第 ${g.round} ${night ? "夜" : "天"}`;
  const CAUSE = { exiled: "被放逐", night: "夜里出局", shot: "被猎人带走", detonated: "自爆" };
  const byRound = {};
  for (const d of pub.dead || []) (byRound[d.round] = byRound[d.round] || []).push(d);
  const rounds = [...new Set([...Object.keys(byRound), ...Object.keys(pub.exiles || {})].map(Number))].sort((a, b) => a - b);
  const rows = [];
  for (const r of rounds) {
    const here = byRound[r] || [];
    if (!here.some((d) => d.cause === "night") && r < g.round) {
      rows.push({ r: `第 ${r} 夜`, t: "平安夜，没有人死", cls: "" });
    }
    for (const d of here) {
      rows.push({ r: `第 ${r} 轮`, t: `${d.player_id} 号 ${CAUSE[d.cause] || d.cause}`,
                  cls: "death" + (d.player_id === state.seat ? " you" : "") });
    }
    if (Object.prototype.hasOwnProperty.call(pub.exiles || {}, r)) {
      const counts = {};
      for (const t of Object.values((pub.votes || {})[r] || {})) if (t != null) counts[t] = (counts[t] || 0) + 1;
      const tally = Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([t, n]) => `${t}号 ${n}票`).join("、");
      const ex = pub.exiles[r];
      rows.push({ r: "", t: `票型 ${tally || "无人投票"} → ${ex == null ? "平票，无人出局" : ex + " 号出局"}`, cls: "" });
    }
  }
  if (pub.sheriff) rows.push({ r: "", t: `${pub.sheriff} 号是警长：每天第一个发言，票算 1.5`, cls: "" });
  if (!rows.length) { box.append(el("div", "it", "还没有公开的消息。")); return; }
  for (const row of rows.slice(-14)) {
    const n = el("div", "n " + row.cls);
    n.append(el("span", "r", row.r), el("span", "t", row.t));
    box.append(n);
  }
  const scroller = box.closest(".news"); if (scroller) scroller.scrollTop = scroller.scrollHeight;
}

function renderFeed() {
  const reviewing = !!(state.s.game && state.s.game.reviewing);
  const hasReview = reviewing || ((state.s.review || []).length > 0);
  $("#tab-review").hidden = !hasReview;
  if (!hasReview && state.feedTab === "review") state.feedTab = "say";
  if (state.feedTab === "think") state.feedTab = "say";      // it lives on the seat now
  // The review is the point of the table once it starts; land on it once, then leave
  // the reader wherever they go next.
  if (reviewing && !state.reviewSeen) { state.feedTab = "review"; state.reviewSeen = true; }
  for (const [id, key] of [["#tab-say", "say"], ["#tab-vote", "vote"],
                           ["#tab-all", "all"], ["#tab-review", "review"]]) {
    $(id).className = "t" + (state.feedTab === key ? " on" : "");
  }
  $("#tab-say").onclick = () => { state.feedTab = "say"; renderFeed(); };
  $("#tab-vote").onclick = () => { state.feedTab = "vote"; renderFeed(); };
  $("#tab-all").onclick = () => { state.feedTab = "all"; loadLog(); };
  $("#tab-review").onclick = () => { state.feedTab = "review"; renderFeed(); };
  if (state.feedTab === "all") return renderLog();
  if (state.feedTab === "vote") return renderVotes();
  if (state.feedTab === "review") return renderReview();
  const v = state.s.view || {}; const pub = v.public || {};
  const box = $("#speeches"); box.textContent = "";
  const all = pub.speeches || [];
  const rounds = [...new Set(all.map((sp) => sp.round))].sort((a, b) => a - b);
  // Which round you are reading is a choice, not the clock: 第 1 轮 is most of what
  // you reason from on 第 3 轮. `null` means all of them, oldest first.
  if (state.sayRound != null && !rounds.includes(state.sayRound)) state.sayRound = null;
  const list = state.sayRound == null ? all : all.filter((sp) => sp.round === state.sayRound);
  $("#spoken").textContent = all.length
    ? (state.sayRound == null ? `${all.length} 条 · 共 ${rounds.length} 轮` : `${list.length} 条`)
    : "";
  if (rounds.length > 1) box.append(roundPicker(rounds, "sayRound", renderFeed));
  if (!all.length) { box.append(el("div", "it", "还没有人发言。")); return; }
  let shown = null;
  for (const sp of list) {
    if (sp.round !== shown && state.sayRound == null) {
      shown = sp.round;
      box.append(el("div", "roundmark", `第 ${sp.round} 轮`));
    }
    const d = el("div", "sp" + (sp.player_id === state.seat ? " mine" : ""));
    const h = el("div");
    h.append(el("span", "who", sp.player_id === state.seat ? `你 · ${sp.player_id} 号` : `${sp.player_id} 号`));
    h.append(el("span", "meta", (sp.kind === "last_words" ? "遗言" : sp.kind === "campaign" ? "竞选" : "发言") + ` · 第 ${sp.round} 轮`));
    d.append(h, el("div", "body", sp.content));
    box.append(d);
  }
  // Only chase the newest line when you are reading everything; a chosen round
  // should start at its first speech, not its last.
  const scroller = box.closest(".feed");
  if (scroller) scroller.scrollTop = state.sayRound == null ? scroller.scrollHeight : 0;
}

/* One row of round chips, plus 全部. Writes the chosen round into `state[key]`
 * so the once-a-second poll re-renders into the same choice. */
function roundPicker(rounds, key, redraw) {
  const bar = el("div", "chips roundpick");
  const pick = (v) => { state[key] = v; redraw(); };
  const all = el("button", "chip" + (state[key] == null ? " on" : ""), "全部");
  all.onclick = () => pick(null);
  bar.append(all);
  for (const r of rounds) {
    const b = el("button", "chip" + (state[key] === r ? " on" : ""), `第 ${r} 轮`);
    b.onclick = () => pick(r);
    bar.append(b);
  }
  return bar;
}


/* One step: index, tactic name (Chinese, with the raw id beside it), the
 * thought that chose it, and what came back. */
function thinkStep(r) {
  const st = el("div", "st" + (r.blocked ? " blocked" : ""));
  st.append(el("span", "k", r.n));
  const v = el("span", "v");
  v.append(el("span", "a", stepCN(r.action) + (r.blocked ? ` [${r.blocked}]` : "")));
  if (stepCN(r.action) !== r.action) v.append(el("span", "id", ` ${r.action}`));
  if (r.thought) v.append(el("span", "o", " " + r.thought));
  if (r.observation) v.append(el("div", "o", "→ " + r.observation));
  st.append(v);
  return st;
}




/* ────────────────────────── 复盘
 * The game is over, the result is fixed, and every seat has been handed the same
 * truth. This pane shows the two halves of that: the deal, and what the players
 * said about it afterwards. It reads `state.s.review`, which the server keeps
 * serving after the phase closes, so the tab still works on a finished game. */
function reviewHead() {
  const s = state.s, g = s.game;
  const head = el("div", "reviewhead");
  const won = g.outcome && g.outcome.winner;
  const title = g.reviewing ? "复盘进行中" : "复盘";
  head.append(el("div", "hd", title + (won ? ` · ${won === "werewolf" ? "狼人阵营" : "好人阵营"}获胜` : "")));
  const roles = s.revealed || {};
  const alive = new Set((g.alive || []).map(Number));
  const grid = el("div", "deal");
  for (const seat of Object.keys(roles).map(Number).sort((a, b) => a - b)) {
    const cell = el("div", "d" + (alive.has(seat) ? " up" : "") + (seat === state.seat ? " mine" : ""));
    cell.append(el("span", "s", `${seat}号`), el("span", "r", roleCN(roles[seat])));
    grid.append(cell);
  }
  if (grid.childNodes.length) head.append(grid);
  return head;
}

function renderReview() {
  const s = state.s;
  const box = $("#speeches"); box.textContent = "";
  const said = s.review || [];
  $("#spoken").textContent = `${said.length} 条复盘发言`;
  box.append(reviewHead());
  if (!said.length) {
    box.append(el("div", "it", s.game.reviewing ? "牌都翻开了，还没有人开口。" : "这一局没有复盘记录。"));
    return;
  }
  for (const e of said) {
    const roles = s.revealed || {};
    const role = roles[e.player_id] || roles[String(e.player_id)];
    const d = el("div", "sp" + (e.player_id === state.seat ? " mine" : ""));
    const h = el("div");
    h.append(el("span", "who", (e.player_id === state.seat ? `你 · ${e.player_id} 号` : `${e.player_id} 号`)
      + (role ? ` · ${roleCN(role)}` : "")));
    h.append(el("span", "meta", `复盘 · 第 ${e.round} 轮`));
    d.append(h, el("div", "body", e.content));
    box.append(d);
  }
  const scroller = box.closest(".feed"); if (scroller) scroller.scrollTop = scroller.scrollHeight;
}


/* Every round's ballot on its own: who put a name on whom, the tally that came
 * out of it, and what the table did about it. The vote pattern is the densest
 * public evidence there is -- reading it one round at a time is the point. */
function renderVotes() {
  const v = state.s.view || {}, pub = v.public || {};
  const box = $("#speeches"); box.textContent = "";
  const votes = pub.votes || {}, exiles = pub.exiles || {};
  const rounds = [...new Set([...Object.keys(votes), ...Object.keys(exiles)].map(Number))].sort((a, b) => a - b);
  $("#spoken").textContent = rounds.length ? `${rounds.length} 轮投票` : "";
  if (!rounds.length) { box.append(el("div", "it", "还没有投过票。")); return; }
  if (state.voteRound != null && !rounds.includes(state.voteRound)) state.voteRound = null;
  if (rounds.length > 1) box.append(roundPicker(rounds, "voteRound", renderVotes));
  const sheriff = pub.sheriff;
  for (const r of (state.voteRound == null ? rounds : [state.voteRound])) {
    const ballot = votes[r] || votes[String(r)] || {};
    box.append(el("div", "roundmark", `第 ${r} 轮`));
    const tally = {};
    for (const [voter, target] of Object.entries(ballot)) {
      if (target == null) continue;
      const w = Number(voter) === sheriff ? 1.5 : 1;      // the badge is worth half a vote more
      tally[target] = (tally[target] || 0) + w;
    }
    const order = Object.entries(tally).sort((a, b) => b[1] - a[1]);
    for (const [target, n] of order) {
      const who = Object.entries(ballot).filter(([, t]) => String(t) === String(target))
        .map(([voter]) => `${voter}号${Number(voter) === sheriff ? "(警)" : ""}`);
      const row = el("div", "ballot" + (String(exiles[r]) === String(target) ? " out" : ""));
      row.append(el("span", "tg", `${target} 号`));
      row.append(el("span", "ct", `${n} 票`));
      row.append(el("span", "vs", who.join("、")));
      box.append(row);
    }
    const skipped = Object.entries(ballot).filter(([, t]) => t == null).map(([voter]) => `${voter}号`);
    if (skipped.length) box.append(el("div", "ballot none", `弃票：${skipped.join("、")}`));
    const has = Object.prototype.hasOwnProperty.call(exiles, r)
             || Object.prototype.hasOwnProperty.call(exiles, String(r));
    const ex = exiles[r] !== undefined ? exiles[r] : exiles[String(r)];
    box.append(el("div", "verdict", !has ? "还没结算"
      : ex == null ? "平票，无人出局" : `${ex} 号出局`));
  }
  const scroller = box.closest(".feed"); if (scroller) scroller.scrollTop = 0;
}

/* ────────────────────────── the whole record
 * One row per turn, with the model that sat in that seat and whether the
 * harness had to fill the turn in. This is the journal `cli replay` runs on,
 * so what is read here is exactly what rebuilds the world. */
async function loadLog() {
  try {
    state.log = await api(`/api/games/${state.game}/log`);
  } catch (e) {
    state.log = { error: String(e) };
  }
  renderFeed();
}

const TASK_SHORT = { speak: "发言", vote: "投票", night: "夜里", campaign: "竞选", campaign_vote: "投警长",
  last_words: "遗言", hunter_shoot: "开枪", badge: "警徽", pack_chat: "狼频道", pack_plan: "狼分工",
  review: "复盘" };

function describeAction(a) {
  if (!a || typeof a !== "object") return String(a);
  const args = a.args || {}, name = toolCN(a.name);
  if (args.content) return `${name}：${args.content}`;
  if (args.target_id != null) return `${name} → ${args.target_id} 号`;
  if (args.plan) return `${name}：${args.plan}` + (args.roles
    ? "（" + Object.entries(args.roles).map(([w, t]) => `${w}号 ${t}`).join("，") + "）" : "");
  if (args.reason) return `${name}：${args.reason}`;
  return name;
}

function renderLog() {
  const box = $("#speeches"); box.textContent = "";
  const log = state.log;
  if (!log) { box.append(el("div", "it", "读取中……")); return; }
  if (log.error) { box.append(el("div", "it", `读不到记录：${log.error}`)); return; }
  $("#spoken").textContent = `${log.journal.length} 个回合`;
  for (const r of log.journal) {
    const row = el("div", "logrow" + (r.forced ? " forced" : "") + (r.actor === state.seat ? " mine" : ""));
    row.append(el("span", "i", r.n));
    row.append(el("span", "s", `${r.actor}号`));
    const body = el("span", "b");
    body.append(el("span", "m", `${TASK_SHORT[r.task] || r.task} · ${SHORT[r.model] || r.model}${r.forced ? " · 兜底" : ""}  `));
    body.append(document.createTextNode(describeAction(r.action)));
    row.append(body);
    box.append(row);
  }
  const scroller = box.closest(".feed"); if (scroller) scroller.scrollTop = scroller.scrollHeight;
}

/* ────────────────────────── your turn */
/* Only the clock ticks; the panel is rebuilt when the TURN changes, never on a
 * timer. Rebuilding it every second re-created the textarea under the cursor,
 * which made typing impossible -- the caret and focus went with it. */
function tickClock() {
  const s = state.s; if (!s) return;
  const a = s.acting || (s.game && s.game.acting);
  const el2 = $("#nowbar .el");
  if (a && el2) { a.seconds += 1; el2.textContent = `${Math.round(a.seconds)}s`; }
  if (!s.me || !s.me.waiting) return;
  const t = s.me.turn; if (!t || t.seconds_left == null) return;
  const node = $("#turn .clock"); if (!node) return;
  const gone = Math.floor((Date.now() - (state.pulledAt || Date.now())) / 1000);
  const left = Math.max(0, t.seconds_left - gone);
  node.textContent = `${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}`;
  if (left === 0) node.style.color = "var(--live)";
}

function turnKey(s) {
  const me = s.me, t = me && me.turn;
  return [s.game.state, me && me.waiting ? 1 : 0, t ? t.seq : -1, t ? t.task : "",
          t ? t.feedback || "" : "", state.tool || "", JSON.stringify(state.args)].join("|");
}

function renderTurn(force) {
  const s = state.s; if (!s) return;
  const key = turnKey(s);
  if (!force && key === state.turnKey && $("#turn").childNodes.length) { tickClock(); return; }
  state.turnKey = key;
  const box = $("#turn"); const me = s.me;
  const g = s.game;
  const active = document.activeElement;
  const keepFocus = active && box.contains(active) && active.tagName === "TEXTAREA";
  const caret = keepFocus ? [active.selectionStart, active.selectionEnd] : null;
  box.textContent = "";
  box.dataset.mine = "";

  if (g.state === "waiting") {
    box.append(el("div", "lbl", "你已入座"));
    const b = el("button", null, "开始这一局");
    b.onclick = async () => { b.disabled = true; await api(`/api/games/${g.id}/start`, { method: "POST" }); pull(); };
    box.append(b);
    return;
  }
  const alive = ((s.view || {}).public || {}).alive || [];
  const iAmOut = state.seat && alive.length && !alive.includes(state.seat);

  if (!me || !me.waiting) {
    // out of the game, or between turns: either way the page keeps showing the table
    if (iAmOut && g.state !== "over") {
      box.className = "card turn watch";
      box.append(el("div", "lbl", `你在第 ${outRound(s)} 轮出局了 · 继续观战`));
      box.append(el("div", "rec", "你不再有回合，但桌子照常打完。上面「桌上发生了什么」会继续更新，"
        + "「全部记录」能看到每一步是谁做的、哪个模型做的。牌面会在结束时翻开。"));
    } else if (g.state === "aborted" || g.state === "crashed") {
      box.className = "card turn watch";
      box.append(el("div", "lbl", g.state === "aborted" ? "这一局作废了" : "这一局崩了"));
      box.append(el("div", "rec", g.error || "没有给出原因"));
      box.append(el("div", "rec", "记录还在，「全部记录」和「思考过程」都能翻到停在哪一步。"));
    } else {
      box.className = "card turn";
      box.append(el("div", "lbl", g.state === "over" ? "这一局结束了" : "轮到别人"));
      box.append(el("div", "idle", g.state === "over"
        ? `${(g.outcome || {}).winner === "village" ? "村民" : "狼"}胜 · 第 ${g.round} 轮 · 共 ${g.health.turns} 回合 · 兜底 ${g.health.forced}`
        : (state.seat ? "等 AI 想完。它们的每一步都会出现在左边。" : "你在观战。")));
    }
    if (me && me.forced) box.append(el("div", g.state === "over" || iAmOut ? "rec" : "why",
      `你有 ${me.forced} 个回合超时，系统按规则替你走了最保守的一步，记录里标成兜底。`));
    if (g.state !== "running") {
      const row = el("div", "opts");
      const b1 = el("button", null, "看全部记录"); b1.onclick = () => { state.feedTab = "all"; loadLog(); };
      const b2 = el("button", null, "回大厅"); b2.onclick = () => { location.hash = "#/"; };
      row.append(b1, b2); box.append(row);
    }
    return;
  }
  box.className = "card turn";

  box.dataset.mine = "1";
  const t = me.turn;
  if (state.lastSeq !== t.seq) { state.lastSeq = t.seq; state.wantFocus = true; }
  const head = el("div", "rowhead");
  head.append(el("span", "lbl", `轮到你 · ${taskName(t.task)}`));
  head.append(el("span", "grow"));
  if (t.seconds_left != null) {
    const left = Math.max(0, t.seconds_left - Math.floor((Date.now() - (state._pulled || Date.now())) / 1000));
    head.append(el("span", "clock", `${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}`));
  }
  box.append(head);

  if (t.feedback) box.append(el("div", "why", `上一次被退回：${t.feedback}`));

  const tools = t.tools;
  if (state.tool == null || !tools.find((x) => x.name === state.tool)) { state.tool = tools[0].name; state.args = {}; }
  if (tools.length > 1) {
    const opts = el("div", "opts");
    for (const tool of tools) {
      const b = el("button", "opt" + (tool.name === state.tool ? " on" : ""));
      b.append(el("span", "cn", toolCN(tool.name)));
      b.onclick = () => { state.tool = tool.name; state.args = {}; renderTurn(true); };
      opts.append(b);
    }
    box.append(opts);
  }
  const tool = tools.find((x) => x.name === state.tool);
  box.append(el("div", "desc", toolHint(tool.name)));

  for (const [key, p] of Object.entries(tool.params)) {
    if (p.type === "dict[int,str]") {
      box.append(el("div", "desc", paramCN(key, p)), rolesPicker(key, p, tool));
    } else if (p.choices && p.choices.length) {
      const opts = el("div", "opts");
      if (!p.required) {
        const b = el("button", "opt" + (state.args[key] == null ? " on" : ""), "不填");
        b.onclick = () => { delete state.args[key]; renderTurn(true); }; opts.append(b);
      }
      for (const c of p.choices) {
        const on = String(state.args[key]) === String(c);
        const lab = p.labels && p.labels[c];
        const b = el("button", "opt" + (on ? " on" : "") + (lab ? " wide" : ""));
        b.append(el("span", "cn", lab ? lab.split(" — ")[0] : seatLabel(c)));
        if (lab) {
          b.append(el("span", "id", c));
          const gloss = lab.split(" — ")[1];
          if (gloss) b.append(el("span", "gloss", gloss));
        }
        b.onclick = () => { state.args[key] = c; renderTurn(true); };
        opts.append(b);
      }
      box.append(el("div", "desc", paramCN(key, p)), opts);
    } else if (p.type === "str") {
      const ta = el("textarea");
      ta.placeholder = paramCN(key, p);
      ta.value = state.args[key] || "";
      ta.oninput = () => {
        state.args[key] = ta.value;
        state.turnKey = turnKey(state.s);          // typing is not a new turn
        const send = $("#send"); if (send) send.disabled = !ready(tool) || state.sending;
      };
      box.append(ta);
    }
  }

  const send = el("button", null, "提交"); send.id = "send";
  send.disabled = !ready(tool) || state.sending;
  send.onclick = () => submit(tool, t.seq);
  box.append(send);
  const rule = el("div", "desc", ruleNote(t.task));
  box.append(rule);

  if (state.wantFocus) box.scrollIntoView({ block: "end", behavior: "smooth" });
  const ta = box.querySelector("textarea");
  if (ta && (keepFocus || state.wantFocus)) {
    ta.focus();
    if (caret) ta.setSelectionRange(caret[0], caret[1]);
    else ta.setSelectionRange(ta.value.length, ta.value.length);
    state.wantFocus = false;
  }
}


/* A seat number is a name here, not a value to be labelled. */
const seatLabel = (c) => (typeof c === "number" ? `${c} 号` : String(c));

/* One tactic per living wolf, picked by name. The pack plan used to be filled
 * in silently by the page, which meant a human wolf could not actually lead
 * his own pack. Defaults follow whichever plan is selected, and stay editable. */
function rolesPicker(key, p, tool) {
  const v = state.s.view || {}, priv = v.private || {};
  const wolves = [v.you, ...(priv.fellow_wolves || [])].sort((a, b) => a - b);
  const wrap = el("div", "opts");
  wrap.style.flexDirection = "column";
  const cur = state.args[key] || defaultRoles();
  for (const w of wolves) {
    const row = el("div", "pair");
    row.append(el("span", "seat-n", w === v.you ? `你 ${w}号` : `${w} 号`));
    const sel = el("select");
    for (const c of p.choices || []) {
      const o = el("option", null, (p.labels && p.labels[c]) || c);
      o.value = c;
      if (String(cur[w]) === String(c)) o.selected = true;
      sel.append(o);
    }
    sel.onchange = () => {
      const next = { ...(state.args[key] || defaultRoles()) };
      next[w] = sel.value;
      state.args[key] = next;
      state.turnKey = turnKey(state.s);            // picking is not a new turn
    };
    row.append(sel);
    wrap.append(row);
  }
  state.args[key] = cur;
  state.turnKey = turnKey(state.s);               // filling the default is not a new turn
  return wrap;
}

const outRound = (s) => {
  const d = (((s.view || {}).public || {}).dead || []).find((x) => x.player_id === state.seat);
  return d ? d.round : "?";
};

function ready(tool) {
  for (const [k, p] of Object.entries(tool.params)) {
    if (!p.required) continue;
    const v = state.args[k];
    if (p.type === "str") { if (!v || !v.trim()) return false; }
    else if (p.type === "dict[int,str]") { continue; }
    else if (v == null) return false;
  }
  return true;
}


/* ── what to call an action in front of a person ─────────────────────────
 * The id is what the rules check and what the record stores. It is not a
 * name, so it never reaches a player on its own. The English description
 * that comes with each tool is the model's prompt, not a player's caption. */
const TOOL_CN = {
  speak:           ["发言", "对全桌说话，进入公开记录。说完这一轮就轮到下一个人。"],
  vote:            ["投票", "投一个人出局。票最多的人出局，平票则没人出局。"],
  night_kill:      ["刀人", "报出今晚你想刀的人。狼队多数意见算数。"],
  night_check:     ["查验", "查一个人是好人还是狼。结果只有你看得到。"],
  night_save:      ["用解药", "救今晚被刀的人。解药只有一瓶。"],
  night_poison:    ["用毒药", "毒死一个人。毒药只有一瓶。"],
  night_protect:   ["守护", "今晚保护一个人不被刀。"],
  night_skip:      ["空过", "今晚什么都不做。"],
  pack_say:        ["狼频道发言", "只有活着的狼听得到。可以顺带点名你想走的战术。"],
  pack_plan:       ["定分工", "给狼队定一个方案，并且给每一只活狼分配一个战术。"],
  self_detonate:   ["自爆", "当场亮狼身份。白天立刻结束，不投票、没有遗言，你出局。"],
  campaign_run:    ["上警", "竞选警长并发表竞选发言。上警就放弃了这次选举里的投票权。"],
  campaign_pass:   ["不上警", "退出竞选，保留你在这次选举里的一票。"],
  campaign_vote:   ["投警长", "在候选人里投一个，也可以弃票。"],
  last_words:      ["留遗言", "所有活人都听得到，说完你就离场了。"],
  hunter_shoot:    ["开枪", "带走一个人。不管你是怎么死的，这一枪都算数。"],
  hunter_hold:     ["不开枪", "把枪收起来，谁也不带走。"],
  badge_transfer:  ["移交警徽", "交给谁，谁明天第一个发言、票算 1.5。"],
  badge_tear:      ["撕掉警徽", "警徽作废，之后没有警长。"],
  review_say:      ["复盘发言", "全场都听得到。牌已经翻开了，说什么都不会改变这一局的输赢。"],
  review_pass:     ["复盘过", "这一轮没有要补充的。全场都过，复盘就结束。"],
};
/* The tools an AI calls to look things up. They never appear as a choice --
 * only in the trace, where the point is seeing what it went and checked. */
const INFO_CN = {
  read_board: "看场上局势", read_belief: "读自己的判断", update_belief: "更新判断",
  check_ability: "确认自己的能力", kill_options: "看今晚能刀谁", query_history: "翻某人的发言",
  query_votes: "翻票型", query_deaths: "翻死亡记录", list_strategies: "列出全部战术",
  query_strategy: "查一条战术", list_plans: "列出狼队方案", my_assignment: "看自己分到的战术",
};
const stepCN = (n) => INFO_CN[n] || (TOOL_CN[n] && TOOL_CN[n][0]) || n;
const toolCN = (n) => (TOOL_CN[n] || [n, ""])[0];
const toolHint = (n) => (TOOL_CN[n] || [n, ""])[1];

const PARAM_CN = {
  target_id: "选一个人",
  content:   "写下你要说的话",
  reason:    "为什么（只给你自己留个底，别人看不到）",
  tactic:    "你想走的战术（可以不选）",
  plan:      "狼队这一夜的整体方案",
  roles:     "每一只活狼分到的战术",
  vote_target: "狼队明天约定投谁（不是命令，可以不定）",
};
const paramCN = (k, p) => PARAM_CN[k] || p.description || k;

const RULE_NOTE = {
  speak: "进入公开记录，任何人随时可以调出来核对。",
  vote: "不能弃票、不能投自己；票最多的人出局，平票则无人出局。",
  night: "没有人看得见这一步，天亮只公布谁死了，不公布怎么死的。",
  campaign: "上警会花掉你在这次选举里的投票权。",
  last_words: "所有活人都听得到，之后没有你的回合了。",
  hunter_shoot: "不管你是怎么死的，这一枪都算数。",
  badge: "交给谁，谁明天第一个发言、票算 1.5。",
  review: "这一局已经结束，输赢定死了。全场底牌和全部记录都摆在你面前，说什么都不会改变结果。",
};
const ruleNote = (t) => RULE_NOTE[t] || "选单是裁判按当前状态算出来的；不在单子上的提交会被退回并告诉你原因。";

async function submit(tool, seq) {
  state.sending = true; renderTurn();
  const args = {};
  for (const [k, p] of Object.entries(tool.params)) {
    if (k in state.args) args[k] = state.args[k];
    else if (p.type === "dict[int,str]") args[k] = defaultRoles();
  }
  const r = await sendAct(seq, { name: tool.name, args });
  state.sending = false;
  if (r.status === "illegal") { state.s.me.turn.feedback = r.reason; renderTurn(true); return; }
  state.args = {}; state.tool = null; state.turnKey = null;
  await pull();
}

function defaultRoles() {
  const v = state.s.view || {}; const p = v.private || {};
  const wolves = [v.you, ...(p.fellow_wolves || [])];
  return Object.fromEntries(wolves.map((w) => [w, "shenshui"]));
}

function renderHealth() {
  const g = (state.s || {}).game; if (!g) return;
  const h = g.health;
  $("#h-forced").textContent = h.turns ? `${(h.forced_rate * 100).toFixed(1)}%` : "—";
  $("#h-forced").style.color = h.usable ? "var(--good)" : "var(--live)";
  $("#h-tokens").textContent = h.tokens ? `${(h.tokens / 1000).toFixed(0)}k` : "0";
  $("#h-conn").textContent = state.conn;
  $("#h-conn").style.color = state.conn === "已连" ? "var(--good)" : "var(--live)";
}

/* ─────────────────────────────────────────────── boot */
$("#nav-lobby").onclick = (e) => { e.preventDefault(); location.hash = "#/"; };
route();
