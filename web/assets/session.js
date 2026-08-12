// 会话详情:聊天视图 + 折叠 + 深链 + 子 agent 抽屉。
import { api, copyText, esc, fmtBytes, fullTime, initHeader, resumeSession, reveal, toast } from "./common.js";

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const SID = (params.get("sid") || "").toLowerCase();
const DEEP_SEQ = params.get("seq") ? Number(params.get("seq")) : null;

const state = {
  firstSeq: null,
  lastSeq: null,
  hasBefore: false,
  hasAfter: false,
  showSystem: false,
  session: null,
};

// ---------- 渲染 ----------
const ROLE_LABEL = { user: "你", assistant: "Claude", tool: "工具", system: "系统", attachment: "附件" };

function blockHtml(b) {
  switch (b.kind) {
    case "md_html":
      return `<div class="md">${b.html}</div>`;
    case "thinking":
      return `<details class="fold"><summary>思考过程</summary><pre>${esc(b.text)}</pre></details>`;
    case "tool_use":
      return `<details class="fold"><summary>🔧 ${esc(b.name)} <i>${esc(b.summary)}</i></summary><pre>${esc(b.detail)}</pre></details>`;
    case "tool_result":
      return `<details class="fold ${b.is_error ? "is-error" : ""}"><summary>↳ 工具结果${b.is_error ? "(出错)" : ""}</summary><pre>${esc(b.text)}</pre></details>`;
    case "command": {
      const out = b.stdout ? `<details class="fold"><summary>输出</summary><pre>${esc(b.stdout)}</pre></details>` : "";
      return `<div class="cmd-badge mono">/${esc(b.name.replace(/^\//, ""))} ${esc(b.args)}</div>${out}`;
    }
    case "compact_summary":
      return `<details class="fold"><summary>上下文压缩摘要(自动生成)</summary><pre>${esc(b.text)}</pre></details>`;
    case "compact_boundary":
      return `<div class="compact-divider mono">── 上下文已压缩 ${esc(String(b.pre ?? "?"))} → ${esc(String(b.post ?? "?"))} tokens(${esc(b.trigger || "")}) ──</div>`;
    case "system":
      return `<div class="sys-line mono">[${esc(b.subtype)}] ${esc(b.text)}</div>`;
    case "attachment":
      return `<div class="sys-line mono">[附件 ${esc(b.subtype)}]${b.text ? " " + esc(b.text) : ""}</div>`;
    default:
      return "";
  }
}

function itemHtml(it) {
  const role = it.role;
  const cls = role === "user" ? "user" : role === "assistant" ? "assistant" : "aux";
  const when = it.ts ? fullTime(it.ts) : "";
  return `
  <article class="msg ${cls}" data-seq="${it.seq}">
    <div class="who mono">${esc(ROLE_LABEL[role] || role)} <span>#${it.seq}</span> <time>${esc(when)}</time></div>
    ${it.blocks.map(blockHtml).join("")}
  </article>`;
}

function renderWindowInto(el, win, { append = false, prepend = false } = {}) {
  const htmlStr = win.items.map(itemHtml).join("");
  if (prepend) el.insertAdjacentHTML("afterbegin", htmlStr);
  else if (append) el.insertAdjacentHTML("beforeend", htmlStr);
  else el.innerHTML = htmlStr || '<div class="empty">这一段没有可显示的消息(试试打开「系统事件」)。</div>';
}

// ---------- 消息分页 ----------
async function fetchWindow(q) {
  const p = new URLSearchParams(q);
  p.set("show_system", state.showSystem);
  return api(`/api/sessions/${SID}/messages?${p}`);
}

function updateEdges(win, { prepend = false, append = false } = {}) {
  if (!prepend) {
    state.hasAfter = win.has_more_after;
    state.lastSeq = win.last_seq ?? state.lastSeq;
  }
  if (!append) {
    state.hasBefore = win.has_more_before;
    state.firstSeq = win.first_seq ?? state.firstSeq;
  }
  if (prepend && win.first_seq != null) state.firstSeq = win.first_seq;
  if (append && win.last_seq != null) state.lastSeq = win.last_seq;
  if (prepend) state.hasBefore = win.has_more_before;
  if (append) state.hasAfter = win.has_more_after;
  $("load-earlier").hidden = !state.hasBefore;
  $("load-later").hidden = !state.hasAfter;
  if (win.source === "archive") {
    const n = $("src-note");
    n.textContent = "源 transcript 已被官方清理,当前展示的是归档副本(只读;要 resume 先去归档页还原)。";
    n.hidden = false;
  }
}

async function initialLoad() {
  const win = DEEP_SEQ != null ? await fetchWindow({ around_seq: DEEP_SEQ }) : await fetchWindow({});
  renderWindowInto($("msgs"), win);
  reveal($("msgs"));
  updateEdges(win);
  if (DEEP_SEQ != null) {
    const target = nearestSeqEl(DEEP_SEQ);
    if (target) {
      target.scrollIntoView({ block: "center" });
      target.classList.add("flash");
    }
  } else {
    window.scrollTo(0, document.body.scrollHeight);
  }
}

function nearestSeqEl(seq) {
  let best = null, bestDist = Infinity;
  document.querySelectorAll("#msgs [data-seq]").forEach((el) => {
    const d = Math.abs(Number(el.dataset.seq) - seq);
    if (d < bestDist) { best = el; bestDist = d; }
  });
  return best;
}

async function loadEarlier() {
  if (state.firstSeq == null) return;
  const win = await fetchWindow({ before_seq: state.firstSeq });
  const prevHeight = document.body.scrollHeight;
  renderWindowInto($("msgs"), win, { prepend: true });
  updateEdges(win, { prepend: true });
  window.scrollBy(0, document.body.scrollHeight - prevHeight); // 保持视口停在原消息
}

async function loadLater() {
  if (state.lastSeq == null) return;
  const win = await fetchWindow({ after_seq: state.lastSeq });
  renderWindowInto($("msgs"), win, { append: true });
  updateEdges(win, { append: true });
}

// ---------- 头部 ----------
function headerBadges(s) {
  const out = [];
  if (s.source_missing) out.push('<span class="badge missing">源已清理</span>');
  else if (s.archived) out.push('<span class="badge archived">已归档</span>');
  if (s.has_compact) out.push('<span class="badge">经历过压缩</span>');
  return out.join("");
}

function renderHeader(det) {
  const s = det.session;
  state.session = s;
  document.title = `ClaudeDeck · ${s.title || s.session_id.slice(0, 8)}`;
  $("s-title").textContent = s.title || "(无标题)";
  $("s-badges").innerHTML = headerBadges(s);

  const actions = [];
  if (s.running) {
    actions.push('<span class="badge running">运行中·去那个窗口继续</span>');
    actions.push(`<button class="ghost-btn" id="btn-archive" title="事情做完了就封存:拷贝到归档区并标记已归档(唯一的备份途径)">封存</button>`);
  } else if (!s.source_missing) {
    actions.push(`<button class="ghost-btn primary" id="btn-resume">恢复 ▶</button>`);
    actions.push(`<button class="ghost-btn" id="btn-archive" title="事情做完了就封存:拷贝到归档区并标记已归档(唯一的备份途径)">封存</button>`);
  } else if (s.archived) {
    actions.push(`<button class="ghost-btn primary" id="btn-restore">还原到 projects</button>`);
  }
  actions.push(
    `<button class="ghost-btn" id="btn-cmd">复制命令</button>`,
    `<a class="ghost-btn" href="/api/sessions/${esc(SID)}/export" title="导出 Markdown">导出 MD</a>`
  );
  actions.push(
    `<label class="ghost-btn sys-toggle"><input type="checkbox" id="sys-toggle"> 系统事件</label>`
  );
  $("s-actions").innerHTML = actions.join("");
  $("btn-cmd").addEventListener("click", async () => {
    const res = await api(`/api/sessions/${SID}/command`);
    await copyText(res.command, "resume 命令已复制");
    if (res.note) setTimeout(() => toast(res.note, 4000), 900);
  });
  $("btn-resume")?.addEventListener("click", (e) => {
    e.target.disabled = true;
    resumeSession(SID).finally(() => { e.target.disabled = false; });
  });
  $("btn-archive")?.addEventListener("click", async () => {
    const res = await api(`/api/sessions/${SID}/archive`, { json: {} });
    toast(`已封存(伴生文件 ${res.companion_copied} 个)`);
  });
  $("btn-restore")?.addEventListener("click", async () => {
    if (!window.confirm("把归档副本还原回 projects 目录,使 resume 重新可用?")) return;
    const res = await api(`/api/sessions/${SID}/restore`, { json: { confirm: true } });
    toast("已还原");
    setTimeout(() => toast(res.note, 6000), 1200);
    setTimeout(() => location.reload(), 2200);
  });
  $("sys-toggle").addEventListener("change", async (e) => {
    state.showSystem = e.target.checked;
    await initialLoad();
  });

  const tok = (s.in_tokens || 0) + (s.out_tokens || 0);
  const meta = [
    s.cwd && `<span class="path" title="${esc(s.cwd)}">${esc(s.cwd)}</span>`,
    `${esc(fullTime(s.first_ts))} → ${esc(fullTime(s.last_ts))}`,
    `${s.msg_count} 条`,
    fmtBytes(s.file_size),
    tok ? `输出 ${(s.out_tokens || 0).toLocaleString()} tok` : null,
    s.version,
    `<span class="sid" data-copy="${esc(s.session_id)}">${esc(s.session_id)}</span>`,
  ].filter(Boolean);
  $("s-meta").innerHTML = meta.map((m) => `<span>${m}</span>`).join("");

  if (det.plan) {
    $("s-meta").insertAdjacentHTML(
      "beforeend",
      `<span title="${esc(det.plan.path)}">plan: ${esc(det.plan.slug)}</span>`
    );
  }

  $("s-subagents").innerHTML = (det.subagents || [])
    .map(
      (a) => `
      <button class="agent-chip" data-agent="${esc(a.agent_id)}" title="${esc(a.description || "")}">
        <span class="mono">${esc(a.agent_type || "agent")}</span> ${esc((a.description || a.agent_id).slice(0, 30))}
        <i class="mono">${esc(fmtBytes(a.file_size))}</i>
      </button>`
    )
    .join("");
}

// ---------- 子 agent 抽屉 ----------
async function openDrawer(agentId, label) {
  $("drawer-title").textContent = label;
  $("drawer-body").innerHTML = '<div class="empty">解析子对话…</div>';
  $("drawer").hidden = false;
  requestAnimationFrame(() => $("drawer").classList.add("open"));
  try {
    const win = await api(`/api/sessions/${SID}/subagents/${agentId}/messages`);
    renderWindowInto($("drawer-body"), win);
  } catch {
    $("drawer-body").innerHTML = '<div class="empty">子对话读取失败(源可能已清理且未归档)。</div>';
  }
}
function closeDrawer() {
  $("drawer").classList.remove("open");
  setTimeout(() => { $("drawer").hidden = true; }, 200);
}

// ---------- 装配 ----------
initHeader(null);
if (!SID) {
  $("s-title").textContent = "缺少 sid 参数";
} else {
  api(`/api/sessions/${SID}`).then(renderHeader).then(initialLoad);
}
$("load-earlier").addEventListener("click", loadEarlier);
$("load-later").addEventListener("click", loadLater);
$("drawer-close").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
document.addEventListener("click", (e) => {
  const chip = e.target.closest("[data-agent]");
  if (chip) { openDrawer(chip.dataset.agent, chip.textContent.trim()); return; }
  const cp = e.target.closest("[data-copy]");
  if (cp) copyText(cp.dataset.copy, "已复制");
});
