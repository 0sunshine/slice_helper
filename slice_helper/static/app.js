const state = { jobs: [], selectedJobId: null, detail: null, segments: [] };

const statusNames = {
  queued: "排队中", probing: "探测中", running: "处理中",
  pause_requested: "等待暂停", paused: "已暂停", completed: "已完成",
  failed: "失败", stop_requested: "等待停止", stopped: "已停止"
};

const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
})[character]);

function statusClass(status) {
  if (status === "completed") return "ok";
  if (["paused", "pause_requested", "queued"].includes(status)) return "warn";
  if (["failed", "stopped", "stop_requested"].includes(status)) return "bad";
  return "neutral";
}

function formatSeconds(value) {
  const total = Number(value || 0);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = (total % 60).toFixed(2).padStart(5, "0");
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${seconds}`;
}

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options
  });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try { message = (await response.json()).detail || message; } catch (_) { /* no-op */ }
    throw new Error(message);
  }
  return response.json();
}

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.hidden = false;
  window.setTimeout(() => { toast.hidden = true; }, 3500);
}

async function loadHealth() {
  const badge = $("healthBadge");
  try {
    const response = await fetch("/health/ready");
    badge.textContent = response.ok ? "服务就绪" : "依赖异常";
    badge.className = `health ${response.ok ? "ok" : "bad"}`;
  } catch (_) {
    badge.textContent = "连接失败";
    badge.className = "health bad";
  }
}

async function loadJobs() {
  const filter = $("statusFilter").value;
  const query = filter ? `?status=${encodeURIComponent(filter)}` : "";
  state.jobs = await api(`/api/jobs${query}`);
  renderJobs();
  if (state.selectedJobId) await loadDetail(state.selectedJobId, false);
}

function renderJobs() {
  const body = $("jobsBody");
  $("jobCount").textContent = state.jobs.length;
  $("emptyJobs").hidden = state.jobs.length !== 0;
  body.innerHTML = state.jobs.map((job) => `
    <tr>
      <td><span class="filename" title="${escapeHtml(job.source_path)}">${escapeHtml(job.source_path.split(/[\\/]/).pop())}</span><span class="muted mono">${escapeHtml(job.id.slice(0, 12))}</span></td>
      <td><span class="status-pill ${statusClass(job.status)}">${escapeHtml(statusNames[job.status] || job.status)}</span></td>
      <td><span class="mini-progress"><span style="width:${Math.max(0, Math.min(100, job.progress || 0))}%"></span></span>${Number(job.progress || 0).toFixed(1)}%</td>
      <td>${job.current_window}/${job.total_windows}</td>
      <td>${job.accepted_segment_count}</td>
      <td>${escapeHtml(formatDate(job.created_at))}</td>
      <td><button class="button secondary small view-job" data-job-id="${escapeHtml(job.id)}" type="button">查看</button></td>
    </tr>`).join("");
  body.querySelectorAll(".view-job").forEach((button) => button.addEventListener("click", () => loadDetail(button.dataset.jobId, true)));
}

async function loadDetail(jobId, scroll) {
  state.selectedJobId = jobId;
  const [detail, segments] = await Promise.all([
    api(`/api/jobs/${jobId}`),
    api(`/api/jobs/${jobId}/segments?acceptedOnly=${$("acceptedOnly").checked}`)
  ]);
  state.detail = detail;
  state.segments = segments;
  renderDetail();
  if (scroll) $("detailBand").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderDetail() {
  const { job, windows } = state.detail;
  $("detailBand").hidden = false;
  $("detailId").textContent = job.id;
  $("detailTitle").textContent = job.source_path.split(/[\\/]/).pop();
  $("detailPath").textContent = job.source_path;
  $("summaryStatus").innerHTML = `<span class="status-pill ${statusClass(job.status)}">${escapeHtml(statusNames[job.status] || job.status)}</span>`;
  $("summaryDuration").textContent = formatSeconds(job.source_duration);
  $("summaryWindow").textContent = `${job.current_window} / ${job.total_windows}`;
  $("summaryCutMode").textContent = job.cut_mode === "copy" ? "流复制" : "重编码";
  $("detailProgress").style.width = `${Math.max(0, Math.min(100, job.progress || 0))}%`;
  $("detailError").hidden = !job.error_message;
  $("detailError").textContent = job.error_message || "";
  $("detailWarnings").innerHTML = (job.warnings || []).map((warning) => `<p>${escapeHtml(warning)}</p>`).join("");
  renderActions(job);

  $("windowsBody").innerHTML = windows.map((windowItem) => `
    <tr>
      <td>${windowItem.window_index + 1}</td>
      <td>${formatSeconds(windowItem.requested_start)}</td>
      <td>${formatSeconds(windowItem.nominal_end)}</td>
      <td><span class="status-pill ${windowItem.status === "completed" ? "ok" : windowItem.status === "failed" ? "bad" : "neutral"}">${escapeHtml(windowItem.status)}</span></td>
      <td>${windowItem.handoff_start == null ? "-" : formatSeconds(windowItem.handoff_start)}</td>
      <td>${escapeHtml(windowItem.error_message || "-")}</td>
    </tr>`).join("");

  $("segmentsBody").innerHTML = state.segments.map((segment) => `
    <tr>
      <td class="mono">${formatSeconds(segment.global_start)}<br><span class="muted">${formatSeconds(segment.global_end)}</span></td>
      <td>${escapeHtml(segment.title)}</td>
      <td>${escapeHtml(segment.topic || "-")}</td>
      <td>${escapeHtml((segment.keywords || []).join(", ") || "-")}</td>
      <td>${segment.window_index + 1}</td>
      <td><span class="status-pill ${segment.accepted ? "ok" : "warn"}">${segment.accepted ? "采用" : escapeHtml(segment.reason || "舍弃")}</span></td>
      <td>${segment.segment_url ? `<a class="external-link" href="${escapeHtml(segment.segment_url)}" target="_blank" rel="noreferrer">打开</a>` : "-"}</td>
    </tr>`).join("");
}

function renderActions(job) {
  const actions = [];
  if (["queued", "running"].includes(job.status)) actions.push(`<button class="button warning small" data-action="pause" type="button">暂停</button>`);
  if (["paused", "failed"].includes(job.status)) actions.push(`<button class="button primary small" data-action="resume" type="button">继续</button>`);
  if (!["completed", "stopped"].includes(job.status)) actions.push(`<button class="button danger small" data-action="stop" type="button">停止</button>`);
  actions.push(`<a class="button secondary small external-link" href="/api/jobs/${escapeHtml(job.id)}/result?download=true">下载 JSON</a>`);
  $("detailActions").innerHTML = actions.join("");
  $("detailActions").querySelectorAll("button[data-action]").forEach((button) => button.addEventListener("click", () => controlJob(job.id, button.dataset.action)));
}

async function controlJob(jobId, action) {
  try {
    await api(`/api/jobs/${jobId}/${action}`, { method: "POST" });
    showToast("作业状态已更新");
    await loadJobs();
  } catch (error) { showToast(error.message); }
}

function openCreate() {
  $("createError").hidden = true;
  $("createDialog").showModal();
}

async function submitCreate(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = Object.fromEntries(form.entries());
  if (!payload.channelName) delete payload.channelName;
  if (!payload.programStartTime) delete payload.programStartTime;
  const submit = $("submitCreateButton");
  submit.disabled = true;
  try {
    const job = await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
    $("createDialog").close();
    event.currentTarget.reset();
    showToast("作业已创建");
    await loadJobs();
    await loadDetail(job.id, true);
  } catch (error) {
    $("createError").textContent = error.message;
    $("createError").hidden = false;
  } finally { submit.disabled = false; }
}

$("openCreateButton").addEventListener("click", openCreate);
$("closeCreateButton").addEventListener("click", () => $("createDialog").close());
$("cancelCreateButton").addEventListener("click", () => $("createDialog").close());
$("createForm").addEventListener("submit", submitCreate);
$("refreshButton").addEventListener("click", () => loadJobs().catch((error) => showToast(error.message)));
$("statusFilter").addEventListener("change", () => loadJobs().catch((error) => showToast(error.message)));
$("acceptedOnly").addEventListener("change", () => state.selectedJobId && loadDetail(state.selectedJobId, false));

Promise.all([loadHealth(), loadJobs()]).catch((error) => showToast(error.message));
window.setInterval(() => loadJobs().catch(() => {}), 5000);
window.setInterval(() => loadHealth().catch(() => {}), 30000);
