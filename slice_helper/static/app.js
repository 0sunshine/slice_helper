const state = {
  jobs: [], channels: [], isliceInstances: [], selectedJobId: null, detail: null, segments: [],
  jobPage: 1, jobPageSize: 20, jobTotal: 0, jobTotalPages: 1,
  segmentPage: 1, previewUrl: null, resplitTarget: null, tailRebuildTarget: null,
  segmentEditTarget: null, selectedSegmentIds: new Set(), mergePreview: null,
  timeRefreshTarget: null
};
const SEGMENTS_PER_PAGE = 10;
const CONTENT_TYPES = [
  "鏂伴椈", "鐢佃鍓?, "鐢靛奖", "缁艰壓", "灏戝効", "浣撹偛", "绾綍鐗?, "绉戞暀",
  "鏂囪壓", "鐢熸椿鏈嶅姟", "鍟嗕笟骞垮憡", "鍏泭骞垮憡", "鐢佃璐墿", "鍏朵粬"
];

const statusNames = {
  pending_schedule: "寰呰皟搴?, queued: "宸茶皟搴?, probing: "鎺㈡祴涓?, running: "澶勭悊涓?,
  pause_requested: "绛夊緟鏆傚仠", paused: "宸叉殏鍋?, completed: "宸插畬鎴?,
  failed: "澶辫触", stop_requested: "绛夊緟鍋滄", stopped: "宸插仠姝?
};

const $ = (id) => document.getElementById(id);

async function configureSchedulingPriority() {
  const current = await api("/api/settings/scheduling-priority");
  const dialog = document.createElement("dialog");
  dialog.className = "instance-dialog";
  dialog.innerHTML = `<form method="dialog"><div class="dialog-header"><h2>调度优先策略</h2><button class="icon-button" value="cancel" aria-label="关闭">×</button></div><p class="muted">选择后立即对后续调度生效，当前正在执行的任务不受影响。</p><label>调度策略<select id="schedulingPriorityChoice"><option value="fewest_completed">完成数最少优先</option><option value="most_completed">完成数最多优先</option></select></label><div class="dialog-actions"><button class="button secondary" value="cancel">取消</button><button id="saveSchedulingPriority" class="button primary" value="default">保存</button></div></form>`;
  document.body.append(dialog);
  const choice = dialog.querySelector("#schedulingPriorityChoice");
  choice.value = current.priority;
  dialog.querySelector("form").addEventListener("submit", async (event) => {
    if (event.submitter?.id !== "saveSchedulingPriority") return;
    event.preventDefault();
    try {
      await api("/api/settings/scheduling-priority", { method: "PUT", body: JSON.stringify({ priority: choice.value }) });
      dialog.close();
      showToast(choice.value === "fewest_completed" ? "已切换为完成数最少优先" : "已切换为完成数最多优先");
    } catch (error) { showToast(error.message); }
  });
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  dialog.showModal();
}const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
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

function toDateTimeLocal(value) {
  if (!value) return "";
  const match = String(value).match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/);
  return match ? `${match[1]}T${match[2]}` : "";
}

function sourceDisplay(job) {
  return job.source_url || job.source_path;
}

function sourceName(job) {
  const source = sourceDisplay(job);
  try {
    const url = new URL(source);
    return decodeURIComponent(url.pathname.split("/").filter(Boolean).pop() || url.hostname);
  } catch (_) {
    return source.split(/[\\/]/).pop();
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options
  });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    let detail = null;
    try {
      detail = (await response.json()).detail;
      message = typeof detail === "string" ? detail : detail?.message || message;
    } catch (_) { /* no-op */ }
    const error = new Error(message);
    error.status = response.status;
    error.detail = detail;
    throw error;
  }
  if (response.status === 204) return null;
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
    badge.textContent = response.ok ? "鏈嶅姟灏辩华" : "渚濊禆寮傚父";
    badge.className = `health ${response.ok ? "ok" : "bad"}`;
  } catch (_) {
    badge.textContent = "杩炴帴澶辫触";
    badge.className = "health bad";
  }
}

async function loadJobs() {
  const query = new URLSearchParams({
    page: String(state.jobPage),
    pageSize: String(state.jobPageSize)
  });
  if ($("statusFilter").value) query.set("status", $("statusFilter").value);
  if ($("channelFilter").value) query.set("channelId", $("channelFilter").value);
  if ($("isliceFilter").value) query.set("isliceBaseUrl", $("isliceFilter").value);
  if ($("dateFilter").value) query.set("broadcastDate", $("dateFilter").value);
  const result = await api(`/api/jobs?${query}`);
  state.jobs = result.items;
  state.jobPage = result.page;
  state.jobTotal = result.total;
  state.jobTotalPages = result.totalPages;
  renderJobs();
  if (state.selectedJobId) await loadDetail(state.selectedJobId, false);
}

function renderJobs() {
  const body = $("jobsBody");
  $("jobCount").textContent = state.jobTotal;
  $("emptyJobs").hidden = state.jobs.length !== 0;
  body.innerHTML = state.jobs.map((job) => `
    <tr>
      <td>${escapeHtml(job.channel_name || "-")}</td>
      <td>${escapeHtml(job.broadcast_date || "-")}</td>
      <td><span class="filename" title="${escapeHtml(sourceDisplay(job))}">${escapeHtml(sourceName(job))}</span><span class="muted mono">${escapeHtml(job.id.slice(0, 12))}</span></td>
      <td class="mono job-first-frame-time">${escapeHtml(formatRealTime(job.program_start_time))}</td>
      <td><span class="status-pill ${statusClass(job.status)}">${escapeHtml(statusNames[job.status] || job.status)}</span></td>
      <td>${renderJobProgress(job)}</td>
      <td>${job.current_window}/${job.total_windows}</td>
      <td>${job.accepted_segment_count}</td>
      <td class="reviewed-cell"><input class="review-checkbox" data-job-id="${escapeHtml(job.id)}" type="checkbox" ${job.reviewed ? "checked" : ""} aria-label="${job.reviewed ? "鍙栨秷" : "鏍囪"}${escapeHtml(job.channel_name || sourceName(job))}宸插鏍?></td>
      <td>${escapeHtml(formatDate(job.created_at))}</td>
      <td><span class="job-row-actions">
        <button class="button secondary small view-job" data-job-id="${escapeHtml(job.id)}" type="button">鏌ョ湅</button>
        <button class="button secondary small refresh-job-time" data-job-id="${escapeHtml(job.id)}" type="button">閲嶆柊鍙栨椂</button>
        ${renderJobControlActions(job)}
      </span></td>
    </tr>`).join("");
  body.querySelectorAll(".view-job").forEach((button) => button.addEventListener("click", () => loadDetail(button.dataset.jobId, true)));
  body.querySelectorAll(".refresh-job-time").forEach((button) => button.addEventListener("click", () => openTimeRefresh(button.dataset.jobId)));
  body.querySelectorAll(".control-job").forEach((button) => button.addEventListener("click", () => controlJob(button.dataset.jobId, button.dataset.action, button)));
  body.querySelectorAll(".review-checkbox").forEach((checkbox) => checkbox.addEventListener("change", () => updateJobReview(checkbox)));
  $("jobPageSummary").textContent = `鍏?${state.jobTotal} 鏉;
  $("jobPageInfo").textContent = `${state.jobPage} / ${state.jobTotalPages}`;
  $("previousJobPage").disabled = state.jobPage <= 1;
  $("nextJobPage").disabled = state.jobPage >= state.jobTotalPages;
}

function renderJobProgress(job) {
  const overall = Math.max(0, Math.min(100, Number(job.progress || 0)));
  const taskProgress = Math.max(0, Math.min(100, Number(job.current_task_progress || 0)));
  const taskId = job.current_task_id || "";
  const taskStatus = job.current_task_service_status || job.current_task_status || "";
  const activeTask = ["pending", "submitting", "processing", "polling", "resplitting"].includes(taskStatus.toLowerCase());
  const taskLabel = taskId && activeTask
    ? `褰撳墠灏忎换鍔?${Number(job.current_task_window || 0) + 1}锛?{taskStatus} ${taskProgress.toFixed(1)}%`
    : "鏆傛棤杩愯涓殑灏忎换鍔?;
  return `<div class="job-progress-cell">
    <span><span class="mini-progress"><span style="width:${overall}%"></span></span>${overall.toFixed(1)}%</span>
    <span class="muted job-current-task" title="${escapeHtml(taskId)}">${escapeHtml(taskLabel)}</span>
  </div>`;
}

function renderJobControlActions(job) {
  const actions = [];
  const jobId = escapeHtml(job.id);
  if (["pending_schedule", "queued", "running"].includes(job.status)) {
    actions.push(`<button class="button warning small control-job" data-job-id="${jobId}" data-action="pause" type="button">鏆傚仠</button>`);
  }
  if (["paused", "failed"].includes(job.status)) {
    actions.push(`<button class="button primary small control-job" data-job-id="${jobId}" data-action="resume" type="button">缁х画</button>`);
  }
  if (!["completed", "stopped"].includes(job.status)) {
    actions.push(`<button class="button danger small control-job" data-job-id="${jobId}" data-action="stop" type="button">鍋滄</button>`);
  }
  return actions.join("");
}

async function updateJobReview(checkbox) {
  const jobId = checkbox.dataset.jobId;
  const reviewed = checkbox.checked;
  checkbox.disabled = true;
  try {
    const updated = await api(`/api/jobs/${jobId}/review`, {
      method: "PATCH",
      body: JSON.stringify({ reviewed })
    });
    const job = state.jobs.find((item) => item.id === jobId);
    if (job) job.reviewed = updated.reviewed;
    if (state.detail?.job?.id === jobId) state.detail.job.reviewed = updated.reviewed;
    checkbox.setAttribute("aria-label", `${updated.reviewed ? "鍙栨秷" : "鏍囪"}${updated.channel_name || sourceName(updated)}宸插鏍竊);
    showToast(updated.reviewed ? "浣滀笟宸叉爣璁颁负瀹℃牳瀹屾垚" : "宸插彇娑堜綔涓氬鏍告爣璁?);
  } catch (error) {
    checkbox.checked = !reviewed;
    showToast(error.message);
  } finally {
    checkbox.disabled = false;
  }
}

function openTimeRefresh(jobId) {
  const job = state.jobs.find((item) => item.id === jobId);
  if (!job) return;
  state.timeRefreshTarget = job;
  $("timeRefreshChannel").textContent = job.channel_name || "-";
  $("timeRefreshSource").textContent = sourceDisplay(job);
  $("timeRefreshCurrent").textContent = formatRealTime(job.program_start_time);
  $("timeRefreshManualTime").value = toDateTimeLocal(job.program_start_time);
  $("timeRefreshError").hidden = true;
  $("timeRefreshError").textContent = "";
  $("timeRefreshDialog").showModal();
}

function closeTimeRefresh() {
  $("timeRefreshDialog").close();
  state.timeRefreshTarget = null;
}

async function submitTimeRefresh(event) {
  event.preventDefault();
  const job = state.timeRefreshTarget;
  if (!job) return;
  const submit = $("submitTimeRefreshButton");
  submit.disabled = true;
  submit.textContent = "璇嗗埆涓?;
  $("timeRefreshError").hidden = true;
  try {
    const manualTime = $("timeRefreshManualTime").value;
    const options = { method: "POST" };
    if (manualTime) options.body = JSON.stringify({ programStartTime: manualTime });
    const result = await api(`/api/jobs/${job.id}/refresh-time-reference`, options);
    closeTimeRefresh();
    showToast(result.manual ? `鎵嬪姩鏃堕棿宸蹭繚瀛橈紝鍚屾 ${result.updatedSegmentCount} 涓墖娈礰 : `棣栧抚鏃堕棿宸叉洿鏂帮紝灏濊瘯 ${result.attemptCount} 娆★紝鍚屾 ${result.updatedSegmentCount} 涓墖娈礰);
    await loadJobs();
  } catch (error) {
    $("timeRefreshError").textContent = error.message;
    $("timeRefreshError").hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "纭閲嶆柊璇嗗埆";
  }
}

async function loadChannels() {
  const selectedFilter = $("channelFilter").value;
  const selectedCreate = $("createChannelId").value;
  state.channels = await api("/api/channels");
  const options = state.channels.map((channel) =>
    `<option value="${escapeHtml(channel.id)}">${escapeHtml(channel.name)}</option>`
  ).join("");
  $("channelFilter").innerHTML = `<option value="">鍏ㄩ儴棰戦亾</option>${options}`;
  $("createChannelId").innerHTML = `<option value="">璇烽€夋嫨棰戦亾</option>${options}`;
  if (state.channels.some((channel) => channel.id === selectedFilter)) $("channelFilter").value = selectedFilter;
  if (state.channels.some((channel) => channel.id === selectedCreate)) $("createChannelId").value = selectedCreate;
  $("exportChannelButton").disabled = !$("channelFilter").value;
  renderChannels();
}

async function loadISliceInstances() {
  const selected = $("createISliceBaseUrl").value;
  const selectedFilter = $("isliceFilter").value;
  state.isliceInstances = await api("/api/islice-instances");
  const filterOptions = state.isliceInstances
    .map((item) => `<option value="${escapeHtml(item.base_url)}">${escapeHtml(item.name || item.source_id || item.base_url)} (${escapeHtml(item.base_url)})</option>`)
    .join("");
  $("isliceFilter").innerHTML = `<option value="">鍏ㄩ儴鑺傜偣</option>${filterOptions}`;
  if (state.isliceInstances.some((item) => item.base_url === selectedFilter)) $("isliceFilter").value = selectedFilter;
  const options = state.isliceInstances.filter((item) => item.schedulable)
    .map((item) => `<option value="${escapeHtml(item.base_url)}">${escapeHtml(item.name || item.source_id)} (${escapeHtml(item.base_url)})</option>`).join("");
  $("createISliceBaseUrl").innerHTML = `<option value="">鑷姩閫夋嫨</option>${options}`;
  if (state.isliceInstances.some((item) => item.schedulable && item.base_url === selected)) $("createISliceBaseUrl").value = selected;
}

function renderChannels() {
  $("channelsList").innerHTML = state.channels.map((channel) => `
    <div class="channel-row">
      <div><strong>${escapeHtml(channel.name)}</strong><span>${channel.job_count || 0} 涓綋鍓嶄綔涓?/span></div>
      <div>
        <button class="button secondary small rename-channel" data-channel-id="${escapeHtml(channel.id)}" type="button">閲嶅懡鍚?/button>
        <button class="button danger small delete-channel" data-channel-id="${escapeHtml(channel.id)}" type="button" ${channel.job_count ? "disabled" : ""}>鍒犻櫎</button>
      </div>
    </div>`).join("") || '<p class="empty-channel-list">鏆傛棤棰戦亾锛岃鍏堟坊鍔犮€?/p>';
  $("channelsList").querySelectorAll(".rename-channel").forEach((button) => button.addEventListener("click", () => renameChannel(button.dataset.channelId)));
  $("channelsList").querySelectorAll(".delete-channel").forEach((button) => button.addEventListener("click", () => deleteChannel(button.dataset.channelId)));
}

async function loadDetail(jobId, scroll) {
  if (state.selectedJobId && state.selectedJobId !== jobId) {
    resetPreview();
    state.segmentPage = 1;
    state.selectedSegmentIds.clear();
  }
  state.selectedJobId = jobId;
  const [detail, segments, archiveStatus] = await Promise.all([
    api(`/api/jobs/${jobId}`),
    api(`/api/jobs/${jobId}/segments?acceptedOnly=${$("acceptedOnly").checked}`),
    api(`/api/jobs/${jobId}/archive-status`).catch(() => ({ status: "unavailable" }))
  ]);
  state.detail = detail;
  state.segments = segments;
  state.archiveStatus = archiveStatus;
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
  $("detailTitle").textContent = sourceName(job);
  $("detailPath").textContent = sourceDisplay(job);
  $("summaryChannel").textContent = job.channel_name || "-";
  $("summaryBroadcastDate").textContent = job.broadcast_date || "-";
  $("summaryISlice").textContent = job.islice_base_url || "-";
  $("summaryISlice").title = job.islice_base_url || "";
  $("summaryStatus").innerHTML = `<span class="status-pill ${statusClass(job.status)}">${escapeHtml(statusNames[job.status] || job.status)}</span>`;
  $("summaryDuration").textContent = formatSeconds(job.source_duration);
  $("summaryWindow").textContent = `${job.current_window} / ${job.total_windows}`;
  $("summaryCutMode").textContent = job.cut_mode === "copy" ? "娴佸鍒? : "閲嶇紪鐮?;
  const referenceNames = {
    ocr: "OCR",
    manual_fallback: "鎵嬪伐鍥為€€",
    manual_override: "鎵嬪伐淇",
    unavailable: "鏈瘑鍒?
  };
  const timeInput = $("summaryRealTime");
  if (document.activeElement !== timeInput) {
    timeInput.value = toDateTimeLocal(job.program_start_time);
  }
  const referenceLabel = referenceNames[job.time_reference_source] || "宸叉湁鏁版嵁";
  $("timeReferenceMeta").textContent = job.time_reference_source === "ocr" && job.time_reference_frame_offset
    ? `${referenceLabel}锛堝彇鏍?+${Number(job.time_reference_frame_offset).toFixed(0)} 绉掞紝宸插弽鎺ㄩ甯э級`
    : referenceLabel;
  $("detailProgress").style.width = `${Math.max(0, Math.min(100, job.progress || 0))}%`;
  $("detailError").hidden = !job.error_message;
  $("detailError").textContent = job.error_message || "";
  $("detailWarnings").innerHTML = (job.warnings || []).map((warning) => `<p>${escapeHtml(warning)}</p>`).join("");
  const archiveStatus = state.archiveStatus || {};
  const archiveLabel = {ready: "宸插綊妗ｏ紝鍙湪 iSlice 娓呯悊鍚庣户缁瑙?, pending: "褰掓。澶勭悊涓紝鏆備笉寤鸿娓呯悊 iSlice", error: "褰掓。瀛樺湪寮傚父锛岃鍏堝鐞?, unavailable: "褰掓。鐘舵€佹殏涓嶅彲鐢?, not_applicable: "鏆傛棤鍙綊妗ｄ换鍔?}[archiveStatus.status] || "褰掓。鐘舵€佹湭鐭?;
  $("archiveStatus").textContent = `褰掓。鐘舵€侊細${archiveLabel}`;
  renderActions(job);

  const latestAttempts = new Map();
  attempts.forEach((attempt) => latestAttempts.set(attempt.window_index, attempt));
  const latestSubmittedAttempt = [...latestAttempts.values()].reduce((latest, attempt) => {
    if (!attempt.submitted_at) return latest;
    return !latest || attempt.submitted_at > latest.submitted_at ? attempt : latest;
  }, null);
  const latestSubmittedWindowIndex = latestSubmittedAttempt?.window_index;
  $("windowsBody").innerHTML = windows.map((windowItem) => `
    <tr class="window-row${windowItem.window_index === latestSubmittedWindowIndex ? " is-latest-submitted" : ""}">
      <td>${windowItem.window_index + 1}</td>
      <td>${formatSeconds(windowItem.requested_start)}</td>
      <td>${formatSeconds(windowItem.nominal_end)}</td>
      <td><span class="status-pill ${windowItem.status === "completed" ? "ok" : windowItem.status === "failed" ? "bad" : "neutral"}">${escapeHtml(windowItem.status)}</span></td>
      <td>${renderTaskProgress(latestAttempts.get(windowItem.window_index))}</td>
      <td class="mono window-submitted-at">${escapeHtml(formatDate(latestAttempts.get(windowItem.window_index)?.submitted_at))}</td>
      <td>${windowItem.handoff_start == null ? "-" : formatSeconds(windowItem.handoff_start)}</td>
      <td>${escapeHtml(windowItem.error_message || "-")}</td>
      <td>${renderWindowActions(job, windowItem, latestAttempts.get(windowItem.window_index))}</td>
    </tr>`).join("");
  $("windowsBody").querySelectorAll(".resplit-window").forEach((button) => {
    button.addEventListener("click", () => openResplit({
      jobId: job.id,
      windowIndex: Number(button.dataset.windowIndex),
      taskId: button.dataset.taskId
    }));
  });
  $("windowsBody").querySelectorAll(".accept-overlap").forEach((button) => {
    button.addEventListener("click", () => acceptOverlap({
      jobId: job.id,
      windowIndex: Number(button.dataset.windowIndex),
      taskId: button.dataset.taskId
    }));
  });
  $("windowsBody").querySelectorAll(".tail-rebuild-window").forEach((button) => {
    button.addEventListener("click", () => openTailRebuild({
      jobId: job.id,
      windowIndex: Number(button.dataset.windowIndex)
    }));
  });

  renderSegments();
}

function renderTaskProgress(attempt) {
  if (!attempt) return '<span class="muted">灏氭湭鎻愪氦</span>';
  const progress = Math.max(0, Math.min(100, Number(attempt.progress || 0)));
  const serviceStatus = attempt.service_status || attempt.status || "pending";
  return `
    <div class="task-progress-cell">
      <span class="mono window-task-id" title="${escapeHtml(attempt.task_id)}">${escapeHtml(attempt.task_id)}</span>
      <span class="muted mono">鐪熷疄璧风偣 ${escapeHtml(formatRealTime(attempt.program_start_time))}</span>
      <span class="task-progress-meta">
        <span class="mini-progress"><span style="width:${progress}%"></span></span>
        <strong>${progress.toFixed(0)}%</strong>
        <span class="muted">${escapeHtml(serviceStatus)}</span>
      </span>
    </div>`;
}

function renderResplitAction(job, windowItem, attempt) {
  if (!attempt) return '<span class="muted">-</span>';
  const jobReady = ["paused", "completed", "failed", "stopped"].includes(job.status);
  const taskReady = ["completed", "failed", "discarded"].includes(attempt.status);
  const disabled = !(jobReady && taskReady);
  const title = disabled ? "浣滀笟鍙婂皬浠诲姟缁撴潫鍚庢墠鑳介噸鏂版媶鍒? : "鍒涘缓鍏ㄦ柊浠诲姟 ID 閲嶆柊鎷嗗垎";
  const resplitButton = `<button class="button secondary small resplit-window" type="button"
    data-window-index="${windowItem.window_index}" data-task-id="${escapeHtml(attempt.task_id)}"
    title="${title}" ${disabled ? "disabled" : ""}>閲嶆柊鎷嗗垎</button>`;
  const boundaryError = String(windowItem.error_message || "");
  const overlapReady = job.status === "paused"
    && windowItem.status === "failed"
    && attempt.status === "completed"
    && attempt.service_status === "completed"
    && Boolean(attempt.raw_response_path)
    && [
      "accepted segments overlap the previous window",
      "the resplit handoff changed from the next window's fixed source start",
      "resplit segments overlap a following window"
    ].some((marker) => boundaryError.includes(marker));
  const overlapButton = overlapReady
    ? `<button class="button danger small accept-overlap" type="button"
        data-window-index="${windowItem.window_index}" data-task-id="${escapeHtml(attempt.task_id)}"
        title="鎺ョ撼宸蹭繚瀛樼殑閲嶆媶缁撴灉锛屼笉鍐嶈皟鐢?iSlice">鍏佽閲嶅彔骞跺悎骞?/button>`
    : "";
  return `<span class="window-actions">${resplitButton}${overlapButton}</span>`;
}

function renderWindowActions(job, windowItem, attempt) {
  const resplit = renderResplitAction(job, windowItem, attempt);
  const jobReady = ["paused", "completed", "failed", "stopped"].includes(job.status);
  const disabled = !jobReady;
  const rebuildButton = `<button class="button danger small tail-rebuild-window" type="button"
    data-window-index="${windowItem.window_index}"
    title="鍒犻櫎姝ょ獥鍙ｅ強鍚庣画缁撴灉锛屽苟浣跨敤鏂扮殑浠诲姟 ID 浠庤繖閲岄噸鏂版墽琛? ${disabled ? "disabled" : ""}>浠庢绐楀彛閲嶈窇</button>`;
  return `<span class="window-actions">${resplit}${rebuildButton}</span>`;
}

function renderSegments() {
  const pageCount = Math.max(1, Math.ceil(state.segments.length / SEGMENTS_PER_PAGE));
  state.segmentPage = Math.max(1, Math.min(pageCount, state.segmentPage));
  const startIndex = (state.segmentPage - 1) * SEGMENTS_PER_PAGE;
  const pageSegments = state.segments.slice(startIndex, startIndex + SEGMENTS_PER_PAGE);
  $("segmentPageInfo").textContent = `${state.segmentPage} / ${pageCount}`;
  $("previousSegmentPage").disabled = state.segmentPage <= 1;
  $("nextSegmentPage").disabled = state.segmentPage >= pageCount;

  const availableIds = new Set(state.segments.filter(isMergeSelectable).map((segment) => Number(segment.id)));
  [...state.selectedSegmentIds].forEach((id) => { if (!availableIds.has(id)) state.selectedSegmentIds.delete(id); });
  $("segmentsBody").innerHTML = pageSegments.map((segment, pageIndex) => {
    const segmentIndex = startIndex + pageIndex;
    const previewable = Boolean(segment.segment_url);
    const active = previewable && segment.segment_url === state.previewUrl;
    const title = segment.title || `鐗囨 ${segmentIndex + 1}`;
    const keywords = (segment.keywords || []).join(", ") || "-";
    const isMerge = segment.record_kind === "merge";
    const isMember = Boolean(segment.active_merge_id);
    const selectable = isMergeSelectable(segment);
    const selected = selectable && state.selectedSegmentIds.has(Number(segment.id));
    const titleMarkup = isMerge
      ? `<span class="manual-merge-badge">鎵嬪伐鍚堝苟 路 ${segment.member_count} 鏉?/span><span class="segment-title" title="${escapeHtml(title)}">${escapeHtml(title)}</span>`
      : `<span class="segment-title" title="${escapeHtml(title)}">${escapeHtml(title)}</span>`;
    let statusMarkup = segment.ignored
      ? '<span class="status-pill neutral">蹇界暐</span>'
      : segment.accepted
        ? '<span class="status-pill ok">閲囩敤</span>'
        : `<span class="status-pill warn">${escapeHtml(segment.reason || "鑸嶅純")}</span>`;
    if (isMember) statusMarkup = `<span class="status-pill merged">宸插苟鍏ユ墜宸ュ悎骞?/span>`;
    if (isMerge) statusMarkup = `<span class="status-pill merged">鎵嬪伐鍚堝苟${segment.ignored ? " 路 蹇界暐" : ""}</span>`;
    if (segment.archive_status === "ready") statusMarkup += '<span class="status-pill ok archive-state-pill">宸插綊妗?/span>';
    else if (segment.archive_status === "pending") statusMarkup += '<span class="status-pill warn archive-state-pill">褰掓。涓?/span>';
    else if (segment.archive_status === "error") statusMarkup += '<span class="status-pill bad archive-state-pill">褰掓。寮傚父</span>';
    let actions = `<button class="button secondary small segment-detail" data-segment-index="${segmentIndex}" type="button">璇︽儏</button>`;
    if (isMerge) {
      actions += `<button class="button secondary small edit-segment" data-segment-index="${segmentIndex}" type="button">缂栬緫</button><button class="button danger small cancel-merge" data-merge-id="${escapeHtml(segment.merge_id)}" type="button">鍙栨秷鍚堝苟</button>`;
    } else if (!isMember) {
      actions += `<button class="button secondary small edit-segment" data-segment-index="${segmentIndex}" type="button">缂栬緫</button>`;
    }
    return `
    <tr class="segment-row${previewable ? " is-previewable" : ""}${active ? " is-active" : ""}${segment.ignored ? " is-ignored" : ""}${isMerge ? " is-manual-merge" : ""}${isMember ? " is-merge-member" : ""}${selected ? " is-selected" : ""}"
        ${previewable ? `data-segment-index="${segmentIndex}" tabindex="0" aria-label="鎾斁 ${escapeHtml(title)}"` : ""}
        ${active ? 'aria-current="true"' : ""}>
      <td class="merge-select-cell">${selectable ? `<input class="merge-segment-checkbox" type="checkbox" data-segment-id="${segment.id}" aria-label="閫夋嫨 ${escapeHtml(title)}鐢ㄤ簬鍚堝苟" ${selected ? "checked" : ""}>` : ""}</td>
      <td>${segment.window_index + 1}</td>
      <td class="mono"><span class="segment-time-stack"><span>${formatRealTime(segment.absolute_start)}</span><span>${formatRealTime(segment.absolute_end)}</span></span></td>
      <td>${titleMarkup}</td>
      <td class="segment-content-type">${escapeHtml(segment.content_type || "-")}</td>
      <td class="segment-news-event-type">${escapeHtml(segment.news_event_type || "-")}</td>
      <td class="segment-topic">${escapeHtml(segment.topic || "-")}</td>
      <td><span class="segment-keywords" title="${escapeHtml(keywords)}">${escapeHtml(keywords)}</span></td>
      <td>${statusMarkup}</td>
      <td><span class="segment-actions">${actions}</span></td>
     </tr>`;
  }).join("") || '<tr><td class="empty-table-row" colspan="10">鏆傛棤鎷嗘潯缁撴灉</td></tr>';
  renderMergeSelectionBar();
}

function isMergeSelectable(segment) {
  return segment.record_kind !== "merge"
    && !segment.active_merge_id
    && Boolean(segment.accepted)
    && !segment.ignored;
}

function renderMergeSelectionBar() {
  const selected = state.segments.filter((segment) => state.selectedSegmentIds.has(Number(segment.id)) && isMergeSelectable(segment));
  $("mergeSelectionBar").hidden = selected.length === 0;
  $("mergeSelectionSummary").textContent = `宸查€夋嫨 ${selected.length} 鏉;
  $("openMergeDialogButton").disabled = selected.length < 2;
  if (!selected.length) {
    $("mergeSelectionTime").textContent = "";
    return;
  }
  const start = Math.min(...selected.map((segment) => Number(segment.global_start)));
  const end = Math.max(...selected.map((segment) => Number(segment.global_end)));
  $("mergeSelectionTime").textContent = `${formatSeconds(start)} 鈥?${formatSeconds(end)}`;
}

function openSegmentEdit(segmentIndex) {
  const segment = state.segments[segmentIndex];
  if (!segment) return;
  const isMerge = segment.record_kind === "merge";
  state.segmentEditTarget = {
    id: segment.id,
    mergeId: segment.merge_id,
    kind: isMerge ? "merge" : "segment",
    index: segmentIndex,
    restoredFromTask: false
  };
  $("segmentEditDialog").querySelector("h2").textContent = isMerge ? "缂栬緫鎵嬪伐鍚堝苟缁撴灉" : "缂栬緫鎷嗘潯缁撴灉";
  $("restoreSegmentEditButton").hidden = isMerge;
  $("segmentEditTitle").value = segment.title || "";
  const currentType = segment.content_type || "";
  const currentOption = $("segmentEditContentTypeCurrent");
  currentOption.value = "";
  if (CONTENT_TYPES.includes(currentType)) {
    currentOption.textContent = "淇濇寔褰撳墠鍊?;
    $("segmentEditContentType").value = currentType;
  } else {
    currentOption.textContent = `淇濇寔褰撳墠锛?{currentType || "鏈～鍐?}锛塦;
    $("segmentEditContentType").value = "";
  }
  $("segmentEditIgnored").checked = Boolean(segment.ignored);
  $("segmentEditRestoreHint").hidden = true;
  $("segmentEditError").hidden = true;
  $("segmentEditDialog").showModal();
}

async function restoreSegmentEdit() {
  const target = state.segmentEditTarget;
  const jobId = state.selectedJobId;
  if (!target || !jobId || target.kind === "merge") return;
  const button = $("restoreSegmentEditButton");
  button.disabled = true;
  button.textContent = "璇诲彇涓?;
  $("segmentEditError").hidden = true;
  try {
    const taskValues = await api(`/api/jobs/${jobId}/segments/${target.id}/task-values`);
    $("segmentEditTitle").value = taskValues.title || "";
    const taskType = taskValues.contentType || "";
    const currentOption = $("segmentEditContentTypeCurrent");
    if (CONTENT_TYPES.includes(taskType)) {
      currentOption.value = "";
      currentOption.textContent = "淇濇寔褰撳墠鍊?;
      $("segmentEditContentType").value = taskType;
    } else {
      currentOption.value = taskType;
      currentOption.textContent = `浠诲姟鍘熷€硷紙${taskType || "鏈～鍐?}锛塦;
      $("segmentEditContentType").value = taskType;
    }
    target.restoredFromTask = true;
    $("segmentEditRestoreHint").textContent = "宸蹭粠浠诲姟淇℃伅濉叆鍘熷鏍囬鍜岃妭鐩被鍨嬶紝鐐瑰嚮淇濆瓨鍚庣敓鏁堛€?;
    $("segmentEditRestoreHint").hidden = false;
  } catch (error) {
    $("segmentEditError").textContent = error.message;
    $("segmentEditError").hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = "杩樺師";
  }
}

async function submitSegmentEdit(event) {
  event.preventDefault();
  const target = state.segmentEditTarget;
  const jobId = state.selectedJobId;
  if (!target || !jobId) return;
  const submit = $("submitSegmentEditButton");
  submit.disabled = true;
  submit.textContent = "淇濆瓨涓?;
  try {
    const payload = {
      title: $("segmentEditTitle").value,
      ignored: $("segmentEditIgnored").checked
    };
    if (target.restoredFromTask) payload.restoredFromTask = true;
    if ($("segmentEditContentType").value || target.restoredFromTask) {
      payload.contentType = $("segmentEditContentType").value;
    }
    const path = target.kind === "merge"
      ? `/api/jobs/${jobId}/segment-merges/${target.mergeId}`
      : `/api/jobs/${jobId}/segments/${target.id}`;
    const updated = await api(path, {
      method: "PATCH",
      body: JSON.stringify(payload)
    });
    const index = state.segments.findIndex((segment) => target.kind === "merge"
      ? segment.merge_id === updated.merge_id
      : segment.id === updated.id);
    if (index >= 0) state.segments[index] = updated;
    if (state.previewUrl && updated.segment_url === state.previewUrl) {
      $("previewTitle").textContent = updated.title || `鐗囨 ${index + 1}`;
    }
    $("segmentEditDialog").close();
    state.segmentEditTarget = null;
    renderSegments();
    showToast(updated.ignored ? "缁撴灉宸叉爣璁颁负蹇界暐" : "缁撴灉宸蹭繚瀛?);
  } catch (error) {
    $("segmentEditError").textContent = error.message;
    $("segmentEditError").hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "淇濆瓨";
  }
}

async function openSegmentMerge() {
  const ids = state.segments
    .filter((segment) => state.selectedSegmentIds.has(Number(segment.id)) && isMergeSelectable(segment))
    .sort((left, right) => Number(left.global_start) - Number(right.global_start))
    .map((segment) => Number(segment.id));
  if (ids.length < 2 || !state.selectedJobId) return;
  await loadSegmentMergePreview(ids, ids[0], true);
}

async function loadSegmentMergePreview(segmentIds, primarySegmentId, openDialog = false) {
  $("segmentMergeError").hidden = true;
  try {
    const preview = await api(`/api/jobs/${state.selectedJobId}/segment-merges/preview`, {
      method: "POST",
      body: JSON.stringify({ segmentIds, primarySegmentId })
    });
    state.mergePreview = preview;
    $("segmentMergeMembers").innerHTML = preview.members.map((member) => `
      <label class="merge-member-option${member.primary ? " is-primary" : ""}">
        <input type="radio" name="mergePrimarySegment" value="${member.id}" ${member.primary ? "checked" : ""}>
        <span class="merge-member-order">绐楀彛 ${member.windowIndex + 1}</span>
        <strong>${escapeHtml(member.title || "鏈懡鍚嶇墖娈?)}</strong>
        <span>${escapeHtml(member.contentType || "鏈垎绫?)}</span>
        <span class="mono">${escapeHtml(formatRealTime(member.absoluteStart))} 鈥?${escapeHtml(formatRealTime(member.absoluteEnd))}</span>
      </label>`).join("");
    $("segmentMergeResultTitle").textContent = preview.result.title || "-";
    $("segmentMergeResultType").textContent = preview.result.contentType || "-";
    $("segmentMergeResultEvent").textContent = preview.result.newsEventType || "-";
    $("segmentMergeResultTime").textContent = `${formatRealTime(preview.result.absoluteStart)} 鈥?${formatRealTime(preview.result.absoluteEnd)}`;
    $("segmentMergeResultDuration").textContent = formatSeconds(preview.result.globalEnd - preview.result.globalStart);
    $("segmentMergeResultBoundary").textContent = `${Number(preview.gapSeconds).toFixed(2)}s / ${Number(preview.overlapSeconds).toFixed(2)}s`;
    if (openDialog) $("segmentMergeDialog").showModal();
  } catch (error) {
    if (openDialog) {
      showToast(error.message);
    } else {
      $("segmentMergeError").textContent = error.message;
      $("segmentMergeError").hidden = false;
    }
  }
}

async function submitSegmentMerge(event) {
  event.preventDefault();
  const preview = state.mergePreview;
  if (!preview || !state.selectedJobId) return;
  const submit = $("submitSegmentMergeButton");
  submit.disabled = true;
  submit.textContent = "鍚堝苟涓?;
  try {
    await api(`/api/jobs/${state.selectedJobId}/segment-merges`, {
      method: "POST",
      body: JSON.stringify({
        segmentIds: preview.segmentIds,
        primarySegmentId: preview.primarySegmentId,
        previewToken: preview.previewToken
      })
    });
    $("segmentMergeDialog").close();
    state.mergePreview = null;
    state.selectedSegmentIds.clear();
    showToast(`宸叉墜宸ュ悎骞?${preview.segmentIds.length} 鏉＄粨鏋渀);
    await loadJobs();
  } catch (error) {
    $("segmentMergeError").textContent = error.message;
    $("segmentMergeError").hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "纭鎵嬪伐鍚堝苟";
  }
}

async function cancelSegmentMerge(mergeId) {
  if (!state.selectedJobId) return;
  if (!window.confirm("鍙栨秷杩欐鎵嬪伐鍚堝苟锛熷師濮嬫媶鏉＄粨鏋滃皢鎭㈠涓虹嫭绔嬬粨鏋滃苟閲嶆柊鍙備笌 Excel 瀵煎嚭銆?)) return;
  try {
    await api(`/api/jobs/${state.selectedJobId}/segment-merges/${mergeId}`, { method: "DELETE" });
    showToast("宸插彇娑堟墜宸ュ悎骞讹紝鍘熷缁撴灉宸叉仮澶?);
    await loadJobs();
  } catch (error) {
    showToast(error.message);
  }
}

function exportDetailValue(value, kind = "text") {
  const textValue = String(value ?? "");
  if (!textValue) return '<span class="muted">锛堢┖锛?/span>';
  if (kind === "link" && /^https?:\/\//i.test(textValue)) {
    return `<a class="external-link" href="${escapeHtml(textValue)}" target="_blank" rel="noreferrer">${escapeHtml(textValue)}</a>`;
  }
  return escapeHtml(textValue);
}

function openSegmentDetail(segmentIndex) {
  const segment = state.segments[segmentIndex];
  const job = state.detail?.job;
  if (!segment || !job) return;
  const keywords = (segment.keywords || []).join(", ");
  const isMerge = segment.record_kind === "merge";
  const isMember = Boolean(segment.active_merge_id);
  const exportDate = String(segment.absolute_start || "").match(/^(\d{4}-\d{2}-\d{2})/)?.[1] || job.broadcast_date || "";
  const unmapped = "鏃犳槧灏勶紙鐣欑┖锛?;
  const fields = [
    ["鑺傜洰ID", unmapped, ""],
    ["program_name", "鏍囬锛堟渶缁堝€硷級", segment.title || ""],
    ["鍏抽敭瀛?, "鍏抽敭璇?, keywords],
    ["鎽樿", "鎽樿", segment.summary || ""],
    ["channel_name", "棰戦亾鍚?, job.channel_name || ""],
    ["begin_time", "瀹為檯寮€濮嬫椂闂?, segment.absolute_start ? formatRealTime(segment.absolute_start) : "", "mono"],
    ["end_time", "瀹為檯缁撴潫鏃堕棿", segment.absolute_end ? formatRealTime(segment.absolute_end) : "", "mono"],
    ["鏃ユ湡", "瀹為檯寮€濮嬫棩鏈燂紱鏃犲彲闈犳椂闂存椂鍥為€€涓氬姟鏃ユ湡", exportDate, "mono"],
    ["绫诲瀷1", "鑺傜洰绫诲瀷锛堟渶缁堝€硷級", segment.content_type || ""],
    ["绫诲瀷2", "鏂伴椈浜嬩欢绫诲瀷", segment.news_event_type || ""],
    ["鑺傜洰鏄惁棣栨挱", unmapped, ""],
    ["鍐呭鏄惁棣栨挱", unmapped, ""],
    ["鏄惁鏍忕洰", unmapped, ""],
    ["鏍忕洰鍚嶇О", unmapped, ""],
    ["鑺傜洰", unmapped, ""],
    ["闆嗘暟", unmapped, ""],
    ["鏄惁榛勯噾鏃舵", unmapped, ""],
    ["鏄惁鍥戒骇", unmapped, ""],
    ["鏄惁鏀跺畼", unmapped, ""],
    ["鏍囩", unmapped, ""],
    ["鏄惁鍚箍鍛?, unmapped, ""],
    ["澶勭悊鏂瑰紡", unmapped, ""],
    ["鏀瑰悗鑺傜洰鍚嶇О", unmapped, ""],
    ["鏀瑰悗寮€濮嬫椂闂?, unmapped, ""],
    ["鏀瑰悗缁撴潫鏃堕棿", unmapped, ""],
    ["鏀瑰悗鏃ユ湡", unmapped, ""],
    ["鏀瑰悗绫诲瀷1", unmapped, ""],
    ["鏀瑰悗绫诲瀷2", unmapped, ""],
    ["鏀瑰悗鑺傜洰鏄惁棣栨挱", unmapped, ""],
    ["鏀瑰悗鍐呭鏄惁棣栨挱", unmapped, ""],
    ["鏀瑰悗鏄惁鏍忕洰", unmapped, ""],
    ["鏀瑰悗鏍忕洰鍚嶇О", unmapped, ""],
    ["鏀瑰悗鑺傜洰", unmapped, ""],
    ["鏀瑰悗闆嗘暟", unmapped, ""],
    ["鏀瑰悗鏄惁榛勯噾鏃舵", unmapped, ""],
    ["鏀瑰悗鏄惁鍥戒骇", unmapped, ""],
    ["鏀瑰悗鏄惁鏀跺畼", unmapped, ""],
    ["鏀瑰悗鏍囩", unmapped, ""],
    ["鏄惁鍚堝苟", isMerge ? "鎵嬪伐鍚堝苟鏍囪" : unmapped, isMerge ? "鏄? : ""],
    ["鏄惁鎷嗘潯", unmapped, ""],
    ["鏀瑰悗鑺傜洰鏇剧敤鍚?, unmapped, ""],
    ["鏄惁绔嬪嵆", unmapped, ""]
  ];
  const eligible = Boolean(segment.accepted && !segment.ignored && !isMember && job.channel_id && job.broadcast_date);
  let eligibilityText = "璇ョ墖娈典細闅忛閬?Excel 瀵煎嚭銆?;
  if (isMerge) eligibilityText = "璇ユ墜宸ュ悎骞剁粨鏋滀細浣滀负涓€鏉¤褰曢殢棰戦亾 Excel 瀵煎嚭锛屽師濮嬫垚鍛樹笉鍐嶅崟鐙鍑恒€?;
  if (!segment.accepted) eligibilityText = "璇ョ墖娈典笉鏄渶缁堥噰鐢ㄧ粨鏋滐紝涓嶄細瀵煎嚭銆?;
  else if (segment.ignored) eligibilityText = "璇ョ墖娈靛凡鏍囪涓哄拷鐣ワ紝涓嶄細瀵煎嚭銆?;
  else if (isMember) eligibilityText = `璇ョ墖娈靛凡骞跺叆鎵嬪伐鍚堝苟鈥?{segment.active_merge_title || "鏈懡鍚?}鈥濓紝涓嶄細鍗曠嫭瀵煎嚭銆俙;
  else if (!job.channel_id || !job.broadcast_date) eligibilityText = "浣滀笟缂哄皯棰戦亾鎴栦笟鍔℃棩鏈燂紝涓嶄細瀵煎嚭銆?;
  $("segmentDetailTitle").textContent = segment.title || `鐗囨 ${segmentIndex + 1}`;
  $("segmentExportEligibility").textContent = eligibilityText;
  $("segmentExportEligibility").className = `export-eligibility ${eligible ? "is-exported" : "is-excluded"}`;
  $("segmentExportDetailGrid").innerHTML = fields.map(([excelField, systemField, value, kind]) => `
    <tr>
      <td>${escapeHtml(excelField)}</td>
      <td class="mapping-source${systemField === unmapped ? " is-unmapped" : ""}">${escapeHtml(systemField)}</td>
      <td class="${kind === "mono" ? "mono" : ""}">${exportDetailValue(value, kind)}</td>
    </tr>`).join("");
  const mergeDetail = $("manualMergeDetail");
  if (isMerge) {
    const members = segment.merge_members || [];
    mergeDetail.innerHTML = `
      <div class="manual-merge-heading"><strong>鎵嬪伐鍚堝苟 路 ${segment.member_count} 鏉?/strong><span>涓绘潯鐩彁渚涘唴瀹瑰瓧娈碉紝鏃堕棿瑕嗙洊鍏ㄩ儴鎴愬憳</span></div>
      <ol>${members.map((member) => `<li class="${member.role === "primary" ? "is-primary" : ""}"><span>${member.role === "primary" ? "涓绘潯鐩? : "鎴愬憳"}</span><strong>${escapeHtml(member.title || "鏈懡鍚嶇墖娈?)}</strong><span class="mono">${escapeHtml(formatRealTime(member.absolute_start))} 鈥?${escapeHtml(formatRealTime(member.absolute_end))}</span></li>`).join("")}</ol>`;
    mergeDetail.hidden = false;
  } else if (isMember) {
    mergeDetail.innerHTML = `<div class="manual-merge-heading"><strong>宸插苟鍏ユ墜宸ュ悎骞?/strong><span>${escapeHtml(segment.active_merge_title || "鏈懡鍚嶅悎骞剁粨鏋?)} 路 鍏?${segment.active_merge_member_count || 0} 鏉?/span></div>`;
    mergeDetail.hidden = false;
  } else {
    mergeDetail.innerHTML = "";
    mergeDetail.hidden = true;
  }
  $("segmentDetailDialog").showModal();
}

function previewSegment(segmentIndex) {
  const segment = state.segments[segmentIndex];
  if (!segment?.segment_url) return;

  const video = $("previewVideo");
  const sourceChanged = video.getAttribute("src") !== segment.segment_url;
  if (sourceChanged) video.pause();
  state.previewUrl = segment.segment_url;
  $("previewTitle").textContent = segment.title || `鐗囨 ${segmentIndex + 1}`;
  $("previewTime").textContent = `${formatRealTime(segment.absolute_start)} - ${formatRealTime(segment.absolute_end)} | ${formatSeconds(segment.global_start)} - ${formatSeconds(segment.global_end)}`;
  $("previewStatus").textContent = "姝ｅ湪鍔犺浇濯掍綋淇℃伅...";
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
      $("previewStatus").textContent = "濯掍綋宸插氨缁?;
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
  $("previewTitle").textContent = "鏈€夋嫨鐗囨";
  $("previewTime").textContent = "";
  $("previewStatus").textContent = "鏈€夋嫨鐗囨";
  $("previewStatus").className = "preview-status";
  document.querySelectorAll(".segment-row.is-active").forEach((row) => {
    row.classList.remove("is-active");
    row.removeAttribute("aria-current");
  });
}

function renderActions(job) {
  $("detailActions").innerHTML = `<a class="button secondary small external-link" href="/api/jobs/${escapeHtml(job.id)}/result?download=true">涓嬭浇 JSON</a>`;
}

async function controlJob(jobId, action, button = null) {
  if (button) button.disabled = true;
  try {
    await api(`/api/jobs/${jobId}/${action}`, { method: "POST" });
    showToast("浣滀笟鐘舵€佸凡鏇存柊");
    await loadJobs();
  } catch (error) {
    showToast(error.message);
    if (button) button.disabled = false;
  }
}

async function saveTimeReference() {
  const jobId = state.selectedJobId;
  const value = $("summaryRealTime").value;
  if (!jobId || !value) {
    showToast("璇峰～鍐欏畬鏁寸殑鐪熷疄鏃堕棿鍩哄噯");
    return;
  }
  const button = $("saveTimeReferenceButton");
  button.disabled = true;
  button.textContent = "淇濆瓨涓?;
  try {
    const result = await api(`/api/jobs/${jobId}/time-reference`, {
      method: "PATCH",
      body: JSON.stringify({ programStartTime: value })
    });
    showToast(`鏃堕棿鍩哄噯宸蹭慨姝ｏ紝宸插悓姝?${result.updatedSegmentCount} 涓墖娈礰);
    await loadJobs();
    await loadDetail(jobId, false);
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "淇濆瓨";
  }
}

function openCreate() {
  if (!state.channels.length) {
    showToast("璇峰厛鍒涘缓棰戦亾");
    $("channelsDialog").showModal();
    return;
  }
  $("createError").hidden = true;
  $("createDialog").showModal();
}

function openChannels() {
  $("channelError").hidden = true;
  $("channelsDialog").showModal();
}

async function submitChannel(event) {
  event.preventDefault();
  const name = $("newChannelName").value.trim();
  if (!name) return;
  try {
    await api("/api/channels", { method: "POST", body: JSON.stringify({ name }) });
    $("newChannelName").value = "";
    $("channelError").hidden = true;
    await loadChannels();
  } catch (error) {
    $("channelError").textContent = error.message;
    $("channelError").hidden = false;
  }
}

async function renameChannel(channelId) {
  const channel = state.channels.find((item) => item.id === channelId);
  const name = window.prompt("鏂扮殑棰戦亾鍚?, channel?.name || "");
  if (!name || name.trim() === channel?.name) return;
  try {
    await api(`/api/channels/${channelId}`, {
      method: "PATCH",
      body: JSON.stringify({ name: name.trim() })
    });
    await loadChannels();
    await loadJobs();
  } catch (error) { showToast(error.message); }
}

async function deleteChannel(channelId) {
  const channel = state.channels.find((item) => item.id === channelId);
  if (!window.confirm(`鍒犻櫎棰戦亾鈥?{channel?.name || ""}鈥濓紵`)) return;
  try {
    await api(`/api/channels/${channelId}`, { method: "DELETE" });
    await loadChannels();
  } catch (error) { showToast(error.message); }
}

function exportSelectedChannel() {
  const channelId = $("channelFilter").value;
  if (channelId) window.location.href = `/api/channels/${encodeURIComponent(channelId)}/export.xlsx`;
}

function openResplit(target) {
  state.resplitTarget = target;
  $("resplitWindow").textContent = String(target.windowIndex + 1);
  $("resplitTaskId").textContent = target.taskId;
  $("resplitError").hidden = true;
  $("resplitDialog").showModal();
}

async function submitResplit(event) {
  event.preventDefault();
  const target = state.resplitTarget;
  if (!target) return;
  const submit = $("submitResplitButton");
  submit.disabled = true;
  submit.textContent = "鎻愪氦涓?;
  try {
    const result = await api(`/api/jobs/${target.jobId}/windows/${target.windowIndex}/resplit`, {
      method: "POST",
      body: JSON.stringify({ taskId: target.taskId })
    });
    $("resplitDialog").close();
    state.resplitTarget = null;
    showToast(`鏂颁换鍔?${result.taskId} 宸茶繘鍏ラ噸鏂版媶鍒嗘祦绋媊);
    await loadJobs();
  } catch (error) {
    $("resplitError").textContent = error.message;
    $("resplitError").hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "纭閲嶆柊鎷嗗垎";
  }
}

async function openTailRebuild(target) {
  try {
    const preview = await api(
      `/api/jobs/${target.jobId}/windows/${target.windowIndex}/tail-rebuild-preview`
    );
    state.tailRebuildTarget = { ...target, preview };
    $("tailRebuildWindow").textContent = String(preview.startWindowNumber);
    $("tailRebuildKeepCount").textContent = `${preview.keptWindowCount} 涓獥鍙;
    $("tailRebuildDeleteCount").textContent = `${preview.deletedWindowCount} 涓獥鍙?/ ${preview.deletedAttemptCount} 娆?attempt / ${preview.deletedSegmentCount} 鏉＄粨鏋渀;
    $("tailRebuildTaskCount").textContent = `${preview.oldTaskCount} 涓紙淇濈暀锛屼笉鍒犻櫎锛塦;
    $("tailRebuildMergeImpactRow").hidden = !preview.invalidatedMergeCount;
    $("tailRebuildMergeImpact").textContent = preview.invalidatedMergeCount
      ? `${preview.invalidatedMergeCount} 缁勫皢鍥犳垚鍛樿鍒犻櫎鑰岃嚜鍔ㄥけ鏁坄
      : "";
    $("tailRebuildSourceStart").textContent = formatSeconds(preview.sourceStart);
    $("tailRebuildAbsoluteStart").textContent = preview.absoluteStart ? formatDate(preview.absoluteStart) : "-";
    $("tailRebuildConfirmationLabel").textContent = preview.confirmationText;
    $("tailRebuildConfirmation").value = "";
    $("tailRebuildConfirmation").placeholder = preview.confirmationText;
    $("tailRebuildError").hidden = true;
    $("tailRebuildDialog").showModal();
  } catch (error) {
    showToast(error.message);
  }
}

async function submitTailRebuild(event) {
  event.preventDefault();
  const target = state.tailRebuildTarget;
  if (!target) return;
  const submit = $("submitTailRebuildButton");
  submit.disabled = true;
    submit.textContent = "姝ｅ湪鍒犻櫎骞堕噸鏂板叆闃?;
  try {
    await api(`/api/jobs/${target.jobId}/windows/${target.windowIndex}/tail-rebuild`, {
      method: "POST",
      body: JSON.stringify({
        previewToken: target.preview.previewToken,
        confirmationText: $("tailRebuildConfirmation").value
      })
    });
    $("tailRebuildDialog").close();
    state.tailRebuildTarget = null;
    showToast(`宸插垹闄ょ獥鍙?${target.windowIndex + 1} 鍙婁箣鍚庣殑鏁版嵁锛屼綔涓氬凡閲嶆柊鍏ラ槦`);
    await loadJobs();
  } catch (error) {
    $("tailRebuildError").textContent = error.message;
    $("tailRebuildError").hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "纭鍒犻櫎骞堕噸璺?;
  }
}

async function acceptOverlap(target) {
  const confirmed = window.confirm(
    `灏嗘帴绾充换鍔?${target.taskId} 宸蹭繚瀛樼殑閲嶆媶缁撴灉锛屽苟淇濈暀鍚庣画绐楀彛鐜版湁缁撴灉銆俓n\n` +
    "浜ょ晫澶勪細淇濈暀鏃堕棿閲嶅彔锛屼笉浼氬啀娆¤皟鐢?iSlice銆傛槸鍚︾户缁紵"
  );
  if (!confirmed) return;
  try {
    await api(`/api/jobs/${target.jobId}/windows/${target.windowIndex}/accept-overlap`, {
      method: "POST",
      body: JSON.stringify({ taskId: target.taskId })
    });
    showToast("宸叉帴绾抽噸鎷嗙粨鏋滃苟淇濈暀璺ㄧ獥鍙ｆ椂闂撮噸鍙?);
    await loadJobs();
    await loadDetail(target.jobId);
  } catch (error) {
    showToast(error.message);
  }
}

async function submitCreate(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = Object.fromEntries(form.entries());
  if (!payload.programStartTime) delete payload.programStartTime;
  const submit = $("submitCreateButton");
  submit.disabled = true;
  submit.textContent = /^https?:\/\//i.test(payload.sourcePath) ? "涓嬭浇涓? : "鍒涘缓涓?;
  try {
    let job;
    try {
      job = await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
    } catch (error) {
      if (error.detail?.code !== "channel_date_exists") throw error;
      const channel = state.channels.find((item) => item.id === payload.channelId);
      const confirmed = window.confirm(
        `棰戦亾鈥?{channel?.name || ""}鈥濆湪 ${payload.broadcastDate} 宸叉湁浣滀笟銆俓n\n瑕嗙洊鍚庢棫浣滀笟灏嗚浆涓哄巻鍙茶褰曪紝鏄惁缁х画锛焋
      );
      if (!confirmed) return;
      payload.overwrite = true;
      job = await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
    }
    $("createDialog").close();
    event.currentTarget.reset();
    state.jobPage = 1;
    showToast(payload.overwrite ? "浣滀笟宸茶鐩? : "浣滀笟宸插垱寤?);
    await loadJobs();
    await loadDetail(job.id, true);
  } catch (error) {
    $("createError").textContent = error.message;
    $("createError").hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "鍒涘缓浣滀笟";
  }
}

$("openCreateButton").addEventListener("click", openCreate);
$("manageChannelsButton").addEventListener("click", openChannels);
$("closeCreateButton").addEventListener("click", () => $("createDialog").close());
$("cancelCreateButton").addEventListener("click", () => $("createDialog").close());
$("closeChannelsButton").addEventListener("click", () => $("channelsDialog").close());
$("doneChannelsButton").addEventListener("click", () => $("channelsDialog").close());
$("closeTimeRefreshButton").addEventListener("click", closeTimeRefresh);
$("cancelTimeRefreshButton").addEventListener("click", closeTimeRefresh);
$("closeResplitButton").addEventListener("click", () => $("resplitDialog").close());
$("cancelResplitButton").addEventListener("click", () => $("resplitDialog").close());
$("closeTailRebuildButton").addEventListener("click", () => $("tailRebuildDialog").close());
$("cancelTailRebuildButton").addEventListener("click", () => $("tailRebuildDialog").close());
$("closeSegmentMergeButton").addEventListener("click", () => $("segmentMergeDialog").close());
$("cancelSegmentMergeButton").addEventListener("click", () => $("segmentMergeDialog").close());
$("closeSegmentEditButton").addEventListener("click", () => $("segmentEditDialog").close());
$("cancelSegmentEditButton").addEventListener("click", () => $("segmentEditDialog").close());
$("restoreSegmentEditButton").addEventListener("click", restoreSegmentEdit);
$("closeSegmentDetailButton").addEventListener("click", () => $("segmentDetailDialog").close());
$("doneSegmentDetailButton").addEventListener("click", () => $("segmentDetailDialog").close());
$("closePreviewButton").addEventListener("click", resetPreview);
$("segmentsBody").addEventListener("click", (event) => {
  if (event.target.closest(".merge-segment-checkbox")) return;
  const cancelMergeButton = event.target.closest(".cancel-merge");
  if (cancelMergeButton) {
    cancelSegmentMerge(cancelMergeButton.dataset.mergeId);
    return;
  }
  const detailButton = event.target.closest(".segment-detail");
  if (detailButton) {
    openSegmentDetail(Number(detailButton.dataset.segmentIndex));
    return;
  }
  const editButton = event.target.closest(".edit-segment");
  if (editButton) {
    openSegmentEdit(Number(editButton.dataset.segmentIndex));
    return;
  }
  const row = event.target.closest(".segment-row.is-previewable");
  if (row) previewSegment(Number(row.dataset.segmentIndex));
});
$("segmentsBody").addEventListener("change", (event) => {
  const checkbox = event.target.closest(".merge-segment-checkbox");
  if (!checkbox) return;
  const segmentId = Number(checkbox.dataset.segmentId);
  if (checkbox.checked) state.selectedSegmentIds.add(segmentId);
  else state.selectedSegmentIds.delete(segmentId);
  renderSegments();
});
$("segmentsBody").addEventListener("keydown", (event) => {
  if (event.target.closest("button")) return;
  const row = event.target.closest(".segment-row.is-previewable");
  if (row && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault();
    previewSegment(Number(row.dataset.segmentIndex));
  }
});
$("previewVideo").addEventListener("loadedmetadata", () => {
  if ($('previewVideo').paused) {
    $("previewStatus").textContent = "濯掍綋宸插氨缁?;
    $("previewStatus").className = "preview-status ready";
  }
});
$("previewVideo").addEventListener("playing", () => {
  $("previewStatus").textContent = "姝ｅ湪鎾斁";
  $("previewStatus").className = "preview-status ready";
});
$("previewVideo").addEventListener("pause", () => {
  const video = $("previewVideo");
  if (video.getAttribute("src") && !video.ended) {
    $("previewStatus").textContent = "宸叉殏鍋?;
    $("previewStatus").className = "preview-status";
  }
});
$("previewVideo").addEventListener("ended", () => {
  $("previewStatus").textContent = "鎾斁缁撴潫";
  $("previewStatus").className = "preview-status";
});
$("previewVideo").addEventListener("error", () => {
  if (!$("previewVideo").getAttribute("src")) return;
  $("previewStatus").textContent = "娴忚鍣ㄦ棤娉曞姞杞借鐗囨锛岃浣跨敤鏂扮獥鍙ｆ墦寮€銆?;
  $("previewStatus").className = "preview-status error";
});
$("createForm").addEventListener("submit", submitCreate);
$("channelCreateForm").addEventListener("submit", submitChannel);
$("timeRefreshForm").addEventListener("submit", submitTimeRefresh);
$("resplitForm").addEventListener("submit", submitResplit);
$("tailRebuildForm").addEventListener("submit", submitTailRebuild);
$("segmentMergeForm").addEventListener("submit", submitSegmentMerge);
$("segmentEditForm").addEventListener("submit", submitSegmentEdit);
$("openMergeDialogButton").addEventListener("click", openSegmentMerge);
$("clearMergeSelectionButton").addEventListener("click", () => {
  state.selectedSegmentIds.clear();
  renderSegments();
});
$("segmentMergeMembers").addEventListener("change", (event) => {
  const radio = event.target.closest('input[name="mergePrimarySegment"]');
  if (!radio || !state.mergePreview) return;
  loadSegmentMergePreview(
    state.mergePreview.segmentIds,
    Number(radio.value),
    false
  );
});
$("refreshButton").addEventListener("click", () => loadJobs().catch((error) => showToast(error.message)));
$("saveTimeReferenceButton").addEventListener("click", saveTimeReference);
$("summaryRealTime").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    saveTimeReference();
  }
});
[$("statusFilter"), $("channelFilter"), $("isliceFilter"), $("dateFilter")].forEach((control) => control.addEventListener("change", () => {
  state.jobPage = 1;
  $("exportChannelButton").disabled = !$("channelFilter").value;
  loadJobs().catch((error) => showToast(error.message));
}));
$("exportChannelButton").addEventListener("click", exportSelectedChannel);
$("jobPageSize").addEventListener("change", () => {
  state.jobPageSize = Number($("jobPageSize").value);
  state.jobPage = 1;
  loadJobs().catch((error) => showToast(error.message));
});
$("previousJobPage").addEventListener("click", () => {
  if (state.jobPage > 1) state.jobPage -= 1;
  loadJobs().catch((error) => showToast(error.message));
});
$("nextJobPage").addEventListener("click", () => {
  if (state.jobPage < state.jobTotalPages) state.jobPage += 1;
  loadJobs().catch((error) => showToast(error.message));
});
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

Promise.all([loadHealth(), loadChannels().then(loadJobs), loadISliceInstances()]).catch((error) => showToast(error.message));
const priorityButton = document.createElement("button");
priorityButton.className = "button secondary";
priorityButton.type = "button";
priorityButton.textContent = "璋冨害绛栫暐";
priorityButton.addEventListener("click", () => configureSchedulingPriority().catch((error) => showToast(error.message)));
document.querySelector(".topbar-actions")?.insertBefore(priorityButton, $("openCreateButton"));
window.setInterval(() => loadJobs().catch(() => {}), 5000);
window.setInterval(() => loadHealth().catch(() => {}), 30000);

