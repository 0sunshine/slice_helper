const state = {
  instances: [], channels: [], page: 1, totalPages: 1,
  archiveItems: [], archivePreviewItem: null, archivePreviewSegments: [],
  resetPreview: null, resetExecuting: false,
};

const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#39;");

const labels = {
  pending: "等待归档", syncing: "同步中", verifying: "校验中",
  delete_pending: "等待删除", archived_hold: "归档保留",
  archived_unpublished: "已归档未发布", publish_conflict: "发布冲突",
  deleted: "本地已清理", failed: "失败",
};

function statusClass(value) {
  if (["deleted", "delete_pending"].includes(value)) return "ok";
  if (["archived_hold", "archived_unpublished", "pending", "syncing", "verifying"].includes(value)) return "warn";
  return "bad";
}

function agentStatus(item) {
  const labels = { online: "代理在线", offline: "代理离线", deploying: "部署中", error: "部署异常", unconfigured: "未部署" };
  const css = item.agent_status === "online" ? "ok" : item.agent_status === "deploying" ? "warn" : ["offline", "error"].includes(item.agent_status) ? "bad" : "neutral";
  return { label: labels[item.agent_status] || item.agent_status || "未部署", css };
}

function bytes(value) {
  let size = Number(value || 0);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size.toFixed(index < 2 ? 0 : 2)} ${units[index]}`;
}

function dateTime(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}

function warnings(value) {
  try {
    const parsed = JSON.parse(value || "[]");
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch (_) {
    return value ? [String(value)] : [];
  }
}

function toast(message, bad = false) {
  const node = $("toast");
  node.textContent = message;
  node.classList.toggle("error", bad);
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 3500);
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try { const payload = await response.json(); message = payload.detail || message; } catch (_) {}
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }
  return response.status === 204 ? null : response.json();
}

function renderInstances() {
  $("instanceCount").textContent = state.instances.length;
  $("emptyInstances").hidden = state.instances.length > 0;
  $("instanceCards").innerHTML = state.instances.map((item) => {
    const agent = agentStatus(item);
    return `
    <article class="instance-card">
      <div class="instance-card-head">
        <div><strong>${escapeHtml(item.name)}</strong><span class="mono">${escapeHtml(item.source_id)}</span></div>
        <div class="instance-statuses"><span class="status-pill ${agent.css}">${escapeHtml(agent.label)}</span><span class="status-pill ${item.schedulable ? "ok" : "neutral"}">${item.schedulable ? "参与调度" : "停止调度"}</span></div>
      </div>
      <dl>
        <div><dt>API</dt><dd class="mono">${escapeHtml(item.base_url)}</dd></div>
        <div><dt>Catalog</dt><dd class="mono">${escapeHtml(item.archive_catalog_url || "未配置")}</dd></div>
        <div><dt>SSH</dt><dd class="mono">${escapeHtml(item.ssh_host ? `${item.ssh_username}@${item.ssh_host}:${item.ssh_port}` : "未配置")}</dd></div>
        <div><dt>主机密钥</dt><dd class="mono">${escapeHtml(item.ssh_host_key_sha256 ? `SHA256:${item.ssh_host_key_sha256.slice(0, 18)}…` : "首次连接时记录")}</dd></div>
        <div><dt>代理</dt><dd>${escapeHtml(item.agent_version || "—")}<small class="table-subtext">检查：${dateTime(item.agent_last_checked_at)}</small>${item.agent_last_error ? `<small class="table-subtext error-text" title="${escapeHtml(item.agent_last_error)}">${escapeHtml(item.agent_last_error)}</small>` : ""}</dd></div>
        <div><dt>作业</dt><dd>${item.job_count} 个，活动 ${item.active_job_count} 个</dd></div>
      </dl>
      <div class="instance-actions">
        <button class="button secondary small edit-instance" data-id="${item.id}" type="button">编辑</button>
        <button class="button secondary small check-agent" data-id="${item.id}" type="button" ${item.ssh_host ? "" : "disabled"}>检查</button>
        <button class="button primary small deploy-agent" data-id="${item.id}" type="button" ${item.ssh_host ? "" : "disabled"}>部署/拉起</button>
        <button class="button danger small delete-instance" data-id="${item.id}" type="button" ${item.job_count ? "disabled" : ""}>删除</button>
      </div>
    </article>`;
  }).join("");
  $("backupSource").innerHTML = '<option value="">全部来源</option>' + state.instances
    .map((item) => `<option value="${escapeHtml(item.source_id)}">${escapeHtml(item.name)}</option>`).join("");
}

async function loadInstances() {
  state.instances = await request("/api/islice-instances");
  renderInstances();
}

function renderChannels() {
  const selected = $("backupChannel").value;
  $("backupChannel").innerHTML = '<option value="">全部频道</option>' + state.channels
    .map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join("");
  if (state.channels.some((item) => item.id === selected)) $("backupChannel").value = selected;
}

async function loadChannels() {
  state.channels = await request("/api/channels");
  renderChannels();
}

function renderSources(sources) {
  const online = sources.filter((item) => item.online).length;
  const badge = $("catalogHealth");
  badge.className = `health ${online === sources.length && sources.length ? "ok" : "bad"}`;
  badge.textContent = `${online}/${sources.length} 归档源在线`;
  $("archiveSummary").innerHTML = sources.map((source) => {
    const summary = source.summary || {};
    const states = summary.states || {};
    return `<article class="archive-source-summary ${source.online ? "" : "offline"}">
      <strong>${escapeHtml(source.name)}</strong>
      <span>${source.online ? `任务 ${summary.taskCount || 0} · ${bytes(summary.totalBytes)}` : escapeHtml(source.error || "无法读取")}</span>
      <small>${source.online ? `已清理 ${states.deleted || 0} · 等待删除 ${states.delete_pending || 0} · 失败 ${states.failed || 0}` : escapeHtml(source.catalogUrl || "未配置 Catalog")}</small>
      <small>更新时间：${dateTime(source.generatedAt)}</small>
    </article>`;
  }).join("");
}

function renderTasks(payload) {
  state.page = payload.page;
  state.totalPages = payload.totalPages;
  state.archiveItems = payload.items;
  $("backupCount").textContent = payload.total;
  $("emptyBackups").hidden = payload.items.length > 0;
  $("backupBody").innerHTML = payload.items.map((item, itemIndex) => {
    const context = item.context || {};
    const revisions = item.revisions || [];
    const detail = item.error_message || warnings(item.warnings_json).join("；") || "—";
    return `<tr>
      <td><strong>${escapeHtml(item.source_name)}</strong><small class="table-subtext mono">${escapeHtml(item.source_id)}</small></td>
      <td>${escapeHtml(context.channel_name || "未关联管理作业")}<small class="table-subtext">${escapeHtml(context.broadcast_date || context.job_id || "")}</small></td>
      <td>${context.window_index === undefined ? "—" : Number(context.window_index) + 1}<small class="table-subtext">${escapeHtml(context.attempt_status || "")}</small></td>
      <td class="mono task-id-cell">${escapeHtml(item.task_id)}</td>
      <td><span class="status-pill ${statusClass(item.state)}">${escapeHtml(labels[item.state] || item.state)}</span></td>
      <td>${item.revision_count || revisions.length || 0}<small class="table-subtext mono">${escapeHtml((item.published_digest || item.manifest_digest || "").slice(0, 12))}</small></td>
      <td>${item.file_count || 0}</td><td>${bytes(item.total_bytes)}</td>
      <td>${dateTime(item.archived_at)}<small class="table-subtext">删除：${dateTime(item.deleted_at || item.delete_after)}</small></td>
      <td class="backup-detail-cell" title="${escapeHtml(detail)}">${escapeHtml(detail)}</td>
      <td><button class="button secondary small preview-archive" data-index="${itemIndex}" type="button">预览</button></td>
    </tr>`;
  }).join("");
  $("backupPageSummary").textContent = `共 ${payload.total} 条`;
  $("backupPageInfo").textContent = `${payload.page} / ${payload.totalPages}`;
  $("previousBackupPage").disabled = payload.page <= 1;
  $("nextBackupPage").disabled = payload.page >= payload.totalPages;
  renderSources(payload.sources);
}

function archiveTime(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return "—";
  const whole = Math.max(0, Math.round(seconds));
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const remain = whole % 60;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(remain).padStart(2, "0")}`;
}

function resetArchivePlayer(message = "正在读取归档结果…") {
  const video = $("archivePreviewVideo");
  video.pause();
  video.removeAttribute("src");
  video.removeAttribute("poster");
  video.load();
  $("archiveSegmentTitle").textContent = "请选择右侧片段";
  $("archiveSegmentTime").textContent = "";
  $("archivePreviewStatus").textContent = message;
  $("openArchiveVideo").hidden = true;
  $("openArchiveVideo").removeAttribute("href");
}

function selectArchiveSegment(index, autoplay = false) {
  const segment = state.archivePreviewSegments[index];
  if (!segment) return;
  const video = $("archivePreviewVideo");
  document.querySelectorAll(".archive-segment-item").forEach((item) => {
    item.classList.toggle("is-active", Number(item.dataset.index) === index);
  });
  $("archiveSegmentTitle").textContent = segment.title || segment.topic || `片段 ${index + 1}`;
  $("archiveSegmentTime").textContent = `${archiveTime(segment.startTime)} — ${archiveTime(segment.endTime)}`;
  if (!segment.segmentUrl) {
    resetArchivePlayer("该片段没有可用的归档视频文件");
    $("archiveSegmentTitle").textContent = segment.title || segment.topic || `片段 ${index + 1}`;
    $("archiveSegmentTime").textContent = `${archiveTime(segment.startTime)} — ${archiveTime(segment.endTime)}`;
    return;
  }
  video.src = segment.segmentUrl;
  if (segment.coverImgUrl) video.poster = segment.coverImgUrl;
  else video.removeAttribute("poster");
  video.load();
  $("archivePreviewStatus").textContent = "视频来自远端归档存储";
  $("openArchiveVideo").href = segment.segmentUrl;
  $("openArchiveVideo").hidden = false;
  if (autoplay) video.play().catch(() => {});
}

function renderArchiveSegments(payload) {
  state.archivePreviewSegments = payload.segments || [];
  $("archiveSegmentList").innerHTML = state.archivePreviewSegments.length
    ? state.archivePreviewSegments.map((segment, index) => `
      <button class="archive-segment-item ${segment.segmentUrl ? "" : "is-unavailable"}" data-index="${index}" type="button">
        <span>${index + 1}</span>
        <strong>${escapeHtml(segment.title || segment.topic || `片段 ${index + 1}`)}</strong>
        <small class="mono">${archiveTime(segment.startTime)} — ${archiveTime(segment.endTime)}</small>
      </button>`).join("")
    : '<div class="empty-state">归档结果中没有片段</div>';
  const warnings = payload.warnings || [];
  $("archivePreviewWarnings").hidden = warnings.length === 0;
  $("archivePreviewWarnings").innerHTML = warnings.map((warning) => `<p>${escapeHtml(warning)}</p>`).join("");
  if (state.archivePreviewSegments.length) selectArchiveSegment(0, false);
  else resetArchivePlayer("归档结果中没有可预览片段");
}

async function loadArchivePreview(revisionDigest = "") {
  const item = state.archivePreviewItem;
  if (!item) return;
  resetArchivePlayer();
  $("archiveSegmentList").innerHTML = '<div class="empty-state">正在加载归档片段…</div>';
  $("archivePreviewWarnings").hidden = true;
  const params = new URLSearchParams();
  if (revisionDigest) params.set("revisionDigest", revisionDigest);
  const query = params.toString();
  const suffix = query ? `?${query}` : "";
  try {
    const payload = await request(`/api/archive/tasks/${encodeURIComponent(item.source_id)}/${encodeURIComponent(item.task_id)}/preview${suffix}`);
    renderArchiveSegments(payload);
  } catch (error) {
    state.archivePreviewSegments = [];
    resetArchivePlayer(error.message);
    $("archiveSegmentList").innerHTML = `<div class="empty-state error-text">${escapeHtml(error.message)}</div>`;
  }
}

function openArchivePreview(item) {
  state.archivePreviewItem = item;
  state.archivePreviewSegments = [];
  $("archivePreviewTitle").textContent = item.context?.channel_name || item.task_id;
  $("archivePreviewSource").textContent = `${item.source_name} · ${item.task_id}`;
  const revisions = (item.revisions || []).filter((revision) => revision.manifest_digest);
  $("archiveRevision").innerHTML = '<option value="">当前归档版本</option>' + revisions
    .map((revision) => `<option value="${escapeHtml(revision.manifest_digest)}">${escapeHtml(revision.state || "历史版本")} · ${escapeHtml(revision.manifest_digest.slice(0, 12))}</option>`)
    .join("");
  $("archiveRevisionField").hidden = revisions.length < 2;
  $("archivePreviewDialog").showModal();
  loadArchivePreview();
}

function closeArchivePreview() {
  resetArchivePlayer("");
  state.archivePreviewItem = null;
  state.archivePreviewSegments = [];
  $("archivePreviewDialog").close();
}

async function loadBackups() {
  const params = new URLSearchParams({ page: state.page, pageSize: 20 });
  if ($("backupSource").value) params.set("sourceId", $("backupSource").value);
  if ($("backupChannel").value) params.set("channelId", $("backupChannel").value);
  if ($("backupDate").value) params.set("broadcastDate", $("backupDate").value);
  if ($("backupState").value) params.set("state", $("backupState").value);
  if ($("backupQuery").value.trim()) params.set("query", $("backupQuery").value.trim());
  renderTasks(await request(`/api/archive/status?${params}`));
}

const resetCountLabels = {
  channels: "频道", jobs: "作业", windows: "窗口", attempts: "iSlice 尝试",
  segments: "拆条片段", segment_merges: "人工合并", segment_merge_members: "合并成员",
  job_rebuilds: "重建记录",
};

function resetCommandCard(item) {
  const command = item.command || item.prepareCommand || "";
  return `<article class="reset-command-card">
    <div><strong>${escapeHtml(item.sourceId)}</strong><button class="button secondary small copy-reset-command" type="button">复制</button></div>
    <code>${escapeHtml(command || "未生成命令，请重新生成重置请求")}</code>
  </article>`;
}

function renderResetPreview(preview) {
  state.resetPreview = preview;
  $("resetExpiry").textContent = `请求有效期至 ${dateTime(preview.expiresAt)}`;
  $("resetCounts").innerHTML = Object.entries(preview.counts || {})
    .map(([key, value]) => `<div><span>${escapeHtml(resetCountLabels[key] || key)}</span><strong>${Number(value || 0)}</strong></div>`)
    .join("");
  $("resetPrepareCommands").innerHTML = preview.sources.map(resetCommandCard).join("");
  $("resetConfirmationHint").textContent = preview.confirmationText;
  $("resetReceipts").value = "";
  $("resetConfirmation").value = "";
  $("resetMediaAck").checked = false;
  $("resetError").hidden = true;
  $("resetPreparePhase").hidden = false;
  $("resetResultPhase").hidden = true;
  $("executeSystemReset").hidden = false;
  $("finishSystemReset").hidden = true;
  $("cancelSystemReset").textContent = "取消";
}

function parseResetReceipts(value) {
  const text = value.trim();
  if (!text) throw new Error("请粘贴所有 iSlice 主机的备份回执");
  try {
    const parsed = JSON.parse(text);
    const receipts = Array.isArray(parsed) ? parsed : [parsed];
    if (!receipts.length || receipts.some((item) => !item || typeof item !== "object" || Array.isArray(item))) {
      throw new Error("回执必须是 JSON 对象");
    }
    return receipts;
  } catch (wholeError) {
    const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    if (lines.length < 2) throw wholeError;
    return lines.map((line, index) => {
      try {
        const parsed = JSON.parse(line);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
        return parsed;
      } catch (_) {
        throw new Error(`第 ${index + 1} 行不是有效的 JSON 回执`);
      }
    });
  }
}

async function startSystemReset() {
  const button = $("startSystemReset");
  button.disabled = true;
  try {
    const preview = await request("/api/system-reset/preview", { method: "POST" });
    renderResetPreview(preview);
    $("resetDialog").showModal();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function executeSystemReset() {
  const errorNode = $("resetError");
  errorNode.hidden = true;
  try {
    if (!state.resetPreview) throw new Error("重置请求不存在，请重新生成");
    if (!$("resetMediaAck").checked) throw new Error("必须确认媒体目录由用户自行处理");
    if ($("resetConfirmation").value !== state.resetPreview.confirmationText) {
      throw new Error("二次确认短语不正确");
    }
    const receipts = parseResetReceipts($("resetReceipts").value);
    state.resetExecuting = true;
    $("executeSystemReset").disabled = true;
    $("cancelSystemReset").disabled = true;
    const result = await request("/api/system-reset/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        requestId: state.resetPreview.requestId,
        confirmationText: $("resetConfirmation").value,
        receipts,
        acknowledgeMediaHandling: true,
      }),
    });
    $("resetPreparePhase").hidden = true;
    $("resetResultPhase").hidden = false;
    $("resetHelperBackup").textContent = result.helperBackup.databaseBackup;
    $("resetHelperSha").textContent = result.helperBackup.sha256;
    $("resetCommitCommands").innerHTML = result.commitCommands.map(resetCommandCard).join("");
    $("executeSystemReset").hidden = true;
    $("finishSystemReset").hidden = false;
    $("cancelSystemReset").hidden = true;
    await Promise.all([loadInstances(), loadChannels()]);
    state.page = 1;
    await loadBackups();
    toast("helper 已完成备份和状态重置；尚未执行各 iSlice 的 commit-reset");
  } catch (error) {
    errorNode.textContent = error.message;
    errorNode.hidden = false;
  } finally {
    state.resetExecuting = false;
    $("executeSystemReset").disabled = false;
    $("cancelSystemReset").disabled = false;
  }
}

function openInstance(item = null) {
  $("instanceDialogTitle").textContent = item ? "编辑服务" : "添加服务";
  $("instanceId").value = item?.id || "";
  $("instanceName").value = item?.name || "";
  $("instanceSourceId").value = item?.source_id || "";
  $("instanceBaseUrl").value = item?.base_url || "";
  $("instanceCatalogUrl").value = item?.archive_catalog_url || "";
  $("instanceSshHost").value = item?.ssh_host || "";
  $("instanceSshPort").value = item?.ssh_port || 22;
  $("instanceSshUsername").value = item?.ssh_username || "root";
  $("instanceSshPassword").value = "";
  $("instanceAgentInstallPath").value = item?.agent_install_path || "";
  $("instanceDatabasePath").value = item?.islice_database_path || "/mnt/c/WorkSpace/PublishPackage/iSlice/data/tasks.db";
  $("instanceStorageRoot").value = item?.storage_root || "/mnt/c/WorkSpace/PublishPackage/iSlice/storage";
  $("instanceArchiveRemoteHost").value = item?.archive_remote_host || "192.168.6.200";
  $("instanceArchiveRemoteUser").value = item?.archive_remote_user || "codex";
  $("instanceArchiveRemoteRoot").value = item?.archive_remote_root || (item?.source_id ? `/mpeg/mpeg2/codex/archive/sources/${item.source_id}` : "");
  $("instanceArchiveHttpBase").value = item?.archive_http_base || (item?.archive_catalog_url || "").replace(/\/catalog\.json$/, "");
  $("instanceArchiveSshKey").value = item?.archive_ssh_key || "/root/.ssh/islice_archiver_ed25519";
  $("instanceArchiveKnownHosts").value = item?.archive_known_hosts || "/root/.ssh/known_hosts";
  $("instanceSchedulable").checked = item?.schedulable ?? true;
  $("deployAfterSave").checked = Boolean(item?.ssh_host || !item);
  $("instanceDialog").showModal();
}

function updateArchiveNamespace() {
  if ($("instanceId").value) return;
  const sourceId = $("instanceSourceId").value.trim().toLowerCase();
  if (!sourceId) return;
  $("instanceSourceId").value = sourceId;
  $("instanceArchiveRemoteRoot").value = `/mpeg/mpeg2/codex/archive/sources/${sourceId}`;
  $("instanceArchiveHttpBase").value = `http://192.168.6.200:18080/sources/${sourceId}`;
  $("instanceCatalogUrl").value = `http://192.168.6.200:18080/sources/${sourceId}/catalog.json`;
}

$("instanceSourceId").addEventListener("change", updateArchiveNamespace);

$("instanceForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const id = $("instanceId").value;
  const body = {
    name: $("instanceName").value,
    sourceId: $("instanceSourceId").value,
    baseUrl: $("instanceBaseUrl").value,
    archiveCatalogUrl: $("instanceCatalogUrl").value,
    schedulable: $("instanceSchedulable").checked,
    sshHost: $("instanceSshHost").value,
    sshPort: Number($("instanceSshPort").value),
    sshUsername: $("instanceSshUsername").value,
    sshPassword: $("instanceSshPassword").value || null,
    agentInstallPath: $("instanceAgentInstallPath").value,
    isliceDatabasePath: $("instanceDatabasePath").value,
    storageRoot: $("instanceStorageRoot").value,
    archiveRemoteHost: $("instanceArchiveRemoteHost").value,
    archiveRemoteUser: $("instanceArchiveRemoteUser").value,
    archiveRemoteRoot: $("instanceArchiveRemoteRoot").value,
    archiveHttpBase: $("instanceArchiveHttpBase").value,
    archiveSshKey: $("instanceArchiveSshKey").value,
    archiveKnownHosts: $("instanceArchiveKnownHosts").value,
  };
  try {
    const saved = await request(id ? `/api/islice-instances/${id}` : "/api/islice-instances", {
      method: id ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    $("instanceDialog").close();
    if ($("deployAfterSave").checked) {
      try {
        await request(`/api/islice-instances/${saved.id}/deploy-agent`, { method: "POST" });
        toast("服务配置已保存，归档代理已部署并拉起");
      } catch (deployError) {
        toast(`服务已保存，但代理部署失败：${deployError.message}`, true);
      }
    } else toast("服务配置已保存");
    await loadInstances(); await loadBackups();
  } catch (error) { toast(error.message, true); }
});

$("instanceCards").addEventListener("click", async (event) => {
  const edit = event.target.closest(".edit-instance");
  if (edit) return openInstance(state.instances.find((item) => item.id === edit.dataset.id));
  const check = event.target.closest(".check-agent");
  if (check) {
    check.disabled = true;
    try { await request(`/api/islice-instances/${check.dataset.id}/check-agent`, { method: "POST" }); await loadInstances(); toast("代理状态已更新"); }
    catch (error) { toast(error.message, true); }
    finally { check.disabled = false; }
    return;
  }
  const deploy = event.target.closest(".deploy-agent");
  if (deploy) {
    deploy.disabled = true;
    try { await request(`/api/islice-instances/${deploy.dataset.id}/deploy-agent`, { method: "POST" }); await loadInstances(); toast("归档代理已部署并拉起"); }
    catch (error) { await loadInstances(); toast(error.message, true); }
    finally { deploy.disabled = false; }
    return;
  }
  const remove = event.target.closest(".delete-instance");
  if (!remove || !confirm("确定删除这个未使用的 iSlice 实例吗？")) return;
  try { await request(`/api/islice-instances/${remove.dataset.id}`, { method: "DELETE" }); await loadInstances(); await loadBackups(); }
  catch (error) { toast(error.message, true); }
});

$("addInstance").addEventListener("click", () => openInstance());
$("closeInstanceDialog").addEventListener("click", () => $("instanceDialog").close());
$("cancelInstance").addEventListener("click", () => $("instanceDialog").close());
$("refreshBackup").addEventListener("click", async () => { await loadInstances(); await loadBackups(); });
$("backupSource").addEventListener("change", () => { state.page = 1; loadBackups(); });
$("backupChannel").addEventListener("change", () => { state.page = 1; loadBackups(); });
$("backupDate").addEventListener("change", () => { state.page = 1; loadBackups(); });
$("backupState").addEventListener("change", () => { state.page = 1; loadBackups(); });
$("backupQuery").addEventListener("input", () => { clearTimeout(state.queryTimer); state.queryTimer = setTimeout(() => { state.page = 1; loadBackups(); }, 300); });
$("previousBackupPage").addEventListener("click", () => { state.page -= 1; loadBackups(); });
$("nextBackupPage").addEventListener("click", () => { state.page += 1; loadBackups(); });
$("backupBody").addEventListener("click", (event) => {
  const button = event.target.closest(".preview-archive");
  if (button) openArchivePreview(state.archiveItems[Number(button.dataset.index)]);
});
$("archiveRevision").addEventListener("change", () => loadArchivePreview($("archiveRevision").value));
$("archiveSegmentList").addEventListener("click", (event) => {
  const item = event.target.closest(".archive-segment-item");
  if (item) selectArchiveSegment(Number(item.dataset.index), true);
});
$("closeArchivePreview").addEventListener("click", closeArchivePreview);
$("finishArchivePreview").addEventListener("click", closeArchivePreview);
$("archivePreviewDialog").addEventListener("cancel", (event) => { event.preventDefault(); closeArchivePreview(); });
$("startSystemReset").addEventListener("click", startSystemReset);
$("executeSystemReset").addEventListener("click", executeSystemReset);
$("closeResetDialog").addEventListener("click", () => { if (!state.resetExecuting) $("resetDialog").close(); });
$("cancelSystemReset").addEventListener("click", () => $("resetDialog").close());
$("finishSystemReset").addEventListener("click", () => $("resetDialog").close());
$("resetDialog").addEventListener("cancel", (event) => { if (state.resetExecuting) event.preventDefault(); });
$("resetDialog").addEventListener("click", async (event) => {
  const button = event.target.closest(".copy-reset-command");
  if (!button) return;
  const command = button.closest(".reset-command-card").querySelector("code").textContent;
  try { await navigator.clipboard.writeText(command); toast("命令已复制"); }
  catch (_) { toast("浏览器未允许访问剪贴板，请手工复制", true); }
});

Promise.all([loadInstances(), loadChannels()])
  .then(loadBackups)
  .catch((error) => toast(error.message, true));
setInterval(() => loadInstances().catch(() => {}), 15000);
