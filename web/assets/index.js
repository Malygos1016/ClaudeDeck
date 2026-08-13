// 会话列表 + 全文搜索页。
import { api, copyText, esc, fmtBytes, fmtTime, fullTime, initHeader, resumeSession, reveal, toast } from "./common.js";

const state = { q: "", project: "", provider: "all", archived: "all", sort: "last_ts", order: "desc", page: 1 };
const $ = (id) => document.getElementById(id);

// 固定四列插槽:归档列 | 运行列 | 恢复 | 复制命令。
// 列职责单一(用户拍板):归档列只放归档态;运行列只放 运行中/已停止,悬浮显示已停止多久。
function sealSlot(s) {
  if (s.source_missing) return '<span class="badge missing" title="源 transcript 已被官方清理,仅存封存副本">源已清理</span>';
  if (s.archived) return '<span class="badge archived" title="已手动封存到归档区">已归档</span>';
  return '<span class="slot-empty"></span>';
}

function stoppedFor(ts) {
  const ms = Date.now() - new Date(ts).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "";
  const m = Math.floor(ms / 60000);
  if (m < 60) return `${m} 分钟`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} 小时`;
  return `${Math.floor(h / 24)} 天`;
}

function runSlot(s) {
  if (s.provider === "codex") return '<span class="slot-empty"></span>'; // codex 无运行注册表
  if (s.running) return '<span class="badge running">运行中</span>';
  return `<span class="badge stopped" title="已停止 ${esc(stoppedFor(s.last_ts))}">已停止</span>`;
}

function metaLine(s) {
  const parts = [];
  if (s.cwd) parts.push(`<span class="path" title="${esc(s.cwd)}">${esc(s.cwd)}</span>`);
  parts.push(`<span title="${esc(fullTime(s.last_ts))}">${esc(fmtTime(s.last_ts))}</span>`);
  if (s.file_size) parts.push(esc(fmtBytes(s.file_size)));
  if (s.msg_count) parts.push(`${s.msg_count} 条`);
  parts.push(
    `<span class="sid" data-copy="${esc(s.session_id)}" title="点击复制完整 session id">${esc(
      (s.session_id || "").slice(0, 8)
    )}</span>`
  );
  return parts.join('<span aria-hidden="true">·</span>');
}

function actionsHtml(s) {
  const resume =
    !s.source_missing && !s.running
      ? `<button class="ghost-btn primary" data-resume-sid="${esc(s.session_id)}" title="新开 Windows Terminal 标签 resume 此会话">恢复 ▶</button>`
      : '<span class="slot-empty"></span>';
  const copy = `<button class="ghost-btn" data-cmd-sid="${esc(s.session_id)}" title="复制在原目录 resume 的完整命令">复制命令</button>`;
  return sealSlot(s) + runSlot(s) + resume + copy;
}

function rowHtml(s, extra = "") {
  return `
  <div class="session-row" data-sid="${esc(s.session_id)}">
    <div class="row-top">
      ${s.provider === "codex" ? '<span class="badge prov-codex" title="Codex CLI 会话(~/.codex/sessions)">codex</span>' : ""}
      <a class="row-title" href="/session.html?sid=${esc(s.session_id)}" title="${esc(s.title || "")}">${esc(s.title || "(无标题)")}</a>
      <span class="row-actions acts-grid">${actionsHtml(s)}</span>
    </div>
    <div class="row-meta">${metaLine(s)}</div>
    ${extra}
  </div>`;
}

function renderList(res) {
  $("notice").hidden = true;
  $("count").textContent = `${res.total} 个会话`;
  if (!res.items.length) {
    $("results").innerHTML =
      '<div class="empty">没有匹配的会话。清空筛选试试,或点右上「重新扫描」重建索引。</div>';
    $("pager").innerHTML = "";
    return;
  }
  $("results").innerHTML = res.items.map((s) => rowHtml(s)).join("");
  reveal($("results"));
  renderPager(res.total);
}

function renderSearch(res) {
  const n = $("notice");
  if (res.fallback) {
    n.textContent = "关键词只有 1-2 个字,已改用模糊匹配(较慢,建议 3 字以上走全文索引)。";
    n.hidden = false;
  } else {
    n.hidden = true;
  }
  $("count").textContent = `${res.total_hits} 条命中 / ${res.groups.length} 个会话`;
  if (!res.groups.length) {
    $("results").innerHTML = `<div class="empty">没有匹配「${esc(state.q)}」的内容——换个词,或去掉项目筛选。</div>`;
    $("pager").innerHTML = "";
    return;
  }
  $("results").innerHTML = res.groups
    .map((g) => {
      const hits = g.hits
        .map(
          (h) =>
            `<a class="hit" href="/session.html?sid=${esc(g.session.session_id)}${h.seq >= 0 ? `&seq=${h.seq}` : ""}">` +
            `<span class="kind">${esc(h.kind)}</span>${h.snippet_html}</a>`
        )
        .join("");
      return `<div class="hit-group">${rowHtml(g.session, `<div class="hits">${hits}</div>`)}</div>`;
    })
    .join("");
  reveal($("results"));
  $("pager").innerHTML = "";
}

function renderPager(total) {
  const pages = Math.max(1, Math.ceil(total / 50));
  if (pages <= 1) {
    $("pager").innerHTML = "";
    return;
  }
  $("pager").innerHTML = `
    <button class="ghost-btn" id="pg-prev" ${state.page <= 1 ? "disabled" : ""}>‹ 上一页</button>
    <span>${state.page} / ${pages}</span>
    <button class="ghost-btn" id="pg-next" ${state.page >= pages ? "disabled" : ""}>下一页 ›</button>`;
  $("pg-prev")?.addEventListener("click", () => { state.page--; refresh(); });
  $("pg-next")?.addEventListener("click", () => { state.page++; refresh(); });
}

async function refresh() {
  const common = new URLSearchParams();
  if (state.project) common.set("project", state.project);
  if (state.q.trim()) {
    common.set("q", state.q.trim());
    renderSearch(await api(`/api/search?${common}`));
  } else {
    common.set("provider", state.provider);
    common.set("archived", state.archived);
    common.set("sort", state.sort);
    common.set("order", state.order);
    common.set("page", String(state.page));
    renderList(await api(`/api/sessions?${common}`));
  }
}

async function loadProjects() {
  const res = await api("/api/projects", { silent: true });
  const sel = $("f-project");
  for (const p of res.items) {
    if (!p.cwd) continue;
    const opt = document.createElement("option");
    opt.value = p.cwd;
    opt.textContent = `${p.cwd}(${p.sessions})`;
    sel.appendChild(opt);
  }
}

function bind() {
  const q = $("q");
  let composing = false; // 中文输入法组词期间不触发
  q.addEventListener("compositionstart", () => { composing = true; });
  q.addEventListener("compositionend", () => { composing = false; });
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !composing) {
      state.q = q.value;
      state.page = 1;
      refresh();
    }
  });
  q.addEventListener("input", () => {
    if (q.value === "" && state.q !== "") {
      state.q = "";
      state.page = 1;
      refresh();
    }
  });

  $("f-project").addEventListener("change", (e) => { state.project = e.target.value; state.page = 1; refresh(); });
  $("f-archived").addEventListener("change", (e) => { state.archived = e.target.value; state.page = 1; refresh(); });
  $("f-provider")?.addEventListener("change", (e) => { state.provider = e.target.value; state.page = 1; refresh(); });
  $("f-sort").addEventListener("change", (e) => {
    [state.sort, state.order] = e.target.value.split(":");
    state.page = 1;
    refresh();
  });

  document.addEventListener("click", async (e) => {
    const copyEl = e.target.closest("[data-copy]");
    if (copyEl) { copyText(copyEl.dataset.copy, "session id 已复制"); return; }
    const resumeEl = e.target.closest("[data-resume-sid]");
    if (resumeEl) {
      resumeEl.disabled = true;
      try { await resumeSession(resumeEl.dataset.resumeSid); } finally { resumeEl.disabled = false; }
      return;
    }
    const cmdEl = e.target.closest("[data-cmd-sid]");
    if (cmdEl) {
      const res = await api(`/api/sessions/${cmdEl.dataset.cmdSid}/command`);
      await copyText(res.command, "resume 命令已复制,粘贴到任意终端执行");
      if (res.note) setTimeout(() => toast(res.note, 4000), 900);
    }
  });
}

initHeader("index");
bind();
loadProjects();
refresh();
