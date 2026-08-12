// 状态看板:运行中窗口(2s)/后台作业(10s)/用量曲线/磁盘/plans。
import { api, copyText, esc, fmtBytes, fmtTime, fullTime, initHeader, poll, reveal } from "./common.js";

let winRevealed = false;
let jobsRevealed = false;
import { lineChart } from "./chart.js";

const $ = (id) => document.getElementById(id);
const SERIES = { tokens: "#b8801d", cost: "#3f92d4" }; // dataviz 验证器全项通过(暗面)

function fmtDur(s) {
  if (s == null) return "";
  s = Math.floor(s);
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
  return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
}

// ---------- 运行中窗口 ----------
function statusText(s) {
  if (!s.alive) return `<span class="st dead">失效(${esc(s.alive_check)})</span>`;
  if (s.status === "waiting") return `<span class="st waiting">等待你 ${esc(fmtDur(s.status_seconds))}</span>`;
  if (s.status === "busy") return `<span class="st busy">忙碌 ${esc(fmtDur(s.status_seconds))}</span>`;
  return `<span class="st idle">空闲 ${esc(fmtDur(s.status_seconds))}</span>`;
}

function lampClass(s) {
  if (!s.alive) return "dead";
  if (s.status === "waiting") return "waiting";
  if (s.status === "busy") return "busy";
  return "";
}

function renderWindows(data) {
  $("degraded").hidden = !data.degraded;
  $("stale-count").textContent = data.stale_count;
  const list = data.sessions;
  if (!list.length) {
    $("windows").innerHTML = '<div class="empty">当前没有运行中的 Claude Code 窗口。</div>';
    return;
  }
  $("windows").innerHTML = list
    .map((s) => {
      const cardMod =
        s.alive && s.status === "waiting" ? "is-waiting" : s.alive && s.status === "busy" ? "is-busy" : "";
      return `
      <div class="win-card ${cardMod}">
        <div class="win-top">
          <span class="lamp ${lampClass(s)}"></span>
          <span class="win-name mono">${esc(s.name || "?")}</span>
          ${s.kind === "bg" ? '<span class="badge bg" title="daemon 驻留的后台会话,没有窗口;用 claude agents 管理或在后台作业区处理">后台驻留</span>' : ""}
          ${statusText(s)}
        </div>
        ${s.title ? `<div class="win-title" title="${esc(s.title)}">${esc(s.title)}</div>` : ""}
        <div class="win-meta mono">
          <span class="path" title="${esc(s.cwd || "")}">${esc(s.cwd || "")}</span>
        </div>
        <div class="win-foot mono">
          pid ${esc(String(s.pid ?? "?"))} · ${esc(s.version || "")}
          ${s.bridge_url ? ` · <a href="${esc(s.bridge_url)}" target="_blank" rel="noopener">网页 ↗</a>` : ""}
        </div>
      </div>`;
    })
    .join("");
  if (!winRevealed) { reveal($("windows")); winRevealed = true; }
}

// ---------- 后台作业 ----------
function renderJobs(data) {
  if (!data.jobs.length) {
    $("jobs").innerHTML = '<div class="empty">无后台作业。</div>';
    return;
  }
  $("jobs").innerHTML = data.jobs
    .map((j) => {
      const blocked = j.state === "blocked";
      return `
      <div class="job-card ${blocked ? "is-blocked" : ""}">
        <div class="win-top">
          <span class="mono job-state ${blocked ? "bad" : ""}">${esc(j.state || "?")}</span>
          <span class="win-name">${esc(j.name || j.id)}</span>
          <span class="job-time mono" title="${esc(fullTime(j.updated_at))}">${esc(fmtTime(j.updated_at))}</span>
        </div>
        ${blocked && j.needs ? `<div class="job-needs">需要你:${esc(j.needs)}</div>` : ""}
        <div class="win-meta mono">
          <span class="path">${esc(j.cwd || "")}</span>
          ${j.tokens ? `· ${esc(String(j.tokens))} tok` : ""}
          ${j.fork_parent_session_id ? `· fork 自 <span class="sid" data-copy="${esc(j.fork_parent_session_id)}">${esc(j.fork_parent_session_id.slice(0, 8))}</span>` : ""}
        </div>
      </div>`;
    })
    .join("");
  if (!jobsRevealed) { reveal($("jobs")); jobsRevealed = true; }
}

// ---------- 用量曲线 ----------
const charts = { tokens: null, cost: null };
let lastCurves = null;

function drawCharts(data) {
  lastCurves = data;
  charts.tokens?.destroy();
  charts.cost?.destroy();

  const usagePts = data.usage.map((r) => ({
    date: r.date,
    value: r.out_tokens || 0,
    extra: r,
  }));
  charts.tokens = lineChart($("c-tokens"), $("tip-tokens"), {
    points: usagePts,
    color: SERIES.tokens,
    fmtY: (v) => (v >= 1000 ? `${Math.round(v / 1000)}k` : String(Math.round(v))),
    fmtTip: (p) =>
      `${esc(p.date)}<br>输出 ${p.value.toLocaleString()}<br>` +
      `输入 ${(p.extra.in_tokens || 0).toLocaleString()} · 缓存读 ${(p.extra.cache_read || 0).toLocaleString()}`,
  });

  const costPts = data.cost.map((r) => ({ date: r.date, value: r.cost_usd || 0, extra: r }));
  charts.cost = lineChart($("c-cost"), $("tip-cost"), {
    points: costPts,
    color: SERIES.cost,
    fmtY: (v) => `$${v >= 10 ? Math.round(v) : v.toFixed(1)}`,
    fmtTip: (p) => `${esc(p.date)}<br>$${p.value.toFixed(2)} · ${(p.extra.tokens || 0).toLocaleString()} tok`,
  });

  $("tbl-tokens").innerHTML = tableHtml(usagePts, (p) => p.value.toLocaleString());
  $("tbl-cost").innerHTML = tableHtml(costPts, (p) => `$${p.value.toFixed(2)}`);
}

function tableHtml(points, fmt) {
  if (!points.length) return '<div class="empty">窗口期内无记录</div>';
  const rows = [...points]
    .reverse()
    .map((p) => `<tr><td>${esc(p.date)}</td><td>${esc(fmt(p))}</td></tr>`)
    .join("");
  return `<table>${rows}</table>`;
}

async function loadCurves() {
  drawCharts(await api(`/api/stats/tokens?days=${$("days").value}`));
}

// ---------- 磁盘 ----------
function barRow(label, bytes, maxBytes, extra = "") {
  const pct = maxBytes ? Math.max(1, Math.round((bytes / maxBytes) * 100)) : 0;
  return `
  <div class="bar-row">
    <span class="bar-label mono" title="${esc(label)}">${esc(label)}</span>
    <span class="bar-track"><span class="bar-fill" style="width:${pct}%"></span></span>
    <span class="bar-val mono">${esc(fmtBytes(bytes))}${extra}</span>
  </div>`;
}

async function loadDisk() {
  const d = await api("/api/stats/disk");
  $("disk-total").textContent = `~/.claude 共 ${fmtBytes(d.total_bytes)} · 归档区 ${fmtBytes(d.archive.bytes)}`;
  const maxDir = Math.max(...d.dirs.map((x) => x.bytes), 1);
  $("disk-dirs").innerHTML =
    d.dirs.slice(0, 10).map((x) => barRow(x.name, x.bytes, maxDir, ` <i>${x.files}个</i>`)).join("") +
    barRow("(顶层文件)", d.top_level_files_bytes, maxDir) +
    `<div class="bar-row archive-row">${barRow("归档区", d.archive.bytes, maxDir, ` <i>${d.archive.files}个</i>`)}</div>`;
  const maxProj = Math.max(...(d.projects || []).map((x) => x.bytes), 1);
  $("disk-projects").innerHTML = (d.projects || [])
    .slice(0, 12)
    .map((p) =>
      barRow(p.cwd || "(未知)", p.bytes, maxProj, ` <i>${p.sessions}会话</i>`)
    )
    .join("") || '<div class="empty">索引尚未就绪</div>';
}

// ---------- plans ----------
async function loadPlans() {
  const res = await api("/api/plans");
  if (!res.items.length) {
    $("plans").innerHTML = '<div class="empty">~/.claude/plans 下没有计划文件。</div>';
    return;
  }
  $("plans").innerHTML = res.items
    .map((p) => {
      const links = p.sessions
        .map(
          (s) =>
            `<a href="/session.html?sid=${esc(s.session_id)}" title="${esc(s.title || "")}">${esc(
              (s.title || s.session_id.slice(0, 8)).slice(0, 24)
            )}</a>`
        )
        .join("、");
      return `
      <div class="plan-row">
        <span class="mono plan-slug" title="${esc(p.slug)}">${esc(p.slug)}</span>
        <span class="mono plan-meta">${esc(fmtTime(p.mtime))} · ${esc(fmtBytes(p.bytes))}</span>
        <span class="plan-links">${links || '<span class="mono plan-meta">无关联会话</span>'}</span>
      </div>`;
    })
    .join("");
}

// ---------- 装配 ----------
initHeader("live");

let showStale = false;
$("show-stale").addEventListener("change", (e) => {
  showStale = e.target.checked;
  refreshWindows();
});
async function refreshWindows() {
  renderWindows(await api(`/api/live?show_stale=${showStale}`, { silent: true }));
}
poll(refreshWindows, 2000);
poll(async () => renderJobs(await api("/api/jobs", { silent: true })), 10000);

$("days").addEventListener("change", loadCurves);
document.querySelectorAll(".chart-toggle").forEach((btn) => {
  btn.addEventListener("click", () => {
    const key = btn.dataset.chart;
    const tbl = $(`tbl-${key}`);
    const cv = $(`c-${key}`);
    const showTable = tbl.hidden;
    tbl.hidden = !showTable;
    cv.style.display = showTable ? "none" : "";
    btn.textContent = showTable ? "曲线" : "表格";
  });
});
window.addEventListener("resize", () => { if (lastCurves) drawCharts(lastCurves); });

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-copy]");
  if (el) copyText(el.dataset.copy, "已复制");
});

loadCurves();
loadDisk();
loadPlans();
