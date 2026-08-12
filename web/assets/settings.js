// 设置:config 读写 / 索引重建 / 项目 purge(官方命令,先强制归档)。
import { api, armConfirm, esc, fmtBytes, initHeader, toast } from "./common.js";

const $ = (id) => document.getElementById(id);
const FIELDS = [
  "archive_dir", "scan_interval_seconds", "archive_quiet_minutes",
  "live_poll_ms", "port", "claude_exe", "index_thinking", "index_tool_results",
];

async function loadConfig() {
  const cfg = await api("/api/config");
  for (const f of FIELDS) {
    const el = $(`cf-${f}`);
    if (el.type === "checkbox") el.checked = !!cfg[f];
    else el.value = cfg[f];
  }
  $("cfg-info").textContent = `claude_home: ${cfg.claude_home}`;
  $("db-path").textContent = `${cfg.data_dir}\\claudedeck.db`;
}

async function save() {
  const body = {};
  for (const f of FIELDS) {
    const el = $(`cf-${f}`);
    if (el.type === "checkbox") body[f] = el.checked;
    else if (el.type === "number") body[f] = Number(el.value);
    else body[f] = el.value;
  }
  const res = await api("/api/config", { method: "PUT", json: body });
  toast(res.changed.length ? `已保存:${res.changed.join(", ")}` : "无改动");
  if (res.note) setTimeout(() => toast(res.note, 6000), 1200);
}

async function loadProjects() {
  const res = await api("/api/projects");
  const sel = $("purge-project");
  sel.innerHTML = '<option value="">选择要清除的项目…</option>';
  for (const p of res.items) {
    if (!p.cwd) continue;
    const opt = document.createElement("option");
    opt.value = p.cwd;
    opt.textContent = `${p.cwd}(${p.sessions} 会话,${fmtBytes(p.bytes)})`;
    sel.appendChild(opt);
  }
}

initHeader("settings");
loadConfig();
loadProjects();
$("save").addEventListener("click", save);

armConfirm($("rebuild"), "确认重建?", async () => {
  const res = await api("/api/index/rebuild", { json: { confirm: true } });
  toast(res.note);
});

armConfirm($("purge"), "确认 purge?", async () => {
  const path = $("purge-project").value;
  const name = $("purge-confirm").value.trim();
  if (!path) { toast("先选择项目"); return; }
  if (!name) { toast("输入项目目录名确认"); return; }
  try {
    const res = await api("/api/projects/purge", { json: { path, confirm_name: name } });
    toast(`purge 完成,已先归档 ${res.archived_before_purge} 个会话`);
    const out = $("purge-out");
    out.style.display = "block";
    out.textContent = res.stdout || "(官方命令无输出)";
    loadProjects();
  } catch (e) {
    /* api() 已 toast */
  }
});
