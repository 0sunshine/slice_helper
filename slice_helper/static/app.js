const state = {
  jobs: [], channels: [], selectedJobId: null, detail: null, segments: [],
  jobPage: 1, jobPageSize: 20, jobTotal: 0, jobTotalPages: 1,
  segmentPage: 1, previewUrl: null, resplitTarget: null, segmentEditTarget: null
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
      <td><span class="status-pill ${statusClass(job.status)}">${escapeHtml(statusNames[job.status] || job.status)}</span></td>
      <td><span class="mini-progress"><span style="width:${Math.max(0, Math.min(100, job.progress || 0))}%"></span></span>${Number(job.progress || 0).toFixed(1)}%</td>
      <td>${job.current_window}/${job.total_windows}</td>
      <td>${job.accepted_segment_count}</td>
      <td class="reviewed-cell"><input class="review-checkbox" data-job-id="${escapeHtml(job.id)}" type="checkbox" ${job.reviewed ? "checked" : ""} aria-label="${job.reviewed ? "取消" : "标记"}${escapeHtml(job.channel_name || sourceName(job))}已审核"></td>
      <td>${escapeHtml(formatDate(job.created_at))}</td>
      <td><button class="button secondary small view-job" data-job-id="${escapeHtml(job.id)}" type="button">查看</button></td>
    </tr>`).join("");
  body.querySelectorAll(".view-job").forEach((button) => button.addEventListener("click", () => loadDetail(button.dataset.jobId, true)));
  body.querySelectorAll(".review-checkbox").forEach((checkbox) => checkbox.addEventListener("change", () => updateJobReview(checkbox)));
  $("jobPageSummary").textContent = `共 ${state.jobTotal} 条`;
  $("jobPageInfo").textContent = `${state.jobPage} / ${state.jobTotalPages}`;
  $("previousJobPage").disabled = state.jobPage <= 1;
  $("nextJobPage").disabled = state.jobPage >= state.jobTotalPages;
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
      <td>${renderResplitAction(job, windowItem, latestAttempts.get(windowItem.window_index))}</td>
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
  const title = disabled ? "作业及小任务结束后才能重新拆分" : "使用相同任务 ID 重新拆分";
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
    <tr class="segment-row${previewable ? " is-previewable" : ""}${active ? " is-active" : ""}${segment.ignored ? " is-ignored" : ""}"
        ${previewable ? `data-segment-index="${segmentIndex}" tabindex="0" aria-label="播放 ${escapeHtml(title)}"` : ""}
        ${active ? 'aria-current="true"' : ""}>
      <td>${segment.window_index + 1}</td>
      <td class="mono"><span class="segment-time-stack"><span>${formatRealTime(segment.absolute_start)}</span><span>${formatRealTime(segment.absolute_end)}</span></span></td>
      <td><span class="segment-title" title="${escapeHtml(title)}">${escapeHtml(title)}</span></td>
      <td class="segment-content-type">${escapeHtml(segment.content_type || "-")}</td>
      <td class="segment-news-event-type">${escapeHtml(segment.news_event_type || "-")}</td>
      <td class="segment-topic">${escapeHtml(segment.topic || "-")}</td>
      <td><span class="segment-keywords" title="${escapeHtml(keywords)}">${escapeHtml(keywords)}</span></td>
      <td><span class="status-pill ${segment.ignored ? "neutral" : segment.accepted ? "ok" : "warn"}">${segment.ignored ? "忽略" : segment.accepted ? "采用" : escapeHtml(segment.reason || "舍弃")}</span></td>
      <td><span class="segment-actions"><button class="button secondary small segment-detail" data-segment-index="${segmentIndex}" type="button">详情</button><button class="button secondary small edit-segment" data-segment-index="${segmentIndex}" type="button">编辑</button></span></td>
     </tr>`;
  }).join("") || '<tr><td class="empty-table-row" colspan="9">暂无拆条结果</td></tr>';
}

function openSegmentEdit(segmentIndex) {
  const segment = state.segments[segmentIndex];
  if (!segment) return;
  state.segmentEditTarget = { id: segment.id, index: segmentIndex };
  $("segmentEditTitle").value = segment.title || "";
  const currentType = segment.content_type || "";
  const currentOption = $("segmentEditContentTypeCurrent");
  if (CONTENT_TYPES.includes(currentType)) {
    currentOption.textContent = "保持当前值";
    $("segmentEditContentType").value = currentType;
  } else {
    currentOption.textContent = `保持当前（${currentType || "未填写"}）`;
    $("segmentEditContentType").value = "";
  }
  $("segmentEditIgnored").checked = Boolean(segment.ignored);
  $("segmentEditError").hidden = true;
  $("segmentEditDialog").showModal();
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
    if ($("segmentEditContentType").value) {
      payload.contentType = $("segmentEditContentType").value;
    }
    const updated = await api(`/api/jobs/${jobId}/segments/${target.id}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    });
    const index = state.segments.findIndex((segment) => segment.id === updated.id);
    if (index >= 0) state.segments[index] = updated;
    if (state.previewUrl && updated.segment_url === state.previewUrl) {
      $("previewTitle").textContent = updated.title || `片段 ${index + 1}`;
    }
    $("segmentEditDialog").close();
    state.segmentEditTarget = null;
    renderSegments();
    showToast(updated.ignored ? "拆条结果已标记为忽略" : "拆条结果已保存");
  } catch (error) {
    $("segmentEditError").textContent = error.message;
    $("segmentEditError").hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "保存";
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
    ["是否合并", unmapped, ""],
    ["是否拆条", unmapped, ""],
    ["改后节目曾用名", unmapped, ""],
    ["是否立即", unmapped, ""]
  ];
  const eligible = Boolean(segment.accepted && !segment.ignored && job.channel_id && job.broadcast_date);
  let eligibilityText = "该片段会随频道 Excel 导出。";
  if (!segment.accepted) eligibilityText = "该片段不是最终采用结果，不会导出。";
  else if (segment.ignored) eligibilityText = "该片段已标记为忽略，不会导出。";
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
    await api(`/api/jobs/${target.jobId}/windows/${target.windowIndex}/resplit`, {
      method: "POST",
      body: JSON.stringify({ taskId: target.taskId })
    });
    $("resplitDialog").close();
    state.resplitTarget = null;
    showToast(`任务 ${target.taskId} 已进入重新拆分流程`);
    await loadJobs();
  } catch (error) {
    $("resplitError").textContent = error.message;
    $("resplitError").hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "确认重新拆分";
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
$("closeResplitButton").addEventListener("click", () => $("resplitDialog").close());
$("cancelResplitButton").addEventListener("click", () => $("resplitDialog").close());
$("closeSegmentEditButton").addEventListener("click", () => $("segmentEditDialog").close());
$("cancelSegmentEditButton").addEventListener("click", () => $("segmentEditDialog").close());
$("closeSegmentDetailButton").addEventListener("click", () => $("segmentDetailDialog").close());
$("doneSegmentDetailButton").addEventListener("click", () => $("segmentDetailDialog").close());
$("closePreviewButton").addEventListener("click", resetPreview);
$("segmentsBody").addEventListener("click", (event) => {
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
$("resplitForm").addEventListener("submit", submitResplit);
$("segmentEditForm").addEventListener("submit", submitSegmentEdit);
$("refreshButton").addEventListener("click", () => loadJobs().catch((error) => showToast(error.message)));
$("saveTimeReferenceButton").addEventListener("click", saveTimeReference);
$("summaryRealTime").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    saveTimeReference();
  }
});
[$("statusFilter"), $("channelFilter"), $("dateFilter")].forEach((control) => control.addEventListener("change", () => {
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

Promise.all([loadHealth(), loadChannels().then(loadJobs)]).catch((error) => showToast(error.message));
window.setInterval(() => loadJobs().catch(() => {}), 5000);
window.setInterval(() => loadHealth().catch(() => {}), 30000);
