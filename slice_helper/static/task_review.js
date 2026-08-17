const state = {
  tasks: [], channels: [], instances: [], page: 1, pageSize: 20,
  total: 0, totalPages: 1, selectedTask: null, segments: [], queryTimer: null,
  segmentContentType: "", segmentEditTarget: null,
  isSeeking: false, pendingSeek: null
};

const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
})[character]);
const reviewLabels = {
  unreviewed: "未审核", hold: "暂保留", approved: "通过", rejected: "不通过"
};
const CONTENT_TYPES = [
  "新闻", "电视剧", "电影", "综艺", "少儿", "体育", "纪录片", "科教",
  "文艺", "生活服务", "商业广告", "公益广告", "电视购物", "其他"
];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options
  });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const detail = (await response.json()).detail;
      message = typeof detail === "string" ? detail : detail?.message || message;
    } catch (_) { /* no-op */ }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { toast.hidden = true; }, 3500);
}

function formatDate(value) {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString("zh-CN", { hour12: false });
}

function formatSeconds(value) {
  const total = Math.max(0, Number(value || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = Math.floor(total % 60);
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function formatMediaTime(value) {
  if (!Number.isFinite(Number(value))) return "--:--:--";
  return formatSeconds(value);
}

function configureSeek(duration, currentTime = 0) {
  const seek = $("taskReviewSeek");
  const safeDuration = Number.isFinite(Number(duration)) && Number(duration) > 0 ? Number(duration) : 0;
  const safeCurrent = Math.min(Math.max(0, Number(currentTime) || 0), safeDuration || 0);
  seek.max = String(safeDuration);
  seek.value = String(safeCurrent);
  seek.disabled = safeDuration <= 0;
  $("taskReviewSeekCurrent").textContent = formatMediaTime(safeCurrent);
  $("taskReviewSeekDuration").textContent = formatMediaTime(safeDuration || NaN);
}

function applyPendingSeek() {
  const video = $("taskReviewVideo");
  if (state.pendingSeek === null || video.readyState < 1 || !Number.isFinite(video.duration)) return;
  video.currentTime = Math.min(Math.max(0, state.pendingSeek), video.duration);
  state.pendingSeek = null;
}

function sourceName(task) {
  const source = task.source_url || task.source_path || "";
  try {
    const url = new URL(source);
    return decodeURIComponent(url.pathname.split("/").filter(Boolean).pop() || url.hostname);
  } catch (_) {
    return source.split(/[\\/]/).pop() || source;
  }
}

function instanceName(baseUrl) {
  return state.instances.find((item) => item.base_url === baseUrl)?.name || baseUrl || "-";
}

function reviewOptions(selected) {
  return Object.entries(reviewLabels).map(([value, label]) =>
    `<option value="${value}" ${selected === value ? "selected" : ""}>${label}</option>`
  ).join("");
}

async function loadFilters() {
  [state.channels, state.instances] = await Promise.all([
    api("/api/channels"), api("/api/islice-instances")
  ]);
  $("taskChannelFilter").innerHTML = '<option value="">全部频道</option>' + state.channels.map((item) =>
    `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`
  ).join("");
  $("taskISliceFilter").innerHTML = '<option value="">全部节点</option>' + state.instances.map((item) =>
    `<option value="${escapeHtml(item.base_url)}">${escapeHtml(item.name)}</option>`
  ).join("");
}

async function loadTasks() {
  const params = new URLSearchParams({ page: String(state.page), pageSize: String(state.pageSize) });
  if ($("taskChannelFilter").value) params.set("channelId", $("taskChannelFilter").value);
  if ($("taskISliceFilter").value) params.set("isliceBaseUrl", $("taskISliceFilter").value);
  if ($("taskDateFilter").value) params.set("broadcastDate", $("taskDateFilter").value);
  if ($("taskReviewStatusFilter").value) params.set("reviewStatus", $("taskReviewStatusFilter").value);
  if ($("taskContentTypeFilter").value) params.set("contentType", $("taskContentTypeFilter").value);
  if ($("taskReviewQuery").value.trim()) params.set("query", $("taskReviewQuery").value.trim());
  const result = await api(`/api/task-reviews?${params}`);
  state.tasks = result.items;
  state.page = result.page;
  state.total = result.total;
  state.totalPages = result.totalPages;
  renderTasks();
}

function renderTasks() {
  $("taskReviewCount").textContent = state.total;
  $("emptyTaskReviews").hidden = state.tasks.length > 0;
  $("taskReviewBody").innerHTML = state.tasks.map((task) => {
    const status = task.review_status || "unreviewed";
    const canResplit = ["paused", "completed", "failed", "stopped"].includes(task.job_status);
    const accepted = Number(task.accepted_segment_count || 0);
    const ignored = Number(task.ignored_segment_count || 0);
    const aiScore = task.ai_review_score === null || task.ai_review_score === undefined
      ? '<span class="muted">-</span>' : `<strong>${Number(task.ai_review_score).toFixed(1)}</strong>`;
    return `<tr class="task-review-row review-${escapeHtml(status)}" data-attempt-id="${task.id}">
      <td class="mono task-review-time">${escapeHtml(formatDate(task.submitted_at))}</td>
      <td><span class="mono task-id-cell" title="${escapeHtml(task.task_id)}">${escapeHtml(task.task_id)}</span><small class="table-subtext" title="${escapeHtml(task.source_url || task.source_path)}">${escapeHtml(sourceName(task))}</small></td>
      <td>${escapeHtml(task.channel_name || "-")}<small class="table-subtext">${escapeHtml(task.broadcast_date || "-")}</small></td>
      <td>${escapeHtml(instanceName(task.islice_base_url))}<small class="table-subtext mono">${escapeHtml(task.islice_base_url)}</small></td>
      <td>第 ${Number(task.window_index) + 1} 个<small class="table-subtext mono">${escapeHtml(formatSeconds(task.requested_start))}</small></td>
      <td>${Number(task.segment_count || 0)} 条<small class="table-subtext">采用 ${accepted} / 忽略 ${ignored}</small></td>
      <td class="mono task-review-time">${escapeHtml(formatDate(task.finished_at))}</td>
      <td><select class="task-review-status review-status-${escapeHtml(status)}" data-attempt-id="${task.id}" aria-label="${escapeHtml(task.task_id)} 人为审核状态">${reviewOptions(status)}</select></td>
      <td class="task-ai-score">${aiScore}${task.ai_review_comment ? '<small class="table-subtext">有评论</small>' : ""}</td>
      <td><span class="task-review-actions">
        <button class="button secondary small open-task-review" data-attempt-id="${task.id}" type="button">查看片段</button>
        <a class="button secondary small button-link" href="/?jobId=${encodeURIComponent(task.job_id)}">所属作业</a>
        <button class="button danger small resplit-task" data-attempt-id="${task.id}" type="button" ${canResplit ? "" : "disabled"}>重新拆分</button>
      </span></td>
    </tr>`;
  }).join("");
  $("taskReviewPageSummary").textContent = `共 ${state.total} 条`;
  $("taskReviewPageInfo").textContent = `${state.page} / ${state.totalPages}`;
  $("previousTaskReviewPage").disabled = state.page <= 1;
  $("nextTaskReviewPage").disabled = state.page >= state.totalPages;
}

async function saveReviewStatus(select) {
  const task = state.tasks.find((item) => item.id === Number(select.dataset.attemptId));
  if (!task) return;
  const previous = task.review_status || "unreviewed";
  select.disabled = true;
  try {
    const updated = await api(`/api/task-reviews/${task.id}`, {
      method: "PATCH", body: JSON.stringify({ reviewStatus: select.value })
    });
    task.review_status = updated.review_status;
    task.reviewed_at = updated.reviewed_at;
    showToast(`已标记为${reviewLabels[updated.review_status]}`);
    renderTasks();
  } catch (error) {
    select.value = previous;
    showToast(error.message, true);
  } finally {
    select.disabled = false;
  }
}

async function openTaskReview(attemptId) {
  state.segmentContentType = $("taskContentTypeFilter").value;
  const params = new URLSearchParams();
  if (state.segmentContentType) params.set("contentType", state.segmentContentType);
  const query = params.toString();
  const suffix = query ? `?${query}` : "";
  const payload = await api(`/api/task-reviews/${attemptId}/segments${suffix}`);
  state.selectedTask = { ...state.tasks.find((item) => item.id === attemptId), ...payload.task };
  state.segments = payload.segments;
  const task = state.selectedTask;
  $("taskReviewDialogTitle").textContent = task.task_id;
  $("taskReviewDialogSubtitle").textContent = instanceName(task.islice_base_url);
  $("taskDialogChannel").textContent = task.channel_name || "-";
  $("taskDialogDate").textContent = task.broadcast_date || "-";
  $("taskDialogWindow").textContent = `第 ${Number(task.window_index) + 1} 个`;
  $("taskDialogSubmitted").textContent = formatDate(task.submitted_at);
  $("taskAIReviewScore").value = task.ai_review_score === null || task.ai_review_score === undefined ? "" : task.ai_review_score;
  $("taskAIReviewComment").value = task.ai_review_comment || "";
  $("openTaskJob").href = `/?jobId=${encodeURIComponent(task.job_id)}`;
  $("resplitReviewedTask").disabled = !["paused", "completed", "failed", "stopped"].includes(task.job_status);
  resetVideo();
  renderSegments();
  $("taskReviewDialog").showModal();
}

function segmentStatus(segment) {
  if (segment.ignored) return '<span class="status-pill neutral">忽略</span>';
  if (segment.accepted) return '<span class="status-pill ok">采用</span>';
  return `<span class="status-pill warn">${escapeHtml(segment.reason || "舍弃")}</span>`;
}

function renderSegments() {
  const visibleSegments = state.segments
    .map((segment, index) => ({ segment, index }))
    .filter(({ segment }) => !state.segmentContentType || segment.content_type === state.segmentContentType);
  $("taskReviewSegmentCount").textContent = state.segmentContentType
    ? `共 ${visibleSegments.length} 条（仅显示${state.segmentContentType}）`
    : `共 ${visibleSegments.length} 条`;
  $("taskReviewSegmentsBody").innerHTML = visibleSegments.map(({ segment, index }, displayIndex) => {
    const start = segment.absolute_start || formatSeconds(segment.global_start);
    const end = segment.absolute_end || formatSeconds(segment.global_end);
    return `<tr class="task-review-segment-row ${segment.segment_url ? "is-previewable" : ""}" data-segment-index="${index}" tabindex="${segment.segment_url ? "0" : "-1"}">
      <td>${displayIndex + 1}</td><td><strong>${escapeHtml(segment.title || `片段 ${displayIndex + 1}`)}</strong></td>
      <td>${escapeHtml(segment.content_type || "-")}</td>
      <td class="mono"><span>${escapeHtml(start)}</span><small class="table-subtext">${escapeHtml(end)}</small></td>
      <td>${segmentStatus(segment)}</td>
      <td><button class="button secondary small task-review-segment-edit" data-segment-index="${index}" type="button">编辑</button></td>
    </tr>`;
  }).join("") || `<tr><td class="empty-table-row" colspan="6">${state.segmentContentType ? `该任务没有${escapeHtml(state.segmentContentType)}类型的片段` : "该任务没有可显示的片段"}</td></tr>`;
  document.querySelector(".task-review-segments-scroll").scrollTop = 0;
}

function openTaskSegmentEdit(index) {
  const segment = state.segments[index];
  if (!segment || !state.selectedTask) return;
  state.segmentEditTarget = { index, id: segment.id, restoredFromTask: false };
  $("taskSegmentEditTitle").value = segment.title || "";
  const currentType = segment.content_type || "";
  const currentOption = $("taskSegmentEditContentTypeCurrent");
  currentOption.value = "";
  if (CONTENT_TYPES.includes(currentType)) {
    currentOption.textContent = "保持当前值";
    $("taskSegmentEditContentType").value = currentType;
  } else {
    currentOption.textContent = `保持当前（${currentType || "未填写"}）`;
    $("taskSegmentEditContentType").value = "";
  }
  $("taskSegmentEditIgnored").checked = Boolean(segment.ignored);
  $("taskSegmentEditRestoreHint").hidden = true;
  $("taskSegmentEditError").hidden = true;
  $("taskSegmentEditDialog").showModal();
}

async function restoreTaskSegmentEdit() {
  const target = state.segmentEditTarget;
  const jobId = state.selectedTask?.job_id;
  if (!target || !jobId) return;
  const button = $("restoreTaskSegmentEdit");
  button.disabled = true;
  button.textContent = "读取中";
  $("taskSegmentEditError").hidden = true;
  try {
    const taskValues = await api(`/api/jobs/${jobId}/segments/${target.id}/task-values`);
    $("taskSegmentEditTitle").value = taskValues.title || "";
    const taskType = taskValues.contentType || "";
    const currentOption = $("taskSegmentEditContentTypeCurrent");
    if (CONTENT_TYPES.includes(taskType)) {
      currentOption.value = "";
      currentOption.textContent = "保持当前值";
      $("taskSegmentEditContentType").value = taskType;
    } else {
      currentOption.value = taskType;
      currentOption.textContent = `任务原值（${taskType || "未填写"}）`;
      $("taskSegmentEditContentType").value = taskType;
    }
    target.restoredFromTask = true;
    $("taskSegmentEditRestoreHint").textContent = "已从任务信息填入原始标题和节目类型，点击保存后生效。";
    $("taskSegmentEditRestoreHint").hidden = false;
  } catch (error) {
    $("taskSegmentEditError").textContent = error.message;
    $("taskSegmentEditError").hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = "还原";
  }
}

async function submitTaskSegmentEdit(event) {
  event.preventDefault();
  const target = state.segmentEditTarget;
  const jobId = state.selectedTask?.job_id;
  if (!target || !jobId) return;
  const submit = $("submitTaskSegmentEdit");
  submit.disabled = true;
  submit.textContent = "保存中";
  $("taskSegmentEditError").hidden = true;
  try {
    const payload = {
      title: $("taskSegmentEditTitle").value,
      ignored: $("taskSegmentEditIgnored").checked
    };
    if (target.restoredFromTask) payload.restoredFromTask = true;
    if ($("taskSegmentEditContentType").value || target.restoredFromTask) {
      payload.contentType = $("taskSegmentEditContentType").value;
    }
    const updated = await api(`/api/jobs/${jobId}/segments/${target.id}`, {
      method: "PATCH", body: JSON.stringify(payload)
    });
    const current = state.segments[target.index];
    if (current) {
      const resolvedMedia = {
        segment_url: current.segment_url,
        cover_img_url: current.cover_img_url,
        archive_status: current.archive_status,
        archive_error: current.archive_error
      };
      state.segments[target.index] = { ...current, ...updated, ...resolvedMedia };
    }
    if ($("taskReviewSegmentTitle").dataset.segmentId === String(target.id)) {
      $("taskReviewSegmentTitle").textContent = updated.title || `片段 ${target.index + 1}`;
    }
    $("taskSegmentEditDialog").close();
    state.segmentEditTarget = null;
    renderSegments();
    const filteredOut = state.segmentContentType && updated.content_type !== state.segmentContentType;
    showToast(filteredOut ? "结果已保存；该片段不再符合当前节目类型筛选" : (updated.ignored ? "结果已标记为忽略" : "结果已保存"));
    loadTasks().catch((error) => showToast(`结果已保存，但任务列表刷新失败：${error.message}`, true));
  } catch (error) {
    $("taskSegmentEditError").textContent = error.message;
    $("taskSegmentEditError").hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "保存";
  }
}

function previewSegment(index) {
  const segment = state.segments[index];
  if (!segment?.segment_url) return;
  const video = $("taskReviewVideo");
  const expectedDuration = Math.max(0, Number(segment.global_end) - Number(segment.global_start));
  state.isSeeking = false;
  state.pendingSeek = null;
  configureSeek(expectedDuration);
  video.src = segment.segment_url;
  video.load();
  video.play().catch(() => {});
  $("taskReviewSegmentTitle").textContent = segment.title || `片段 ${index + 1}`;
  $("taskReviewSegmentTitle").dataset.segmentId = String(segment.id);
  $("taskReviewSegmentTime").textContent = `${segment.absolute_start || formatSeconds(segment.global_start)} - ${segment.absolute_end || formatSeconds(segment.global_end)}`;
  $("taskReviewVideoStatus").textContent = segment.archive_status === "ready" ? "正在读取归档视频" : "正在加载";
  $("openTaskSegmentVideo").href = segment.segment_url;
  $("openTaskSegmentVideo").hidden = false;
  document.querySelectorAll(".task-review-segment-row.is-active").forEach((row) => row.classList.remove("is-active"));
  document.querySelector(`.task-review-segment-row[data-segment-index="${index}"]`)?.classList.add("is-active");
}

function resetVideo() {
  const video = $("taskReviewVideo");
  video.pause();
  video.removeAttribute("src");
  video.load();
  state.isSeeking = false;
  state.pendingSeek = null;
  configureSeek(0);
  $("taskReviewSegmentTitle").textContent = "请选择片段";
  delete $("taskReviewSegmentTitle").dataset.segmentId;
  $("taskReviewSegmentTime").textContent = "";
  $("taskReviewVideoStatus").textContent = "";
  $("openTaskSegmentVideo").hidden = true;
}

async function saveAIReview(event) {
  event.preventDefault();
  const task = state.selectedTask;
  if (!task) return;
  const rawScore = $("taskAIReviewScore").value.trim();
  const score = rawScore === "" ? null : Number(rawScore);
  if (score !== null && (!Number.isFinite(score) || score < 0 || score > 10)) {
    showToast("AI 评分必须在 0 到 10 之间", true);
    return;
  }
  const button = $("saveTaskAIReview");
  button.disabled = true;
  try {
    const updated = await api(`/api/task-reviews/${task.id}`, {
      method: "PATCH",
      body: JSON.stringify({ aiReviewScore: score, aiReviewComment: $("taskAIReviewComment").value })
    });
    task.ai_review_score = updated.ai_review_score;
    task.ai_review_comment = updated.ai_review_comment;
    const listed = state.tasks.find((item) => item.id === task.id);
    if (listed) Object.assign(listed, task);
    renderTasks();
    showToast("AI 审核已保存");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function resplitTask(attemptId) {
  const task = state.tasks.find((item) => item.id === attemptId) || state.selectedTask;
  if (!task) return;
  if (!window.confirm(`确认重新拆分 ${task.task_id}？\n\n系统会创建新的 task ID，本次已完成结果将被替换。`)) return;
  try {
    await api(`/api/jobs/${task.job_id}/windows/${task.window_index}/resplit`, {
      method: "POST", body: JSON.stringify({ taskId: task.task_id })
    });
    if ($("taskReviewDialog").open) $("taskReviewDialog").close();
    await loadTasks();
    showToast("已创建重新拆分任务");
  } catch (error) {
    showToast(error.message, true);
  }
}

$("refreshTaskReviews").addEventListener("click", () => loadTasks().catch((error) => showToast(error.message, true)));
[$("taskChannelFilter"), $("taskISliceFilter"), $("taskDateFilter"), $("taskReviewStatusFilter"), $("taskContentTypeFilter")].forEach((control) => control.addEventListener("change", () => {
  state.page = 1; loadTasks().catch((error) => showToast(error.message, true));
}));
$("taskReviewQuery").addEventListener("input", () => {
  window.clearTimeout(state.queryTimer);
  state.queryTimer = window.setTimeout(() => {
    state.page = 1; loadTasks().catch((error) => showToast(error.message, true));
  }, 300);
});
$("taskReviewPageSize").addEventListener("change", () => {
  state.pageSize = Number($("taskReviewPageSize").value); state.page = 1;
  loadTasks().catch((error) => showToast(error.message, true));
});
$("previousTaskReviewPage").addEventListener("click", () => {
  if (state.page > 1) state.page -= 1;
  loadTasks().catch((error) => showToast(error.message, true));
});
$("nextTaskReviewPage").addEventListener("click", () => {
  if (state.page < state.totalPages) state.page += 1;
  loadTasks().catch((error) => showToast(error.message, true));
});
$("taskReviewBody").addEventListener("change", (event) => {
  const select = event.target.closest(".task-review-status");
  if (select) saveReviewStatus(select);
});
$("taskReviewBody").addEventListener("click", (event) => {
  if (event.target.closest("select,a")) return;
  const resplit = event.target.closest(".resplit-task");
  if (resplit) { resplitTask(Number(resplit.dataset.attemptId)); return; }
  const trigger = event.target.closest(".open-task-review") || event.target.closest(".task-review-row");
  if (trigger) openTaskReview(Number(trigger.dataset.attemptId)).catch((error) => showToast(error.message, true));
});
$("taskReviewSegmentsBody").addEventListener("click", (event) => {
  const edit = event.target.closest(".task-review-segment-edit");
  if (edit) { openTaskSegmentEdit(Number(edit.dataset.segmentIndex)); return; }
  const row = event.target.closest(".task-review-segment-row.is-previewable");
  if (row) previewSegment(Number(row.dataset.segmentIndex));
});
$("taskReviewSegmentsBody").addEventListener("keydown", (event) => {
  if (event.target.closest("button")) return;
  const row = event.target.closest(".task-review-segment-row.is-previewable");
  if (row && ["Enter", " "].includes(event.key)) { event.preventDefault(); previewSegment(Number(row.dataset.segmentIndex)); }
});
$("taskReviewSeek").addEventListener("input", () => {
  const target = Number($("taskReviewSeek").value);
  state.isSeeking = true;
  state.pendingSeek = target;
  $("taskReviewSeekCurrent").textContent = formatMediaTime(target);
  applyPendingSeek();
});
$("taskReviewSeek").addEventListener("change", () => {
  state.pendingSeek = Number($("taskReviewSeek").value);
  applyPendingSeek();
  state.isSeeking = false;
});
$("taskReviewVideo").addEventListener("loadedmetadata", () => {
  const video = $("taskReviewVideo");
  const expected = Number($("taskReviewSeek").max);
  configureSeek(Number.isFinite(video.duration) ? video.duration : expected, video.currentTime);
  applyPendingSeek();
});
$("taskReviewVideo").addEventListener("durationchange", () => {
  const video = $("taskReviewVideo");
  if (Number.isFinite(video.duration) && video.duration > 0) {
    configureSeek(video.duration, video.currentTime);
  }
});
$("taskReviewVideo").addEventListener("timeupdate", () => {
  if (state.isSeeking) return;
  const video = $("taskReviewVideo");
  const seek = $("taskReviewSeek");
  seek.value = String(Math.min(video.currentTime, Number(seek.max) || video.currentTime));
  $("taskReviewSeekCurrent").textContent = formatMediaTime(video.currentTime);
});
$("taskReviewVideo").addEventListener("canplay", () => {
  $("taskReviewVideoStatus").textContent = "视频已就绪";
});
$("taskReviewVideo").addEventListener("seeking", () => {
  $("taskReviewVideoStatus").textContent = "正在定位";
});
$("taskReviewVideo").addEventListener("seeked", () => {
  state.isSeeking = false;
  $("taskReviewVideoStatus").textContent = `已定位到 ${formatMediaTime($("taskReviewVideo").currentTime)}`;
});
$("taskReviewVideo").addEventListener("error", () => {
  $("taskReviewVideoStatus").textContent = "视频加载失败";
});
$("taskAIReviewForm").addEventListener("submit", saveAIReview);
$("taskSegmentEditForm").addEventListener("submit", submitTaskSegmentEdit);
$("restoreTaskSegmentEdit").addEventListener("click", restoreTaskSegmentEdit);
[$("closeTaskSegmentEdit"), $("cancelTaskSegmentEdit")].forEach((button) => button.addEventListener("click", () => $("taskSegmentEditDialog").close()));
$("taskSegmentEditDialog").addEventListener("close", () => { state.segmentEditTarget = null; });
$("resplitReviewedTask").addEventListener("click", () => state.selectedTask && resplitTask(state.selectedTask.id));
[$("closeTaskReviewDialog"), $("doneTaskReviewDialog")].forEach((button) => button.addEventListener("click", () => $("taskReviewDialog").close()));
$("taskReviewDialog").addEventListener("close", resetVideo);

loadFilters().then(loadTasks).catch((error) => showToast(error.message, true));
