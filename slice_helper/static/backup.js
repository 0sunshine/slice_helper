const state = { instances: [], channels: [], page: 1, totalPages: 1 };

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
  $("instanceCards").innerHTML = state.instances.map((item) => `
    <article class="instance-card">
      <div class="instance-card-head">
        <div><strong>${escapeHtml(item.name)}</strong><span class="mono">${escapeHtml(item.source_id)}</span></div>
        <span class="status-pill ${item.schedulable ? "ok" : "neutral"}">${item.schedulable ? "参与调度" : "停止调度"}</span>
      </div>
      <dl>
        <div><dt>API</dt><dd class="mono">${escapeHtml(item.base_url)}</dd></div>
        <div><dt>Catalog</dt><dd class="mono">${escapeHtml(item.archive_catalog_url || "未配置")}</dd></div>
        <div><dt>作业</dt><dd>${item.job_count} 个，活动 ${item.active_job_count} 个</dd></div>
      </dl>
      <div class="instance-actions">
        <button class="button secondary small edit-instance" data-id="${item.id}" type="button">编辑</button>
        <button class="button danger small delete-instance" data-id="${item.id}" type="button" ${item.job_count ? "disabled" : ""}>删除</button>
      </div>
    </article>`).join("");
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
  $("backupCount").textContent = payload.total;
  $("emptyBackups").hidden = payload.items.length > 0;
  $("backupBody").innerHTML = payload.items.map((item) => {
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
    </tr>`;
  }).join("");
  $("backupPageSummary").textContent = `共 ${payload.total} 条`;
  $("backupPageInfo").textContent = `${payload.page} / ${payload.totalPages}`;
  $("previousBackupPage").disabled = payload.page <= 1;
  $("nextBackupPage").disabled = payload.page >= payload.totalPages;
  renderSources(payload.sources);
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

function openInstance(item = null) {
  $("instanceDialogTitle").textContent = item ? "编辑 iSlice 实例" : "添加 iSlice 实例";
  $("instanceId").value = item?.id || "";
  $("instanceName").value = item?.name || "";
  $("instanceSourceId").value = item?.source_id || "";
  $("instanceBaseUrl").value = item?.base_url || "";
  $("instanceCatalogUrl").value = item?.archive_catalog_url || "";
  $("instanceSchedulable").checked = item?.schedulable ?? true;
  $("instanceDialog").showModal();
}

$("instanceForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const id = $("instanceId").value;
  const body = {
    name: $("instanceName").value,
    sourceId: $("instanceSourceId").value,
    baseUrl: $("instanceBaseUrl").value,
    archiveCatalogUrl: $("instanceCatalogUrl").value,
    schedulable: $("instanceSchedulable").checked,
  };
  try {
    await request(id ? `/api/islice-instances/${id}` : "/api/islice-instances", {
      method: id ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    $("instanceDialog").close(); await loadInstances(); await loadBackups(); toast("实例配置已保存");
  } catch (error) { toast(error.message, true); }
});

$("instanceCards").addEventListener("click", async (event) => {
  const edit = event.target.closest(".edit-instance");
  if (edit) return openInstance(state.instances.find((item) => item.id === edit.dataset.id));
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

Promise.all([loadInstances(), loadChannels()])
  .then(loadBackups)
  .catch((error) => toast(error.message, true));
