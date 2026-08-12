// ClaudeDeck 公共工具:fetch 封装/toast/轮询器/格式化/顶栏组件。

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

export async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { Accept: "application/json" }, ...opts });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch { /* 保留 statusText */ }
    if (!opts.silent) toast(`请求失败:${msg}`);
    throw new Error(msg);
  }
  return r.json();
}

let toastTimer = null;
export function toast(text, ms = 2600) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ms);
}

// visibilitychange 感知的轮询器:页面切后台即暂停,回来立即刷一次。
export function poll(fn, ms) {
  let stopped = false;
  const tick = async () => {
    if (stopped || document.hidden) return;
    try { await fn(); } catch { /* 单次失败静默,下一轮再试 */ }
  };
  const timer = setInterval(tick, ms);
  const onVis = () => { if (!document.hidden) tick(); };
  document.addEventListener("visibilitychange", onVis);
  tick();
  return () => { stopped = true; clearInterval(timer); document.removeEventListener("visibilitychange", onVis); };
}

export function fmtBytes(n) {
  n = n || 0;
  if (n < 1024) return `${n}B`;
  const units = ["KB", "MB", "GB"];
  let v = n;
  for (const u of units) {
    v /= 1024;
    if (v < 1024 || u === "GB") return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)}${u}`;
  }
}

const pad = (x) => String(x).padStart(2, "0");

export function fullTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 7 * 86400) return `${Math.floor(diff / 86400)} 天前`;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export async function copyText(s, doneMsg = "已复制") {
  try {
    await navigator.clipboard.writeText(s);
    toast(doneMsg);
  } catch {
    toast("复制失败:浏览器未授予剪贴板权限");
  }
}

export function navHtml(active) {
  const items = [
    ["/", "会话", "index"],
    ["/live.html", "看板", "live"],
  ];
  // 后续阶段就位后加入:["/archive.html","归档","archive"],["/settings.html","设置","settings"]
  return items
    .map(([href, label, key]) => `<a href="${href}" class="${key === active ? "active" : ""}">${label}</a>`)
    .join("");
}

export function initHeader(active) {
  const nav = document.getElementById("nav");
  if (nav) nav.innerHTML = navHtml(active);

  const rescan = document.getElementById("rescan");
  if (rescan) {
    rescan.addEventListener("click", async () => {
      const res = await api("/api/index/scan", { method: "POST" });
      toast(res.started ? "已触发扫描" : "扫描已在进行中");
    });
  }

  const bar = document.getElementById("scanbar");
  if (bar) {
    const fill = bar.querySelector(".scanbar-fill");
    const label = bar.querySelector(".scanbar-label");
    poll(async () => {
      const st = await api("/api/index/status", { silent: true });
      if (st.phase === "scanning") {
        bar.hidden = false;
        const pct = st.files_total ? Math.round((st.files_done / st.files_total) * 100) : 0;
        fill.style.width = `${pct}%`;
        label.textContent = `索引中 ${st.files_done}/${st.files_total}`;
      } else {
        bar.hidden = true;
        fill.style.width = "0%";
      }
    }, 1500);
  }

  initAnnunciator();
}

// 通告牌灯条:S3 上线 /api/live 后自动点亮;此前探测 404 保持隐藏。
async function initAnnunciator() {
  const el = document.getElementById("annunciator");
  if (!el) return;
  let probe;
  try {
    probe = await fetch("/api/live");
  } catch {
    return;
  }
  if (!probe.ok) return;

  const render = (data) => {
    const cells = (data.sessions || []).map((s) => {
      const lamp = s.status === "waiting" ? "waiting" : s.status === "busy" ? "busy" : "";
      const name = esc(s.name || s.session_id?.slice(0, 8) || "?");
      const title = esc(`${s.cwd || ""} · ${s.status === "waiting" ? "等待你" : s.status}`);
      return `<span class="ann-cell" title="${title}"><span class="lamp ${lamp}"></span>${name}</span>`;
    });
    el.innerHTML = cells.join("");
    el.hidden = cells.length === 0;
  };
  render(await probe.json());
  poll(async () => render(await api("/api/live", { silent: true })), 2000);
}
