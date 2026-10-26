const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
let state = null;
let selectedId = "";
let weekStart = "";
let currentTab = "overview";
let requestSequence = 0;
let toastTimer;
let writing = false;
let settingsDirty = false;

function dateTime(value, options = {}) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: state?.selected?.timezone || "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    ...options,
  }).format(new Date(value));
}
function clock(value) {
  return dateTime(value, { month: undefined, day: undefined });
}
function shiftDate(value, delta) {
  const date = new Date(`${value}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + delta);
  return date.toISOString().slice(0, 10);
}
function showToast(message, error = false) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").className = `toast${error ? " error" : ""}`;
  $("toast").hidden = false;
  toastTimer = setTimeout(
    () => {
      $("toast").hidden = true;
    },
    error ? 6500 : 4000,
  );
}
function showError(error) {
  showToast(error.message || String(error), true);
}
function warning(message) {
  $("banner").textContent = message;
  $("banner").hidden = !message;
}
function activate(tab) {
  currentTab = tab;
  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tab);
    if (button.dataset.tab === tab) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.hidden = panel.id !== `tab-${tab}`;
  });
}
function table(headers, rows, empty) {
  if (!rows.length) return `<div class="empty-inline">${escape(empty)}</div>`;
  return `<div class="table-scroll"><table class="data-table"><thead><tr>${headers.map((h) => `<th scope="col">${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}
function render() {
  const selected = state.selected;
  const scheduler = state.scheduler;
  const future = (state.scheduler.mode || state.settings.scheduler_mode) === "future_tasks";
  $("connection").textContent = scheduler.error ? "需要处理" : "已连接 AstrBot";
  $("connection").className = `chip${scheduler.error ? " warn" : ""}`;
  $("engine-label").textContent = future ? "AstrBot 未来任务" : "插件轮询";
  $("sync-label").textContent = scheduler.last_sync
    ? `最近同步 ${clock(scheduler.last_sync)}`
    : "等待首次同步";
  const issues = [scheduler.error];
  if (scheduler.truncated)
    issues.push(
      "部分任务超过排期上限（每张课表 200、总计 1000）；后续任务将自动续排，请检查是否导入了重复课程。",
    );
  if (!state.settings.enabled)
    issues.push("自动提醒已全局暂停，课表仍可查看。可在插件设置中开启。");
  warning(issues.filter(Boolean).join(" "));
  $("binding-select").innerHTML = state.bindings.length
    ? state.bindings
        .map(
          (b) =>
            `<option value="${escape(b.id)}"${selected?.id === b.id ? " selected" : ""}>${escape(b.name)} · ${escape(b.nickname)}${b.group ? " · 群聊" : ""}${b.active ? "" : " · 已暂停"}</option>`,
        )
        .join("")
    : '<option value="">暂无课表</option>';
  $("binding-tools").hidden = !selected;
  $("empty").hidden = !!selected;
  $("overview-content").hidden = !selected;
  $("plan-count").textContent = future
    ? String(scheduler.jobs.filter((j) => j.binding_id === selected?.id).length)
    : String(selected?.upcoming.length || 0);
  $("version").textContent = `v${state.version}`;
  if (selected) renderOverview(selected);
  renderPlans(selected);
  renderRecords();
  if (!settingsDirty) fillSettings();
  activate(currentTab);
  $("loading").hidden = true;
  $("app").hidden = false;
}
function renderOverview(b) {
  const next = b.upcoming[0];
  const total = b.days.reduce((sum, d) => sum + d.courses.length, 0);
  const source =
    b.kind === "calendar" && !b.custom_lead
      ? "沿用日历"
      : `提前 ${b.lead_minutes.join(" / ")} 分钟`;
  $("stats").innerHTML = [
    [
      "这一周的课程",
      `${total}<em>课次</em>`,
      `${b.start.slice(5)} — ${shiftDate(b.start, 6).slice(5)}`,
    ],
    [
      "下一次提醒",
      next ? clock(next.fire) : "—",
      next
        ? `${dateTime(next.fire, { hour: undefined, minute: undefined })} · ${next.name}`
        : "当前课表没有未来课程",
    ],
    [
      "提醒方式",
      source,
      b.kind === "calendar" && !b.custom_lead
        ? "日历未设置闹钟时使用默认提前量"
        : "可在提醒设置中调整",
    ],
    [
      "当前状态",
      b.active ? "提醒已开启" : "提醒已暂停",
      b.group ? `群聊 · @ ${b.sender}` : `私聊 · ${b.nickname || b.sender}`,
    ],
  ]
    .map(
      ([label, value, sub], i) =>
        `<div class="stat panel"><div class="stat-label">${label}</div><div class="stat-value${i > 1 ? " small" : ""}">${value}</div><div class="stat-sub" title="${escape(sub)}">${escape(sub)}</div></div>`,
    )
    .join("");
  $("toggle-binding").textContent = b.enabled ? "暂停提醒" : "开启提醒";
  $("week-label").textContent =
    `${b.start} — ${shiftDate(b.start, 6)} · ${b.timezone}`;
  $("calendar").innerHTML = b.days
    .map(
      (d, i) =>
        `<div class="day${d.today ? " today" : ""}"><div class="day-heading"><span>${["周一", "周二", "周三", "周四", "周五", "周六", "周日"][i]}</span><b>${escape(d.date.slice(5).replace("-", "/"))}</b></div>${d.override !== "none" ? `<span class="override">${d.override === null ? "全天停课" : `调课自 ${escape(d.override.slice(5))}`}</span>` : ""}${d.courses.length ? d.courses.map((c, j) => `<article class="course tone-${j % 3}"><time>${clock(c.begin)}–${clock(c.finish)}</time><strong>${escape(c.name)}</strong><small>${escape(c.room || "地点未填写")}</small>${c.teacher ? `<small>${escape(c.teacher)}</small>` : ""}</article>`).join("") : `<div class="free">${d.override === null ? "提醒已取消" : "留一点空白"}</div>`}</div>`,
    )
    .join("");
  $("destination").textContent = `投递到 ${b.umo}`;
  $("next-card").innerHTML = next
    ? `<div class="next-time">${clock(next.fire)}</div><div class="next-detail"><div class="eyebrow">NEXT REMINDER</div><strong>${escape(next.name)}</strong><p>${dateTime(next.begin)} 上课 · ${escape(next.room || "地点未填写")} · 提前 ${next.lead} 分钟</p><small>${b.active ? "到时按当前会话人格生成提醒，课程详情保持准确。" : "课表已暂停，当前仅展示计划时间。"}</small></div><span class="chip${b.active ? "" : " muted"}">${b.active ? "按计划提醒" : "已暂停"}</span>`
    : '<div class="next-detail"><strong>课笺暂时翻到了最后一页。</strong><p>没有接下来的提醒，可检查有效日期或导入新课表。</p></div>';
}
function renderPlans(b) {
  const scheduler = state.scheduler;
  const future = (state.scheduler.mode || state.settings.scheduler_mode) === "future_tasks";
  $("resync").disabled = !scheduler.available || writing;
  $("regenerate").hidden = !future;
  $("plans-subtitle").textContent = future
    ? "未来 7 天内的正式提醒；每 30 秒同步，重启后重新绑定回调。"
    : "当前使用插件轮询，以下为接下来 30 次计划时间。";
  $("task-notice").textContent = future
    ? "这些任务也会出现在 AstrBot「未来任务」中。暂停单个任务只影响此次提醒；直接删除的任务不会自动重建。改时间请使用课笺的停课、调课或提前量设置。"
    : "切换到「AstrBot 未来任务」后，将自动排入一次性任务。两种模式共用投递去重记录。";
  const jobs = scheduler.jobs
    .filter((j) => j.binding_id === b?.id)
    .sort((a, c) => String(a.fire).localeCompare(String(c.fire)));
  const rows = future
    ? jobs.map(
        (j) =>
          `<tr><td><strong>${escape(j.course)}</strong><small>${dateTime(j.begin)} 上课</small><details><summary>任务需求 · ${j.custom_requirement ? "单条自定义" : "跟随全局"}</summary><p style="white-space:pre-wrap">${escape(j.requirement || "等待同步需求")}</p></details></td><td><time>${dateTime(j.fire)}</time></td><td><span class="chip${j.enabled ? "" : " muted"}">${j.enabled ? "已排入" : "已暂停"}</span></td><td><button class="quiet" data-task-id="${escape(j.id)}" data-enable="${!j.enabled}">${j.enabled ? "暂停此次" : "恢复此次"}</button><button class="quiet" data-requirement-id="${escape(j.id)}">编辑需求</button></td></tr>`,
      )
    : (b?.upcoming || []).map(
        (j) =>
          `<tr><td><strong>${escape(j.name)}</strong><small>${dateTime(j.begin)} 上课</small></td><td><time>${dateTime(j.fire)}</time></td><td><span class="chip${b.active ? "" : " muted"}">${b.active ? "计划中" : "已暂停"}</span></td><td>提前 ${j.lead} 分钟</td></tr>`,
      );
  $("plans-list").innerHTML = table(
    ["课程", "提醒时间", "状态", "操作 / 提前量"],
    rows,
    b
      ? "暂时没有排入的任务，可检查课表、提醒开关和课程日期。"
      : "请先导入课表。",
  );
}
function renderRecords() {
  const names = {
    sent: "已发送",
    failed: "发送失败",
    sending: "正在发送",
    pending: "待投递",
  };
  const rows = state.records.map(
    (r) =>
      `<tr><td><strong>${escape(r.detail.name || "旧版提醒记录")}</strong><small>${r.detail.begin ? dateTime(r.detail.begin) : escape(r.key.slice(0, 12))}</small></td><td><time>${r.updated ? dateTime(r.updated * 1000) : "—"}</time></td><td><span class="chip${r.state === "failed" ? " warn" : r.state === "sent" ? "" : " muted"}">${names[r.state] || escape(r.state)}</span></td><td>${r.attempts} 次${r.error ? `<small>${escape(r.error)}</small>` : ""}</td></tr>`,
  );
  $("records-list").innerHTML = table(
    ["课程", "最近投递时间", "状态", "尝试次数"],
    rows,
    "还没有正式投递记录。发送测试可以检查 QQ 主动消息和人格，但不会消耗正式提醒。",
  );
}
function fillSettings() {
  const form = $("settings-form");
  for (const [key, value] of Object.entries(state.settings)) {
    const control = form.elements.namedItem(key);
    if (!control) continue;
    if (control.type === "checkbox") control.checked = value;
    else control.value = Array.isArray(value) ? value.join(",") : value;
  }
}
async function refresh({ reset = false } = {}) {
  const seq = ++requestSequence;
  const params = {};
  if (!reset && selectedId) params.binding = selectedId;
  if (!reset && weekStart) params.start = weekStart;
  try {
    const data = await bridge.apiGet("state", params);
    if (seq !== requestSequence) return;
    state = data;
    selectedId = data.selected?.id || "";
    weekStart = data.selected?.start || "";
    render();
  } catch (error) {
    $("loading").hidden = true;
    warning(`数据加载失败：${error.message}。请使用右上角刷新重试。`);
    showError(error);
  }
}
async function write(endpoint, payload, { reset = false, close = false } = {}) {
  if (writing) return;
  writing = true;
  document
    .querySelectorAll(
      '#modal button[type="submit"], #settings-form button[type="submit"]',
    )
    .forEach((b) => {
      b.disabled = true;
    });
  try {
    const result = await bridge.apiPost(endpoint, payload);
    if (result.binding_id) selectedId = result.binding_id;
    if (close) $("modal").close();
    showToast(result.message || "已更新。");
    await refresh({ reset });
    return result;
  } catch (error) {
    if ($("dialog-error")) $("dialog-error").textContent = error.message;
    showError(error);
    throw error;
  } finally {
    writing = false;
    document
      .querySelectorAll(
        '#modal button[type="submit"], #settings-form button[type="submit"]',
      )
      .forEach((b) => {
        b.disabled = false;
      });
  }
}
function openModal(title, html) {
  $("modal-title").textContent = title;
  $("modal-body").innerHTML = html;
  if (!$("modal").open) $("modal").showModal();
}
function modalError() {
  return '<div id="dialog-error" class="dialog-error" role="alert"></div>';
}
function confirmation(title, message, label, callback, danger = false) {
  openModal(
    title,
    `<p class="dialog-note">${escape(message)}</p>${modalError()}<form class="dialog-actions" id="confirm-form"><button type="button" class="secondary" data-close>取消</button><button type="submit" class="primary${danger ? " danger" : ""}">${escape(label)}</button></form>`,
  );
  $("confirm-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await callback();
    } catch {
      /* Reported by write(). */
    }
  });
}
function actionPayload(action, argument = "") {
  return { binding_id: selectedId, action, argument };
}

async function openImport() {
  if (!state) {
    showToast("请等待数据加载完成。", true);
    return;
  }
  openModal("导入课表", '<p class="dialog-note">正在读取已有 QQ 会话…</p>');
  let targets = [];
  let targetsError = "";
  try {
    targets = (await bridge.apiGet("targets")).targets;
  } catch (error) {
    targetsError = error.message;
  }
  if (!$("modal").open) return;
  const bindings = state.bindings;
  openModal(
    "导入课表",
    `<form id="import-form" class="dialog-form"><label>导入到<select id="import-binding">${bindings.map((b) => `<option value="${escape(b.id)}"${b.id === selectedId ? " selected" : ""}>${escape(b.name)} · ${escape(b.nickname)}</option>`).join("")}<option value="new"${!bindings.length ? " selected" : ""}>＋ 添加到其他 QQ 会话</option></select></label><div id="new-target" class="dialog-form"${bindings.length ? " hidden" : ""}><label>投递会话<select id="import-umo"><option value="">${targets.length ? "选择已有 QQ 会话" : "暂无 QQ 会话，请先与 bot 对话"}</option>${targets.map((t) => `<option value="${escape(t.umo)}">${escape(t.title)} · ${t.group ? "群聊" : "私聊"} · ${escape(t.umo)}</option>`).join("")}</select></label><div class="form-grid"><label>提醒对象账号标识<input id="import-sender" placeholder="QQ 号或适配器中的账号标识"/><small>群聊用于 @ 本人；请核对账号，避免提醒错人。</small></label><label>课表持有者称呼<input id="import-nickname" placeholder="例如：我的课表" maxlength="40"/></label></div>${targetsError ? `<div class="dialog-error">会话列表读取失败：${escape(targetsError)}</div>` : ""}</div><label>导入方式<select id="import-kind"><option value="ics">WakeUp 日历文件 · 推荐</option><option value="share">WakeUp 分享文本 / 链接</option><option value="backup">WakeUp 备份文本 / JSON</option></select></label><label id="upload-area" class="upload-area">选择 WakeUp 导出的日历文件<small>.ics / .ical · 最大 2 MiB</small><input id="import-file" type="file" accept=".ics,.ical,text/calendar"/></label><label id="import-text-label"><span id="import-text-title">或粘贴日历内容</span><textarea id="import-text" rows="4" placeholder="BEGIN:VCALENDAR … END:VCALENDAR"></textarea></label><p class="dialog-note">先预览，再确认导入。同一会话、同一持有者的原课表将被替换；提前量、暂停状态和日期覆盖会保留。</p><div id="import-preview" class="preview" hidden></div>${modalError()}<div class="dialog-actions"><button type="button" class="secondary" data-close>取消</button><button type="submit" id="import-submit" class="primary">预览课表</button></div></form>`,
  );
  let previewed = false;
  let pendingPayload = null;
  let importVersion = 0;
  const importForm = $("import-form");
  const invalidate = () => {
    importVersion++;
    previewed = false;
    pendingPayload = null;
    $("import-preview").hidden = true;
    $("import-submit").textContent = "预览课表";
  };
  $("import-form").addEventListener("input", invalidate);
  $("import-binding").addEventListener("change", () => {
    $("new-target").hidden = $("import-binding").value !== "new";
    invalidate();
  });
  $("import-kind").addEventListener("change", () => {
    const kind = $("import-kind").value;
    $("upload-area").hidden = kind !== "ics";
    $("import-text-title").textContent =
      kind === "ics"
        ? "或粘贴日历内容"
        : kind === "share"
          ? "完整的分享文本、链接或口令"
          : "WakeUp 五段备份文本或兼容 JSON";
    $("import-text").placeholder =
      kind === "ics" ? "BEGIN:VCALENDAR … END:VCALENDAR" : "在这里粘贴完整内容";
    $("import-text").value = "";
    invalidate();
  });
  $("import-file").addEventListener("change", async () => {
    const file = $("import-file").files[0];
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) {
      $("dialog-error").textContent = "文件超过 2 MiB，请重新选择。";
      $("import-file").value = "";
      return;
    }
    $("import-text").value = await file.text();
    $("dialog-error").textContent = "";
    invalidate();
  });
  $("import-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (writing) return;
    $("dialog-error").textContent = "";
    if (previewed) {
      try {
        await write(
          "import",
          { ...pendingPayload, confirm: true },
          { close: true },
        );
      } catch {
        invalidate();
      }
      return;
    }
    const target = $("import-binding").value;
    const payload = {
      kind: $("import-kind").value,
      content: $("import-text").value,
      filename: $("import-file").files[0]?.name || "WakeUp 日历",
      ...(target === "new"
        ? {
            umo: $("import-umo").value,
            sender: $("import-sender").value,
            nickname: $("import-nickname").value,
          }
        : { binding_id: target }),
    };
    $("import-submit").disabled = true;
    const version = importVersion;
    try {
      const result = await bridge.apiPost("import", payload);
      if (
        version !== importVersion ||
        !importForm.isConnected ||
        !$("modal").open
      )
        return;
      const p = result.preview;
      $("import-preview").innerHTML =
        `<strong>《${escape(p.name)}》</strong><br/>${p.count} ${p.kind === "calendar" ? "课次" : "条排课"} · ${escape(p.first)}${p.last ? ` — ${escape(p.last)}` : " 起"}<br/>投递到：${escape(p.target)}${p.warnings.length ? `<br/>${p.warnings.map(escape).join("<br/>")}` : ""}`;
      $("import-preview").hidden = false;
      $("import-submit").textContent = "确认导入";
      pendingPayload = payload;
      previewed = true;
    } catch (error) {
      if (importForm.isConnected) $("dialog-error").textContent = error.message;
    } finally {
      if (importForm.isConnected) $("import-submit").disabled = false;
    }
  });
}
function configureBinding() {
  const b = state.selected;
  openModal(
    "课表提醒设置",
    `<form id="binding-form" class="dialog-form"><p class="dialog-note">${escape(b.name)} · ${escape(b.nickname || b.sender)}<br/>时区：${escape(b.timezone)}</p><label>提前提醒来源<select id="lead-source"><option value="default"${!b.custom_lead && (b.kind !== "calendar" || b.respect_ics_alarms === false) ? " selected" : ""}>跟随插件默认设置</option><option value="custom"${b.custom_lead ? " selected" : ""}>自定义提前量</option>${b.kind === "calendar" ? `<option value="calendar"${!b.custom_lead && b.respect_ics_alarms !== false ? " selected" : ""}>沿用日历闹钟</option>` : ""}</select></label><label id="lead-value-label">提前分钟数<input id="lead-value" value="${escape(b.lead_minutes.join(","))}" placeholder="20 或 30,10"/><small>1–1440 分钟，最多 3 次。日历模式下无闹钟的课程使用这些默认值。</small></label>${b.kind !== "calendar" ? `<label>第一教学周周一<input id="semester-monday" type="date" value="${escape(b.monday)}" required/></label>` : ""}<label class="switch-row"><span><strong>开启此课表提醒</strong></span><input id="binding-enabled" type="checkbox" class="switch"${b.enabled ? " checked" : ""}/></label>${modalError()}<div class="dialog-actions"><button type="button" class="secondary" data-close>取消</button><button type="submit" class="primary">保存提醒设置</button></div></form>`,
  );
  $("binding-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await write(
        "action",
        {
          ...actionPayload("configure"),
          lead_source: $("lead-source").value,
          lead_minutes: $("lead-value").value,
          enabled: $("binding-enabled").checked,
          ...($("semester-monday")
            ? { monday: $("semester-monday").value }
            : {}),
        },
        { close: true },
      );
    } catch {
      /* Kept in the dialog. */
    }
  });
}
function openRequirement(id) {
  const job = state.scheduler.jobs.find((j) => j.id === id);
  if (!job) return;
  openModal("编辑此次提醒的任务需求", `<form id="requirement-form" class="dialog-form"><p class="dialog-note">${escape(job.course)} · ${dateTime(job.begin)}。内容同步到 AstrBot 未来任务，并用于本次人格生成；准确课程信息仍由程序追加。</p><label>任务需求<textarea id="requirement-text" rows="7" maxlength="4000" required>${escape(job.requirement || "")}</textarea></label><label><input id="requirement-reset" type="checkbox"/> 恢复跟随全局模板</label><p class="dialog-note">单条自定义需求在修改全局模板后仍保留。勾选恢复后，将使用最新全局模板。</p>${modalError()}<div class="dialog-actions"><button type="button" class="secondary" data-close>取消</button><button type="submit" class="primary">保存并同步</button></div></form>`);
  $("requirement-reset").addEventListener("change", () => {
    $("requirement-text").disabled = $("requirement-reset").checked;
  });
  $("requirement-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await write("tasks", { operation: "requirement", id, text: $("requirement-text").value, reset: $("requirement-reset").checked }, { close: true });
    } catch { /* Keep the edited requirement visible. */ }
  });
}

function openOverride() {
  const b = state.selected;
  const date = b.days.find((d) => d.today)?.date || b.start;
  openModal(
    "停课与调课",
    `<form id="override-form" class="dialog-form"><label>操作<select id="override-action"><option value="停课">停课 · 暂停某天的全部课程</option><option value="调课">调课 · 某天改上另一日期的课程</option><option value="恢复">恢复 · 移除当天覆盖</option></select></label><div class="form-grid"><label>实际日期<input id="override-date" type="date" value="${escape(date)}" required/></label><label id="source-date-label" hidden>参照原日期<input id="override-source" type="date"/></label></div><p class="dialog-note">调课会替换实际日期的全部课程，不会自动取消原日期。若周六补周一课、周一放假，请分别设置调课和停课。重新导入会保留这些覆盖。</p><div class="override-list">${
      Object.entries(b.overrides)
        .sort()
        .map(
          ([day, source]) =>
            `<div class="override-row"><span>${escape(day)} · ${source === null ? "停课" : `改上 ${escape(source)}`}</span><button type="button" class="quiet" data-restore="${escape(day)}">恢复</button></div>`,
        )
        .join("") || '<p class="dialog-note">还没有日期覆盖。</p>'
    }</div>${modalError()}<div class="dialog-actions"><button type="button" class="secondary" data-close>取消</button><button type="submit" class="primary">应用日期安排</button></div></form>`,
  );
  $("override-action").addEventListener("change", () => {
    $("source-date-label").hidden = $("override-action").value !== "调课";
    $("override-source").required = $("override-action").value === "调课";
  });
  $("override-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const action = $("override-action").value;
    const argument =
      $("override-date").value +
      (action === "调课" ? ` ${$("override-source").value}` : "");
    try {
      await write("action", actionPayload(action, argument), { close: true });
    } catch {
      /* Kept in the dialog. */
    }
  });
}

$("refresh").addEventListener("click", () => {
  if (bridge) refresh();
});
$("open-import").addEventListener("click", openImport);
document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.dataset.tab) activate(button.dataset.tab);
  if (button.hasAttribute("data-open-import")) openImport();
  if (button.hasAttribute("data-close") && !writing) $("modal").close();
  if (button.dataset.requirementId) openRequirement(button.dataset.requirementId);
  if (button.dataset.taskId) {
    try {
      await write("tasks", {
        operation: "toggle",
        id: button.dataset.taskId,
        enabled: button.dataset.enable === "true",
      });
    } catch {
      /* Toast shown. */
    }
  }
  if (button.dataset.restore) {
    try {
      await write("action", actionPayload("恢复", button.dataset.restore), {
        close: true,
      });
    } catch {
      /* Toast shown. */
    }
  }
});
$("close-modal").addEventListener("click", () => {
  if (!writing) $("modal").close();
});
$("modal").addEventListener("cancel", (event) => {
  if (writing) event.preventDefault();
});
$("binding-select").addEventListener("change", () => {
  selectedId = $("binding-select").value;
  weekStart = "";
  refresh();
});
$("previous-week").addEventListener("click", () => {
  weekStart = shiftDate(weekStart, -7);
  refresh();
});
$("next-week").addEventListener("click", () => {
  weekStart = shiftDate(weekStart, 7);
  refresh();
});
$("current-week").addEventListener("click", () => {
  weekStart = "";
  refresh();
});
$("configure-binding").addEventListener("click", configureBinding);
$("open-override").addEventListener("click", openOverride);
$("toggle-binding").addEventListener("click", async () => {
  try {
    await write(
      "action",
      actionPayload(state.selected.enabled ? "暂停" : "开启"),
    );
  } catch {
    /* Toast shown. */
  }
});
$("test-binding").addEventListener("click", () =>
  confirmation(
    "发送测试提醒",
    `将向 ${state.selected.umo} 发送下一门课程的测试提醒，可能调用当前会话模型。测试不会消耗正式提醒。`,
    "发送测试",
    () => write("action", actionPayload("测试"), { close: true }),
  ),
);
$("delete-binding").addEventListener("click", () =>
  confirmation(
    "删除这张课表？",
    `将删除《${state.selected.name}》以及本课表的投递记录，并清理它生成的未来任务。此操作无法在插件内撤销；其他持有者的课表不受影响。`,
    "删除课表",
    () =>
      write(
        "action",
        { ...actionPayload("删除"), confirm: true },
        { close: true, reset: true },
      ),
    true,
  ),
);
$("resync").addEventListener("click", async () => {
  try {
    await write("tasks", { operation: "sync" });
  } catch {
    /* Toast shown. */
  }
});
$("regenerate").addEventListener("click", () =>
  confirmation(
    "恢复已删除的任务",
    "将重新生成课笺管理的、未来 7 天内被你手动删除的提醒。已发送的提醒不会重发；其他插件和手动创建的任务不受影响。",
    "重新生成",
    () =>
      write(
        "tasks",
        { operation: "sync", reset_deleted: true },
        { close: true },
      ),
  ),
);
$("export-binding").addEventListener("click", async () => {
  try {
    await bridge.download(
      "export",
      { binding: selectedId },
      "classnote-calendar.ics",
    );
    showToast("课表日历已导出。");
  } catch (error) {
    showError(error);
  }
});
$("settings-form").addEventListener("input", () => {
  settingsDirty = true;
});
$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {};
  for (const key of Object.keys(state.settings)) {
    const control = form.elements.namedItem(key);
    if (control)
      payload[key] =
        control.type === "checkbox"
          ? control.checked
          : key === "catchup_seconds"
            ? Number(control.value)
            : control.value;
  }
  try {
    const result = await write("settings", payload);
    if (result) {
      settingsDirty = false;
      fillSettings();
    }
  } catch {
    /* Keep unsaved inputs. */
  }
});
try {
  if (!bridge)
    throw new Error(
      "请从 AstrBot「插件 → 课笺 → 课表管理」打开此页面，并使用支持插件视图的新版 AstrBot。",
    );
  const context = await bridge.ready();
  const theme = (ctx) => {
    document.documentElement.dataset.theme = ctx.isDark ? "dark" : "light";
  };
  theme(context);
  if (bridge.onContext) bridge.onContext(theme);
  await refresh();
  setInterval(() => {
    if (!document.hidden && !writing && !settingsDirty && !$("modal").open)
      refresh();
  }, 30000);
} catch (error) {
  $("loading").hidden = true;
  $("connection").textContent = "未连接";
  warning(error.message);
}
