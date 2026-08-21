// ClaudeDeck 公共工具:fetch 封装/toast/轮询器/格式化/顶栏组件。

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

export async function api(path, opts = {}) {
  const init = { headers: { Accept: "application/json" }, ...opts };
  if (opts.json !== undefined) {
    init.method = opts.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.json);
  }
  const r = await fetch(path, init);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail ?? detail; } catch { /* 保留 statusText */ }
    const msg = typeof detail === "string" ? detail : detail.message || JSON.stringify(detail);
    if (!opts.silent) toast(`请求失败:${msg}`);
    const err = new Error(msg);
    err.detail = detail;
    err.status = r.status;
    throw err;
  }
  return r.json();
}

// 一键恢复:cwd 缺失时确认后降级到用户主目录打开。
export async function resumeSession(sid, { fork = false } = {}) {
  try {
    const res = await api(`/api/sessions/${sid}/resume`, { json: { fork }, silent: true });
    toast(`已拉起新窗口(${res.used_wt ? "WT 标签" : "独立控制台"})`);
    setTimeout(() => toast(res.note, 5000), 1200);
    return res;
  } catch (e) {
    if (e.detail?.code === "cwd_missing") {
      const go = window.confirm(`${e.detail.message}\n\n目录: ${e.detail.cwd}\n\n改在用户主目录打开?`);
      if (go) {
        const res = await api(`/api/sessions/${sid}/resume`, { json: { fork, use_home_fallback: true } });
        toast("已在用户主目录拉起(跨目录 resume)");
        setTimeout(() => toast(res.note, 5000), 1200);
        return res;
      }
      return null;
    }
    toast(`恢复失败:${e.message}`, 4000);
    throw e;
  }
}

// 危险按钮两段式确认:第一次点击进入武装态,3 秒内再点才执行。
export function armConfirm(btn, armedLabel, fn) {
  let armed = false, timer = null;
  const original = btn.textContent;
  btn.addEventListener("click", async () => {
    if (!armed) {
      armed = true;
      btn.classList.add("danger-armed");
      btn.textContent = armedLabel;
      timer = setTimeout(() => {
        armed = false;
        btn.classList.remove("danger-armed");
        btn.textContent = original;
      }, 3000);
      return;
    }
    clearTimeout(timer);
    armed = false;
    btn.classList.remove("danger-armed");
    btn.textContent = original;
    await fn();
  });
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
// ---------- 标签就地编辑器(body 级浮层,免疫容器的轮询重渲染) ----------
let _tagEditor = null;

export function closeTagEditor() {
  _tagEditor?.remove();
  _tagEditor = null;
}

export function openTagEditor(anchor, sid, cur) {
  closeTagEditor();
  const r = anchor.getBoundingClientRect();
  const wrap = document.createElement("div");
  wrap.className = "tag-editor";
  wrap.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - 248))}px`;
  wrap.style.top = `${r.bottom + 6}px`;
  wrap.innerHTML = `<input class="tag-input" maxlength="60" placeholder="起个名字,留空=清除,回车保存">`;
  document.body.appendChild(wrap);
  _tagEditor = wrap;
  const input = wrap.querySelector("input");
  input.value = cur || "";
  input.focus();
  input.select();
  const save = async () => {
    const v = input.value;
    closeTagEditor();
    await api(`/api/sessions/${sid}/tag`, { json: { tag: v }, method: "PUT" });
    toast(v.trim() ? `已命名:${v.trim().slice(0, 60)}` : "已清除标签");
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") save();
    else if (e.key === "Escape") closeTagEditor();
  });
  input.addEventListener("blur", () => setTimeout(closeTagEditor, 150));
}

// 点击委托(灯条与看板卡共用):铅笔=就地改名,其余带 data-focus-sid 的=聚焦
document.addEventListener("click", async (e) => {
  const ed = e.target.closest("[data-tagedit-sid]");
  if (ed) {
    openTagEditor(ed, ed.dataset.tageditSid, ed.dataset.tageditCur);
    return;
  }
  const el = e.target.closest("[data-focus-sid]");
  if (!el) return;
  try {
    await api(`/api/live/${el.dataset.focusSid}/focus`, { json: {} });
  } catch { /* api() 已 toast 错误 */ }
});

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
    ["/archive.html", "归档", "archive"],
    ["/settings.html", "设置", "settings"],
  ];
  return items
    .map(([href, label, key]) => `<a href="${href}" class="${key === active ? "active" : ""}">${label}</a>`)
    .join("");
}

const REDUCED_MOTION = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

const _revealObserver =
  !REDUCED_MOTION && "IntersectionObserver" in window
    ? new IntersectionObserver(
        (entries) => {
          for (const e of entries) {
            if (e.isIntersecting) {
              e.target.classList.add("in");
              _revealObserver.unobserve(e.target);
            }
          }
        },
        { rootMargin: "0px 0px -4% 0px" }
      )
    : null;

// 容器子元素入场渐入(滚入视口才触发,批内 36ms 阶梯)。轮询重绘的区域只在首次调用。
export function reveal(container) {
  if (!_revealObserver || !container) return;
  let i = 0;
  for (const el of container.children) {
    if (el.classList.contains("reveal")) continue;
    el.classList.add("reveal");
    el.style.transitionDelay = `${(i++ % 8) * 36}ms`;
    _revealObserver.observe(el);
  }
}

// 顶栏随滚轮隐现:下滚累计 90px 淡出,任意上滚立即淡回。
function initScrollHide() {
  if (REDUCED_MOTION) return;
  const header = document.querySelector(".deck-header");
  if (!header) return;
  let last = window.scrollY;
  let acc = 0;
  window.addEventListener(
    "scroll",
    () => {
      const y = window.scrollY;
      const delta = y - last;
      last = y;
      if (y < 80) {
        header.classList.remove("nav-hidden");
        acc = 0;
        return;
      }
      if (delta > 0) {
        acc += delta;
        if (acc > 90) header.classList.add("nav-hidden");
      } else {
        acc = 0;
        header.classList.remove("nav-hidden");
      }
    },
    { passive: true }
  );
}

export function initHeader(active) {
  const nav = document.getElementById("nav");
  if (nav) nav.innerHTML = navHtml(active);
  initScrollHide();

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
      const bg = s.kind === "bg" ? " bg" : "";
      const label = esc(s.tag || s.name || s.session_id?.slice(0, 8) || "?");
      const state = s.status === "waiting" ? "等待你" : s.status;
      const focusAttr = s.kind === "bg" ? "" : ` data-focus-sid="${esc(s.session_id || "")}"`;
      const title = esc(
        `${s.tag ? `${s.name} · ` : ""}${s.cwd || ""} · ${state}` +
          (s.kind === "bg" ? " · 后台驻留(无窗口)" : " · 点击聚焦到该窗口")
      );
      const pencil = `<button class="ann-edit" data-tagedit-sid="${esc(s.session_id || "")}" data-tagedit-cur="${esc(s.tag || "")}" title="重命名(只影响 ClaudeDeck/CCTopBar 显示)">🖊</button>`;
      return `<span class="ann-cell${bg}"${focusAttr} title="${title}"><span class="lamp ${lamp}"></span>${label}${pencil}</span>`;
    });
    el.innerHTML = cells.join("");
    el.hidden = cells.length === 0;
  };
  render(await probe.json());
  poll(async () => render(await api("/api/live", { silent: true })), 2000);
}
