const state = {
  jobs: [], channels: [], isliceInstances: [], selectedJobId: null, detail: null, segments: [],
  jobPage: 1, jobPageSize: 20, jobTotal: 0, jobTotalPages: 1,
  segmentPage: 1, previewUrl: null, resplitTarget: null, tailRebuildTarget: null,
  segmentEditTarget: null, selectedSegmentIds: new Set(), mergePreview: null,
  timeRefreshTarget: null
};
const SEGMENTS_PER_PAGE = 10;
const CONTENT_TYPES = [
  "新闻", "电视剧", "电影", "综艺", "少儿", "体育", "纪录片", "科教",
  "文艺", "生活服务", "商业广告", "公益广告", "电视购物", "其他"
];

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
    badge.textContent = response.ok ? "服务就绪" : "依赖异常";
    badge.className = `health ${response.ok ? "ok" : "bad"}`;
  } catch (_) {
    badge.textContent = "连接失败";
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
      <td class="reviewed-cell"><input class="review-checkbox" data-job-id="${escapeHtml(job.id)}" type="checkbox" ${job.reviewed ? "checked" : ""} aria-label="${job.reviewed ? "取消" : "标记"}${escapeHtml(job.channel_name || sourceName(job))}已审核"></td>
      <td>${escapeHtml(formatDate(job.created_at))}</td>
      <td><span class="job-row-actions">
        <button class="button secondary small view-job" data-job-id="${escapeHtml(job.id)}" type="button">查看</button>
        <button class="button secondary small refresh-job-time" data-job-id="${escapeHtml(job.id)}" type="button">重新取时</button>
        ${renderJobControlActions(job)}
      </span></td>
    </tr>`).join("");
  body.querySelectorAll(".view-job").forEach((button) => button.addEventListener("click", () => loadDetail(button.dataset.jobId, true)));
  body.querySelectorAll(".refresh-job-time").forEach((button) => button.addEventListener("click", () => openTimeRefresh(button.dataset.jobId)));
  body.querySelectorAll(".control-job").forEach((button) => button.addEventListener("click", () => controlJob(button.dataset.jobId, button.dataset.action, button)));
  body.querySelectorAll(".review-checkbox").forEach((checkbox) => checkbox.addEventListener("change", () => updateJobReview(checkbox)));
  $("jobPageSummary").textContent = `共 ${state.jobTotal} 条`;
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
    ? `当前小任务 ${Number(job.current_task_window || 0) + 1}：${taskStatus} ${taskProgress.toFixed(1)}%`
    : "暂无运行中的小任务";
  return `<div class="job-progress-cell">
    <span><span class="mini-progress"><span style="width:${overall}%"></span></span>${overall.toFixed(1)}%</span>
    <span class="muted job-current-task" title="${escapeHtml(taskId)}">${escapeHtml(taskLabel)}</span>
  </div>`;
}

function renderJobControlActions(job) {
  const actions = [];
  const jobId = escapeHtml(job.id);
  if (["pending_schedule", "queued", "running"].includes(job.status)) {
    actions.push(`<button class="button warning small control-job" data-job-id="${jobId}" data-action="pause" type="button">暂停</button>`);
  }
  if (["paused", "failed"].includes(job.status)) {
    actions.push(`<button class="button primary small control-job" data-job-id="${jobId}" data-action="resume" type="button">继续</button>`);
  }
  if (!["completed", "stopped"].includes(job.status)) {
    actions.push(`<button class="button danger small control-job" data-job-id="${jobId}" data-action="stop" type="button">停止</button>`);
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
    checkbox.setAttribute("aria-label", `${updated.reviewed ? "取消" : "标记"}${updated.channel_name || sourceName(updated)}已审核`);
    showToast(updated.reviewed ? "作业已标记为审核完成" : "已取消作业审核标记");
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
  submit.textContent = "识别中";
  $("timeRefreshError").hidden = true;
  try {
    const result = await api(`/api/jobs/${job.id}/refresh-time-reference`, {
      method: "POST"
    });
    closeTimeRefresh();
    showToast(`首帧时间已更新，尝试 ${result.attemptCount} 次，同步 ${result.updatedSegmentCount} 个片段`);
    await loadJobs();
  } catch (error) {
    $("timeRefreshError").textContent = error.message;
    $("timeRefreshError").hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "确认重新识别";
  }
}

async function loadChannels() {
  const selectedFilter = $("channelFilter").value;
  const selectedCreate = $("createChannelId").value;
  state.channels = await api("/api/channels");
  const options = state.channels.map((channel) =>
    `<option value="${escapeHtml(channel.id)}">${escapeHtml(channel.name)}</option>`
  ).join("");
  $("channelFilter").innerHTML = `<option value="">全部频道</option>${options}`;
  $("createChannelId").innerHTML = `<option value="">请选择频道</option>${options}`;
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
  $("isliceFilter").innerHTML = `<option value="">全部节点</option>${filterOptions}`;
  if (state.isliceInstances.some((item) => item.base_url === selectedFilter)) $("isliceFilter").value = selectedFilter;
  const options = state.isliceInstances.filter((item) => item.schedulable)
    .map((item) => `<option value="${escapeHtml(item.base_url)}">${escapeHtml(item.name || item.source_id)} (${escapeHtml(item.base_url)})</option>`).join("");
  $("createISliceBaseUrl").innerHTML = `<option value="">自动选择</option>${options}`;
  if (state.isliceInstances.some((item) => item.schedulable && item.base_url === selected)) $("createISliceBaseUrl").value = selected;
}

function renderChannels() {
  $("channelsList").innerHTML = state.channels.map((channel) => `
    <div class="channel-row">
      <div><strong>${escapeHtml(channel.name)}</strong><span>${channel.job_count || 0} 个当前作业</span></div>
      <div>
        <button class="button secondary small rename-channel" data-channel-id="${escapeHtml(channel.id)}" type="button">重命名</button>
        <button class="button danger small delete-channel" data-channel-id="${escapeHtml(channel.id)}" type="button" ${channel.job_count ? "disabled" : ""}>删除</button>
      </div>
    </div>`).join("") || '<p class="empty-channel-list">暂无频道，请先添加。</p>';
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
  $("detailTitle").textContent = sourceName(job);
  $("detailPath").textContent = sourceDisplay(job);
  $("summaryChannel").textContent = job.channel_name || "-";
  $("summaryBroadcastDate").textContent = job.broadcast_date || "-";
  $("summaryISlice").textContent = job.islice_base_url || "-";
  $("summaryISlice").title = job.islice_base_url || "";
  $("summaryStatus").innerHTML = `<span class="status-pill ${statusClass(job.status)}">${escapeHtml(statusNames[job.status] || job.status)}</span>`;
  $("summaryDuration").textContent = formatSeconds(job.source_duration);
  $("summaryWindow").textContent = `${job.current_window} / ${job.total_windows}`;
  $("summaryCutMode").textContent = job.cut_mode === "copy" ? "流复制" : "重编码";
  const referenceNames = {
    ocr: "OCR",
    manual_fallback: "手工回退",
    manual_override: "手工修正",
    unavailable: "未识别"
  };
  const timeInput = $("summaryRealTime");
  if (document.activeElement !== timeInput) {
    timeInput.value = toDateTimeLocal(job.program_start_time);
  }
  const referenceLabel = referenceNames[job.time_reference_source] || "已有数据";
  $("timeReferenceMeta").textContent = job.time_reference_source === "ocr" && job.time_reference_frame_offset
    ? `${referenceLabel}（取样 +${Number(job.time_reference_frame_offset).toFixed(0)} 秒，已反推首帧）`
    : referenceLabel;
  $("detailProgress").style.width = `${Math.max(0, Math.min(100, job.progress || 0))}%`;
  $("detailError").hidden = !job.error_message;
  $("detailError").textContent = job.error_message || "";
  $("detailWarnings").innerHTML = (job.warnings || []).map((warning) => `<p>${escapeHtml(warning)}</p>`).join("");
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
  if (!attempt) return '<span class="muted">尚未提交</span>';
  const progress = Math.max(0, Math.min(100, Number(attempt.progress || 0)));
  const serviceStatus = attempt.service_status || attempt.status || "pending";
  return `
    <div class="task-progress-cell">
      <span class="mono window-task-id" title="${escapeHtml(attempt.task_id)}">${escapeHtml(attempt.task_id)}</span>
      <span class="muted mono">真实起点 ${escapeHtml(formatRealTime(attempt.program_start_time))}</span>
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
  const title = disabled ? "作业及小任务结束后才能重新拆分" : "创建全新任务 ID 重新拆分";
  const resplitButton = `<button class="button secondary small resplit-window" type="button"
    data-window-index="${windowItem.window_index}" data-task-id="${escapeHtml(attempt.task_id)}"
    title="${title}" ${disabled ? "disabled" : ""}>重新拆分</button>`;
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
        title="接纳已保存的重拆结果，不再调用 iSlice">允许重叠并合并</button>`
    : "";
  return `<span class="window-actions">${resplitButton}${overlapButton}</span>`;
}

function renderWindowActions(job, windowItem, attempt) {
  const resplit = renderResplitAction(job, windowItem, attempt);
  const jobReady = ["paused", "completed", "failed", "stopped"].includes(job.status);
  const disabled = !jobReady;
  const rebuildButton = `<button class="button danger small tail-rebuild-window" type="button"
    data-window-index="${windowItem.window_index}"
    title="删除此窗口及后续结果，并使用新的任务 ID 从这里重新执行" ${disabled ? "disabled" : ""}>从此窗口重跑</button>`;
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
    const title = segment.title || `片段 ${segmentIndex + 1}`;
    const keywords = (segment.keywords || []).join(", ") || "-";
    const isMerge = segment.record_kind === "merge";
    const isMember = Boolean(segment.active_merge_id);
    const selectable = isMergeSelectable(segment);
    const selected = selectable && state.selectedSegmentIds.has(Number(segment.id));
    const titleMarkup = isMerge
      ? `<span class="manual-merge-badge">手工合并 · ${segment.member_count} 条</span><span class="segment-title" title="${escapeHtml(title)}">${escapeHtml(title)}</span>`
      : `<span class="segment-title" title="${escapeHtml(title)}">${escapeHtml(title)}</span>`;
    let statusMarkup = segment.ignored
      ? '<span class="status-pill neutral">忽略</span>'
      : segment.accepted
        ? '<span class="status-pill ok">采用</span>'
        : `<span class="status-pill warn">${escapeHtml(segment.reason || "舍弃")}</span>`;
    if (isMember) statusMarkup = `<span class="status-pill merged">已并入手工合并</span>`;
    if (isMerge) statusMarkup = `<span class="status-pill merged">手工合并${segment.ignored ? " · 忽略" : ""}</span>`;
    let actions = `<button class="button secondary small segment-detail" data-segment-index="${segmentIndex}" type="button">详情</button>`;
    if (isMerge) {
      actions += `<button class="button secondary small edit-segment" data-segment-index="${segmentIndex}" type="button">编辑</button><button class="button danger small cancel-merge" data-merge-id="${escapeHtml(segment.merge_id)}" type="button">取消合并</button>`;
    } else if (!isMember) {
      actions += `<button class="button secondary small edit-segment" data-segment-index="${segmentIndex}" type="button">编辑</button>`;
    }
    return `
    <tr class="segment-row${previewable ? " is-previewable" : ""}${active ? " is-active" : ""}${segment.ignored ? " is-ignored" : ""}${isMerge ? " is-manual-merge" : ""}${isMember ? " is-merge-member" : ""}${selected ? " is-selected" : ""}"
        ${previewable ? `data-segment-index="${segmentIndex}" tabindex="0" aria-label="播放 ${escapeHtml(title)}"` : ""}
        ${active ? 'aria-current="true"' : ""}>
      <td class="merge-select-cell">${selectable ? `<input class="merge-segment-checkbox" type="checkbox" data-segment-id="${segment.id}" aria-label="选择 ${escapeHtml(title)}用于合并" ${selected ? "checked" : ""}>` : ""}</td>
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
  }).join("") || '<tr><td class="empty-table-row" colspan="10">暂无拆条结果</td></tr>';
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
  $("mergeSelectionSummary").textContent = `已选择 ${selected.length} 条`;
  $("openMergeDialogButton").disabled = selected.length < 2;
  if (!selected.length) {
    $("mergeSelectionTime").textContent = "";
    return;
  }
  const start = Math.min(...selected.map((segment) => Number(segment.global_start)));
  const end = Math.max(...selected.map((segment) => Number(segment.global_end)));
  $("mergeSelectionTime").textContent = `${formatSeconds(start)} – ${formatSeconds(end)}`;
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
  $("segmentEditDialog").querySelector("h2").textContent = isMerge ? "编辑手工合并结果" : "编辑拆条结果";
  $("restoreSegmentEditButton").hidden = isMerge;
  $("segmentEditTitle").value = segment.title || "";
  const currentType = segment.content_type || "";
  const currentOption = $("segmentEditContentTypeCurrent");
  currentOption.value = "";
  if (CONTENT_TYPES.includes(currentType)) {
    currentOption.textContent = "保持当前值";
    $("segmentEditContentType").value = currentType;
  } else {
    currentOption.textContent = `保持当前（${currentType || "未填写"}）`;
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
  button.textContent = "读取中";
  $("segmentEditError").hidden = true;
  try {
    const taskValues = await api(`/api/jobs/${jobId}/segments/${target.id}/task-values`);
    $("segmentEditTitle").value = taskValues.title || "";
    const taskType = taskValues.contentType || "";
    const currentOption = $("segmentEditContentTypeCurrent");
    if (CONTENT_TYPES.includes(taskType)) {
      currentOption.value = "";
      currentOption.textContent = "保持当前值";
      $("segmentEditContentType").value = taskType;
    } else {
      currentOption.value = taskType;
      currentOption.textContent = `任务原值（${taskType || "未填写"}）`;
      $("segmentEditContentType").value = taskType;
    }
    target.restoredFromTask = true;
    $("segmentEditRestoreHint").textContent = "已从任务信息填入原始标题和节目类型，点击保存后生效。";
    $("segmentEditRestoreHint").hidden = false;
  } catch (error) {
    $("segmentEditError").textContent = error.message;
    $("segmentEditError").hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = "还原";
  }
}

async function submitSegmentEdit(event) {
  event.preventDefault();
  const target = state.segmentEditTarget;
  const jobId = state.selectedJobId;
  if (!target || !jobId) return;
  const submit = $("submitSegmentEditButton");
  submit.disabled = true;
  submit.textContent = "保存中";
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
      $("previewTitle").textContent = updated.title || `片段 ${index + 1}`;
    }
    $("segmentEditDialog").close();
    state.segmentEditTarget = null;
    renderSegments();
    showToast(updated.ignored ? "结果已标记为忽略" : "结果已保存");
  } catch (error) {
    $("segmentEditError").textContent = error.message;
    $("segmentEditError").hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "保存";
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
        <span class="merge-member-order">窗口 ${member.windowIndex + 1}</span>
        <strong>${escapeHtml(member.title || "未命名片段")}</strong>
        <span>${escapeHtml(member.contentType || "未分类")}</span>
        <span class="mono">${escapeHtml(formatRealTime(member.absoluteStart))} – ${escapeHtml(formatRealTime(member.absoluteEnd))}</span>
      </label>`).join("");
    $("segmentMergeResultTitle").textContent = preview.result.title || "-";
    $("segmentMergeResultType").textContent = preview.result.contentType || "-";
    $("segmentMergeResultEvent").textContent = preview.result.newsEventType || "-";
    $("segmentMergeResultTime").textContent = `${formatRealTime(preview.result.absoluteStart)} – ${formatRealTime(preview.result.absoluteEnd)}`;
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
  submit.textContent = "合并中";
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
    showToast(`已手工合并 ${preview.segmentIds.length} 条结果`);
    await loadJobs();
  } catch (error) {
    $("segmentMergeError").textContent = error.message;
    $("segmentMergeError").hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "确认手工合并";
  }
}

async function cancelSegmentMerge(mergeId) {
  if (!state.selectedJobId) return;
  if (!window.confirm("取消这次手工合并？原始拆条结果将恢复为独立结果并重新参与 Excel 导出。")) return;
  try {
    await api(`/api/jobs/${state.selectedJobId}/segment-merges/${mergeId}`, { method: "DELETE" });
    showToast("已取消手工合并，原始结果已恢复");
    await loadJobs();
  } catch (error) {
    showToast(error.message);
  }
}

function exportDetailValue(value, kind = "text") {
  const textValue = String(value ?? "");
  if (!textValue) return '<span class="muted">（空）</span>';
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
  const unmapped = "无映射（留空）";
  const fields = [
    ["节目ID", unmapped, ""],
    ["program_name", "标题（最终值）", segment.title || ""],
    ["关键字", "关键词", keywords],
    ["摘要", "摘要", segment.summary || ""],
    ["channel_name", "频道名", job.channel_name || ""],
    ["begin_time", "实际开始时间", segment.absolute_start ? formatRealTime(segment.absolute_start) : "", "mono"],
    ["end_time", "实际结束时间", segment.absolute_end ? formatRealTime(segment.absolute_end) : "", "mono"],
    ["日期", "实际开始日期；无可靠时间时回退业务日期", exportDate, "mono"],
    ["类型1", "节目类型（最终值）", segment.content_type || ""],
    ["类型2", "新闻事件类型", segment.news_event_type || ""],
    ["节目是否首播", unmapped, ""],
    ["内容是否首播", unmapped, ""],
    ["是否栏目", unmapped, ""],
    ["栏目名称", unmapped, ""],
    ["节目", unmapped, ""],
    ["集数", unmapped, ""],
    ["是否黄金时段", unmapped, ""],
    ["是否国产", unmapped, ""],
    ["是否收官", unmapped, ""],
    ["标签", unmapped, ""],
    ["是否含广告", unmapped, ""],
    ["处理方式", unmapped, ""],
    ["改后节目名称", unmapped, ""],
    ["改后开始时间", unmapped, ""],
    ["改后结束时间", unmapped, ""],
    ["改后日期", unmapped, ""],
    ["改后类型1", unmapped, ""],
    ["改后类型2", unmapped, ""],
    ["改后节目是否首播", unmapped, ""],
    ["改后内容是否首播", unmapped, ""],
    ["改后是否栏目", unmapped, ""],
    ["改后栏目名称", unmapped, ""],
    ["改后节目", unmapped, ""],
    ["改后集数", unmapped, ""],
    ["改后是否黄金时段", unmapped, ""],
    ["改后是否国产", unmapped, ""],
    ["改后是否收官", unmapped, ""],
    ["改后标签", unmapped, ""],
    ["是否合并", isMerge ? "手工合并标记" : unmapped, isMerge ? "是" : ""],
    ["是否拆条", unmapped, ""],
    ["改后节目曾用名", unmapped, ""],
    ["是否立即", unmapped, ""]
  ];
  const eligible = Boolean(segment.accepted && !segment.ignored && !isMember && job.channel_id && job.broadcast_date);
  let eligibilityText = "该片段会随频道 Excel 导出。";
  if (isMerge) eligibilityText = "该手工合并结果会作为一条记录随频道 Excel 导出，原始成员不再单独导出。";
  if (!segment.accepted) eligibilityText = "该片段不是最终采用结果，不会导出。";
  else if (segment.ignored) eligibilityText = "该片段已标记为忽略，不会导出。";
  else if (isMember) eligibilityText = `该片段已并入手工合并“${segment.active_merge_title || "未命名"}”，不会单独导出。`;
  else if (!job.channel_id || !job.broadcast_date) eligibilityText = "作业缺少频道或业务日期，不会导出。";
  $("segmentDetailTitle").textContent = segment.title || `片段 ${segmentIndex + 1}`;
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
      <div class="manual-merge-heading"><strong>手工合并 · ${segment.member_count} 条</strong><span>主条目提供内容字段，时间覆盖全部成员</span></div>
      <ol>${members.map((member) => `<li class="${member.role === "primary" ? "is-primary" : ""}"><span>${member.role === "primary" ? "主条目" : "成员"}</span><strong>${escapeHtml(member.title || "未命名片段")}</strong><span class="mono">${escapeHtml(formatRealTime(member.absolute_start))} – ${escapeHtml(formatRealTime(member.absolute_end))}</span></li>`).join("")}</ol>`;
    mergeDetail.hidden = false;
  } else if (isMember) {
    mergeDetail.innerHTML = `<div class="manual-merge-heading"><strong>已并入手工合并</strong><span>${escapeHtml(segment.active_merge_title || "未命名合并结果")} · 共 ${segment.active_merge_member_count || 0} 条</span></div>`;
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
  $("detailActions").innerHTML = `<a class="button secondary small external-link" href="/api/jobs/${escapeHtml(job.id)}/result?download=true">下载 JSON</a>`;
}

async function controlJob(jobId, action, button = null) {
  if (button) button.disabled = true;
  try {
    await api(`/api/jobs/${jobId}/${action}`, { method: "POST" });
    showToast("作业状态已更新");
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
    showToast("请填写完整的真实时间基准");
    return;
  }
  const button = $("saveTimeReferenceButton");
  button.disabled = true;
  button.textContent = "保存中";
  try {
    const result = await api(`/api/jobs/${jobId}/time-reference`, {
      method: "PATCH",
      body: JSON.stringify({ programStartTime: value })
    });
    showToast(`时间基准已修正，已同步 ${result.updatedSegmentCount} 个片段`);
    await loadJobs();
    await loadDetail(jobId, false);
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "保存";
  }
}

function openCreate() {
  if (!state.channels.length) {
    showToast("请先创建频道");
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
  const name = window.prompt("新的频道名", channel?.name || "");
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
  if (!window.confirm(`删除频道“${channel?.name || ""}”？`)) return;
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
  submit.textContent = "提交中";
  try {
    const result = await api(`/api/jobs/${target.jobId}/windows/${target.windowIndex}/resplit`, {
      method: "POST",
      body: JSON.stringify({ taskId: target.taskId })
    });
    $("resplitDialog").close();
    state.resplitTarget = null;
    showToast(`新任务 ${result.taskId} 已进入重新拆分流程`);
    await loadJobs();
  } catch (error) {
    $("resplitError").textContent = error.message;
    $("resplitError").hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "确认重新拆分";
  }
}

async function openTailRebuild(target) {
  try {
    const preview = await api(
      `/api/jobs/${target.jobId}/windows/${target.windowIndex}/tail-rebuild-preview`
    );
    state.tailRebuildTarget = { ...target, preview };
    $("tailRebuildWindow").textContent = String(preview.startWindowNumber);
    $("tailRebuildKeepCount").textContent = `${preview.keptWindowCount} 个窗口`;
    $("tailRebuildDeleteCount").textContent = `${preview.deletedWindowCount} 个窗口 / ${preview.deletedAttemptCount} 次 attempt / ${preview.deletedSegmentCount} 条结果`;
    $("tailRebuildTaskCount").textContent = `${preview.oldTaskCount} 个（保留，不删除）`;
    $("tailRebuildMergeImpactRow").hidden = !preview.invalidatedMergeCount;
    $("tailRebuildMergeImpact").textContent = preview.invalidatedMergeCount
      ? `${preview.invalidatedMergeCount} 组将因成员被删除而自动失效`
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
    submit.textContent = "正在删除并重新入队";
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
    showToast(`已删除窗口 ${target.windowIndex + 1} 及之后的数据，作业已重新入队`);
    await loadJobs();
  } catch (error) {
    $("tailRebuildError").textContent = error.message;
    $("tailRebuildError").hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "确认删除并重跑";
  }
}

async function acceptOverlap(target) {
  const confirmed = window.confirm(
    `将接纳任务 ${target.taskId} 已保存的重拆结果，并保留后续窗口现有结果。\n\n` +
    "交界处会保留时间重叠，不会再次调用 iSlice。是否继续？"
  );
  if (!confirmed) return;
  try {
    await api(`/api/jobs/${target.jobId}/windows/${target.windowIndex}/accept-overlap`, {
      method: "POST",
      body: JSON.stringify({ taskId: target.taskId })
    });
    showToast("已接纳重拆结果并保留跨窗口时间重叠");
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
  submit.textContent = /^https?:\/\//i.test(payload.sourcePath) ? "下载中" : "创建中";
  try {
    let job;
    try {
      job = await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
    } catch (error) {
      if (error.detail?.code !== "channel_date_exists") throw error;
      const channel = state.channels.find((item) => item.id === payload.channelId);
      const confirmed = window.confirm(
        `频道“${channel?.name || ""}”在 ${payload.broadcastDate} 已有作业。\n\n覆盖后旧作业将转为历史记录，是否继续？`
      );
      if (!confirmed) return;
      payload.overwrite = true;
      job = await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
    }
    $("createDialog").close();
    event.currentTarget.reset();
    state.jobPage = 1;
    showToast(payload.overwrite ? "作业已覆盖" : "作业已创建");
    await loadJobs();
    await loadDetail(job.id, true);
  } catch (error) {
    $("createError").textContent = error.message;
    $("createError").hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "创建作业";
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
window.setInterval(() => loadJobs().catch(() => {}), 5000);
window.setInterval(() => loadHealth().catch(() => {}), 30000);
