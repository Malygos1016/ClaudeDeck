// 归档管理:归档区概况 + 源已清理会话的浏览/还原。
import { api, esc, fmtBytes, fmtTime, fullTime, initHeader, reveal, toast } from "./common.js";

const $ = (id) => document.getElementById(id);

async function loadInfo() {
  const [disk, st] = await Promise.all([api("/api/stats/disk"), api("/api/index/status")]);
  const last = st.finished_at ? fmtTime(st.finished_at) : "尚未完成过";
  $("arc-info").innerHTML = `
    <div class="row-meta" style="font-size:12.5px">
      <span class="mono">${esc(disk.archive.path)}</span>
      <span>${esc(fmtBytes(disk.archive.bytes))} · ${disk.archive.files} 个文件</span>
      <span>上次归档扫描:${esc(last)}</span>
    </div>`;
}

function rowHtml(s) {
  return `
  <div class="session-row" data-sid="${esc(s.session_id)}">
    <div class="row-top">
      <a class="row-title" href="/session.html?sid=${esc(s.session_id)}" title="${esc(s.title || "")}">${esc(s.title || "(无标题)")}</a>
      <span class="badge missing">源已清理</span>
      <span class="row-actions">
        ${s.archived ? `<button class="ghost-btn primary" data-restore="${esc(s.session_id)}">还原</button>` : '<span class="badge">无归档副本</span>'}
      </span>
    </div>
    <div class="row-meta">
      ${s.cwd ? `<span class="path" title="${esc(s.cwd)}">${esc(s.cwd)}</span>` : ""}
      <span title="${esc(fullTime(s.last_ts))}">${esc(fmtTime(s.last_ts))}</span>
      ${s.file_size ? `<span>${esc(fmtBytes(s.file_size))}</span>` : ""}
      <span class="sid">${esc(s.session_id.slice(0, 8))}</span>
    </div>
  </div>`;
}

async function loadMissing() {
  const res = await api("/api/sessions?archived=missing&page_size=200&sort=last_ts");
  $("missing-count").textContent = `${res.total} 个`;
  if (!res.items.length) {
    $("missing-list").innerHTML =
      '<div class="empty">没有"源已被清理"的会话——当前所有会话的源文件都还在。</div>';
    return;
  }
  $("missing-list").innerHTML = res.items.map(rowHtml).join("");
  reveal($("missing-list"));
}

let pendingRestore = null;
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-restore]");
  if (!btn) return;
  const sid = btn.dataset.restore;
  if (pendingRestore !== sid) {
    pendingRestore = sid;
    btn.textContent = "确认还原?";
    btn.classList.add("danger-armed");
    setTimeout(() => {
      if (pendingRestore === sid) {
        pendingRestore = null;
        btn.textContent = "还原";
        btn.classList.remove("danger-armed");
      }
    }, 3000);
    return;
  }
  pendingRestore = null;
  btn.disabled = true;
  try {
    const res = await api(`/api/sessions/${sid}/restore`, { json: { confirm: true } });
    toast("已还原回 projects,可以 resume 了");
    setTimeout(() => toast(res.note, 6000), 1200);
    await loadMissing();
  } catch {
    btn.disabled = false;
    btn.textContent = "还原";
    btn.classList.remove("danger-armed");
  }
});

initHeader("archive");
loadInfo();
loadMissing();
