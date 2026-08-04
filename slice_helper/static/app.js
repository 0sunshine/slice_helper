const state = {
  jobs: [], selectedJobId: null, detail: null, segments: [],
  segmentPage: 1, previewUrl: null
};
const SEGMENTS_PER_PAGE = 10;

const statusNames = {
  pending_schedule: "待调度", queued: "已调度", probing: "探测中", running: "处理中",
  pause_requested: "等待暂停", paused: "已暂停", completed: "已完成",
  failed: "失败", stop_requested: "等待停止", stopped: "已停止"
};

const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
})[character]);

function statusClass(status) {
  if (status === "completed") return "ok";
  if (["paused", "pause_requested", "pending_schedule", "queued"].includes(status)) return "warn";
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

function formatRealTime(value) {
  if (!value) return "-";
  const match = String(value).match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/);
  return match ? `${match[1]} ${match[2]}` : formatDate(value);
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
  if (state.selectedJobId && state.selectedJobId !== jobId) {
    resetPreview();
    state.segmentPage = 1;
  }
  state.selectedJobId = jobId;
  const [detail, segments] = await Promise.all([
    api(`/api/jobs/${jobId}`),
    api(`/api/jobs/${jobId}/segments?acceptedOnly=${$("acceptedOnly").checked}`)
  ]);
  state.detail = detail;
  state.segments = segments;
  if (state.previewUrl && !segments.some((segment) => segment.segment_url === state.previewUrl)) {
    resetPreview();
  }
  renderDetail();
  if (scroll) $("detailBand").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderDetail() {
  const { job, windows, attempts = [] } = state.detail;
  $("detailBand").hidden = false;
  $("detailId").textContent = job.id;
  $("detailTitle").textContent = job.source_path.split(/[\\/]/).pop();
  $("detailPath").textContent = job.source_path;
  $("summaryISlice").textContent = job.islice_base_url || "-";
  $("summaryISlice").title = job.islice_base_url || "";
  $("summaryStatus").innerHTML = `<span class="status-pill ${statusClass(job.status)}">${escapeHtml(statusNames[job.status] || job.status)}</span>`;
  $("summaryDuration").textContent = formatSeconds(job.source_duration);
  $("summaryWindow").textContent = `${job.current_window} / ${job.total_windows}`;
  $("summaryCutMode").textContent = job.cut_mode === "copy" ? "流复制" : "重编码";
  const referenceNames = { ocr: "OCR", manual_fallback: "手工回退" };
  $("summaryRealTime").textContent = job.program_start_time
    ? `${formatRealTime(job.program_start_time)} (${referenceNames[job.time_reference_source] || "已有数据"})`
    : "未识别";
  $("detailProgress").style.width = `${Math.max(0, Math.min(100, job.progress || 0))}%`;
  $("detailError").hidden = !job.error_message;
  $("detailError").textContent = job.error_message || "";
  $("detailWarnings").innerHTML = (job.warnings || []).map((warning) => `<p>${escapeHtml(warning)}</p>`).join("");
  renderActions(job);

  const latestAttempts = new Map();
  attempts.forEach((attempt) => latestAttempts.set(attempt.window_index, attempt));
  $("windowsBody").innerHTML = windows.map((windowItem) => `
    <tr>
      <td>${windowItem.window_index + 1}</td>
      <td>${formatSeconds(windowItem.requested_start)}</td>
      <td>${formatSeconds(windowItem.nominal_end)}</td>
      <td><span class="status-pill ${windowItem.status === "completed" ? "ok" : windowItem.status === "failed" ? "bad" : "neutral"}">${escapeHtml(windowItem.status)}</span></td>
      <td>${renderTaskProgress(latestAttempts.get(windowItem.window_index))}</td>
      <td>${windowItem.handoff_start == null ? "-" : formatSeconds(windowItem.handoff_start)}</td>
      <td>${escapeHtml(windowItem.error_message || "-")}</td>
    </tr>`).join("");

  renderSegments();
}

function renderTaskProgress(attempt) {
  if (!attempt) return '<span class="muted">尚未提交</span>';
  const progress = Math.max(0, Math.min(100, Number(attempt.progress || 0)));
  const serviceStatus = attempt.service_status || attempt.status || "pending";
  return `
    <div class="task-progress-cell">
      <span class="mono window-task-id" title="${escapeHtml(attempt.task_id)}">${escapeHtml(attempt.task_id)}</span>
      <span class="task-progress-meta">
        <span class="mini-progress"><span style="width:${progress}%"></span></span>
        <strong>${progress.toFixed(0)}%</strong>
        <span class="muted">${escapeHtml(serviceStatus)}</span>
      </span>
    </div>`;
}

function renderSegments() {
  const pageCount = Math.max(1, Math.ceil(state.segments.length / SEGMENTS_PER_PAGE));
  state.segmentPage = Math.max(1, Math.min(pageCount, state.segmentPage));
  const startIndex = (state.segmentPage - 1) * SEGMENTS_PER_PAGE;
  const pageSegments = state.segments.slice(startIndex, startIndex + SEGMENTS_PER_PAGE);
  $("segmentPageInfo").textContent = `${state.segmentPage} / ${pageCount}`;
  $("previousSegmentPage").disabled = state.segmentPage <= 1;
  $("nextSegmentPage").disabled = state.segmentPage >= pageCount;

  $("segmentsBody").innerHTML = pageSegments.map((segment, pageIndex) => {
    const segmentIndex = startIndex + pageIndex;
    const previewable = Boolean(segment.segment_url);
    const active = previewable && segment.segment_url === state.previewUrl;
    const title = segment.title || `片段 ${segmentIndex + 1}`;
    const keywords = (segment.keywords || []).join(", ") || "-";
    return `
    <tr class="segment-row${previewable ? " is-previewable" : ""}${active ? " is-active" : ""}"
        ${previewable ? `data-segment-index="${segmentIndex}" tabindex="0" aria-label="播放 ${escapeHtml(title)}"` : ""}
        ${active ? 'aria-current="true"' : ""}>
      <td class="mono"><span class="segment-time-stack"><span>${formatSeconds(segment.global_start)}</span><span>${formatSeconds(segment.global_end)}</span></span></td>
      <td class="mono"><span class="segment-time-stack"><span>${formatRealTime(segment.absolute_start)}</span><span>${formatRealTime(segment.absolute_end)}</span></span></td>
      <td><span class="segment-title" title="${escapeHtml(title)}">${escapeHtml(title)}</span></td>
      <td class="segment-topic">${escapeHtml(segment.topic || "-")}</td>
      <td><span class="segment-keywords" title="${escapeHtml(keywords)}">${escapeHtml(keywords)}</span></td>
      <td>${segment.window_index + 1}</td>
      <td><span class="status-pill ${segment.accepted ? "ok" : "warn"}">${segment.accepted ? "采用" : escapeHtml(segment.reason || "舍弃")}</span></td>
    </tr>`;
  }).join("") || '<tr><td class="empty-table-row" colspan="7">暂无拆条结果</td></tr>';
}

function previewSegment(segmentIndex) {
  const segment = state.segments[segmentIndex];
  if (!segment?.segment_url) return;

  const video = $("previewVideo");
  const sourceChanged = video.getAttribute("src") !== segment.segment_url;
  if (sourceChanged) video.pause();
  state.previewUrl = segment.segment_url;
  $("previewTitle").textContent = segment.title || `片段 ${segmentIndex + 1}`;
  $("previewTime").textContent = `${formatRealTime(segment.absolute_start)} - ${formatRealTime(segment.absolute_end)} | ${formatSeconds(segment.global_start)} - ${formatSeconds(segment.global_end)}`;
  $("previewStatus").textContent = "正在加载媒体信息...";
  $("previewStatus").className = "preview-status";
  $("openPreviewExternally").href = segment.segment_url;
  $("openPreviewExternally").hidden = false;
  $("closePreviewButton").disabled = false;
  if (segment.cover_img_url) video.poster = segment.cover_img_url;
  else video.removeAttribute("poster");
  if (sourceChanged) {
    video.src = segment.segment_url;
    video.load();
  }
  document.querySelectorAll(".segment-row").forEach((row) => {
    const active = Number(row.dataset.segmentIndex) === segmentIndex;
    row.classList.toggle("is-active", active);
    if (active) row.setAttribute("aria-current", "true");
    else row.removeAttribute("aria-current");
  });
  video.play().catch(() => {
    if (video.getAttribute("src")) {
      $("previewStatus").textContent = "媒体已就绪";
      $("previewStatus").className = "preview-status ready";
    }
  });
  document.querySelector(".media-workspace").scrollIntoView({
    behavior: "smooth", block: "start"
  });
}

function resetPreview() {
  const video = $("previewVideo");
  state.previewUrl = null;
  video.pause();
  video.removeAttribute("src");
  video.removeAttribute("poster");
  video.load();
  $("openPreviewExternally").removeAttribute("href");
  $("openPreviewExternally").hidden = true;
  $("closePreviewButton").disabled = true;
  $("previewTitle").textContent = "未选择片段";
  $("previewTime").textContent = "";
  $("previewStatus").textContent = "未选择片段";
  $("previewStatus").className = "preview-status";
  document.querySelectorAll(".segment-row.is-active").forEach((row) => {
    row.classList.remove("is-active");
    row.removeAttribute("aria-current");
  });
}

function renderActions(job) {
  const actions = [];
  if (["pending_schedule", "queued", "running"].includes(job.status)) actions.push(`<button class="button warning small" data-action="pause" type="button">暂停</button>`);
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
$("closePreviewButton").addEventListener("click", resetPreview);
$("segmentsBody").addEventListener("click", (event) => {
  const row = event.target.closest(".segment-row.is-previewable");
  if (row) previewSegment(Number(row.dataset.segmentIndex));
});
$("segmentsBody").addEventListener("keydown", (event) => {
  const row = event.target.closest(".segment-row.is-previewable");
  if (row && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault();
    previewSegment(Number(row.dataset.segmentIndex));
  }
});
$("previewVideo").addEventListener("loadedmetadata", () => {
  if ($('previewVideo').paused) {
    $("previewStatus").textContent = "媒体已就绪";
    $("previewStatus").className = "preview-status ready";
  }
});
$("previewVideo").addEventListener("playing", () => {
  $("previewStatus").textContent = "正在播放";
  $("previewStatus").className = "preview-status ready";
});
$("previewVideo").addEventListener("pause", () => {
  const video = $("previewVideo");
  if (video.getAttribute("src") && !video.ended) {
    $("previewStatus").textContent = "已暂停";
    $("previewStatus").className = "preview-status";
  }
});
$("previewVideo").addEventListener("ended", () => {
  $("previewStatus").textContent = "播放结束";
  $("previewStatus").className = "preview-status";
});
$("previewVideo").addEventListener("error", () => {
  if (!$("previewVideo").getAttribute("src")) return;
  $("previewStatus").textContent = "浏览器无法加载该片段，请使用新窗口打开。";
  $("previewStatus").className = "preview-status error";
});
$("createForm").addEventListener("submit", submitCreate);
$("refreshButton").addEventListener("click", () => loadJobs().catch((error) => showToast(error.message)));
$("statusFilter").addEventListener("change", () => loadJobs().catch((error) => showToast(error.message)));
$("acceptedOnly").addEventListener("change", () => {
  state.segmentPage = 1;
  if (state.selectedJobId) loadDetail(state.selectedJobId, false);
});
$("previousSegmentPage").addEventListener("click", () => {
  state.segmentPage -= 1;
  renderSegments();
});
$("nextSegmentPage").addEventListener("click", () => {
  state.segmentPage += 1;
  renderSegments();
});

Promise.all([loadHealth(), loadJobs()]).catch((error) => showToast(error.message));
window.setInterval(() => loadJobs().catch(() => {}), 5000);
window.setInterval(() => loadHealth().catch(() => {}), 30000);
