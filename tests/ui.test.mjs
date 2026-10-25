import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { JSDOM } from "jsdom";

const html = await readFile(
  new URL("../views/classnote/index.html", import.meta.url),
  "utf8",
);
const source = await readFile(
  new URL("../views/classnote/app.js", import.meta.url),
  "utf8",
);
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
function fixture() {
  const b = {
    id: "binding",
    name: "2026 秋季学期",
    nickname: "测试课表",
    umo: "qq:FriendMessage:12345",
    sender: "12345",
    enabled: true,
    active: true,
    kind: "weekly",
    timezone: "Asia/Shanghai",
    monday: "2026-09-07",
    lead_minutes: [20],
    custom_lead: true,
    overrides: {},
    start: "2026-10-26",
    warnings: [],
    days: Array.from({ length: 7 }, (_, i) => ({
      date: `2026-10-${26 + i}`,
      today: i === 0,
      override: "none",
      courses:
        i === 0
          ? [
              {
                id: "course",
                name: "大学化学",
                room: "逸夫楼 A201",
                begin: "2026-10-26T08:00:00+08:00",
                finish: "2026-10-26T09:40:00+08:00",
              },
            ]
          : [],
    })),
    upcoming: [
      {
        key: "course-key",
        name: "大学化学",
        room: "逸夫楼 A201",
        lead: 20,
        fire: "2026-10-26T07:40:00+08:00",
        begin: "2026-10-26T08:00:00+08:00",
      },
    ],
  };
  return {
    version: "0.3.1",
    bindings: [b],
    selected: b,
    records: [],
    settings: {
      enabled: true,
      admins_only: true,
      allow_group: false,
      auto_import_ics: true,
      timezone: "Asia/Shanghai",
      default_lead_minutes: [20],
      use_llm_persona: true,
      fallback_opener: "准备去上课吧。",
      catchup_seconds: 300,
      scheduler_mode: "future_tasks",
    },
    scheduler: {
      available: true,
      mode: "future_tasks",
      error: "",
      horizon_days: 7,
      last_sync: "2026-10-25T00:00:00Z",
      truncated: false,
      jobs: [
        {
          id: "native-1",
          key: "course-key",
          binding_id: "binding",
          enabled: true,
          course: "大学化学",
          fire: "2026-10-26T07:40:00+08:00",
          begin: "2026-10-26T08:00:00+08:00",
        },
      ],
    },
  };
}
async function setup(
  t,
  { data = fixture(), connected = true, failGet = false } = {},
) {
  const dom = new JSDOM(html, {
    runScripts: "outside-only",
    pretendToBeVisual: true,
    url: "https://classnote.test/",
  });
  t.after(() => dom.window.close());
  const w = dom.window;
  w.HTMLDialogElement.prototype.showModal = function () {
    this.open = true;
  };
  w.HTMLDialogElement.prototype.close = function () {
    this.open = false;
  };
  const calls = [];
  if (connected)
    w.AstrBotPluginView = {
      ready: async () => ({ isDark: false }),
      onContext: () => {},
      apiGet: async (endpoint, params) => {
        calls.push({ method: "GET", endpoint, params });
        if (endpoint === "targets") return { targets: [] };
        if (failGet) throw new Error("测试加载错误");
        return structuredClone(data);
      },
      apiPost: async (endpoint, payload) => {
        calls.push({ method: "POST", endpoint, payload });
        if (endpoint === "import" && !payload.confirm)
          return {
            preview: {
              name: "导入预览",
              count: 3,
              kind: "calendar",
              first: "2026-10-26",
              last: "2026-12-31",
              target: data.selected.umo,
              warnings: [],
            },
          };
        if (endpoint === "action" && payload.action === "暂停") {
          data.selected.enabled = false;
          data.selected.active = false;
        }
        if (endpoint === "tasks" && payload.operation === "toggle")
          data.scheduler.jobs[0].enabled = payload.enabled;
        return { message: "操作成功" };
      },
      download: async (...args) => {
        calls.push({ method: "DOWNLOAD", args });
      },
    };
  await w.eval(`(async () => { ${source}\n })()`);
  const $ = (id) => w.document.getElementById(id);
  const click = async (id) => {
    $(id).click();
    await settle();
  };
  const submit = async (id) => {
    $(id).dispatchEvent(
      new w.Event("submit", { bubbles: true, cancelable: true }),
    );
    await settle();
  };
  const input = (id, value) => {
    $(id).value = value;
    $(id).dispatchEvent(new w.Event("input", { bubbles: true }));
  };
  return { dom, w, $, calls, click, submit, input };
}

test("renders a seven-day timetable, exact timezone and current persona controls", async (t) => {
  const { $, w } = await setup(t);
  assert.equal($("app").hidden, false);
  assert.equal(w.document.querySelectorAll(".day").length, 7);
  assert.match($("calendar").textContent, /大学化学/);
  assert.match($("week-label").textContent, /Asia\/Shanghai/);
  assert.equal($("banner").hidden, true);
});
test("all four tabs expose their own content", async (t) => {
  const { w, $ } = await setup(t);
  for (const name of ["plans", "records", "settings", "overview"]) {
    w.document.querySelector(`[data-tab="${name}"]`).click();
    assert.equal($(`tab-${name}`).hidden, false);
  }
});
test("pause updates the backend and renders the changed state", async (t) => {
  const { click, calls, $ } = await setup(t);
  await click("toggle-binding");
  assert.equal(calls.find((c) => c.method === "POST").payload.action, "暂停");
  assert.equal($("toggle-binding").textContent, "开启提醒");
});
test("settings serialize switches as booleans and catch-up as a number", async (t) => {
  const { w, $, submit, calls } = await setup(t);
  const form = $("settings-form");
  form.elements.allow_group.checked = true;
  form.elements.catchup_seconds.value = "180";
  form.dispatchEvent(new w.Event("input", { bubbles: true }));
  await submit("settings-form");
  const payload = calls.find((c) => c.endpoint === "settings").payload;
  assert.equal(payload.allow_group, true);
  assert.equal(payload.catchup_seconds, 180);
  assert.equal(payload.scheduler_mode, "future_tasks");
});
test("import requires preview followed by explicit confirmation", async (t) => {
  const { click, input, submit, calls, $ } = await setup(t);
  await click("open-import");
  input("import-text", "BEGIN:VCALENDAR\nEND:VCALENDAR");
  await submit("import-form");
  assert.equal($("import-submit").textContent, "确认导入");
  assert.equal(
    calls.filter((c) => c.endpoint === "import")[0].payload.confirm,
    undefined,
  );
  await submit("import-form");
  assert.equal(
    calls.filter((c) => c.endpoint === "import")[1].payload.confirm,
    true,
  );
  assert.equal($("modal").open, false);
});
test("editing an import invalidates the previous preview", async (t) => {
  const { click, input, submit, calls, $ } = await setup(t);
  await click("open-import");
  input("import-text", "calendar one");
  await submit("import-form");
  input("import-text", "calendar two");
  assert.equal($("import-preview").hidden, true);
  await submit("import-form");
  const imports = calls.filter((c) => c.endpoint === "import");
  assert.equal(imports[1].payload.confirm, undefined);
  assert.equal(imports[1].payload.content, "calendar two");
});
test("destructive delete stays behind a confirmation dialog", async (t) => {
  const { click, calls, $, submit } = await setup(t);
  await click("delete-binding");
  assert.equal(calls.filter((c) => c.method === "POST").length, 0);
  assert.match($("modal-title").textContent, /删除/);
  await submit("confirm-form");
  assert.equal(calls.find((c) => c.method === "POST").payload.confirm, true);
});
test("task switches address the native task id and do not send messages", async (t) => {
  const { w, calls } = await setup(t);
  w.document.querySelector('[data-tab="plans"]').click();
  w.document.querySelector("[data-task-id]").click();
  await settle();
  const post = calls.find((c) => c.method === "POST");
  assert.equal(post.endpoint, "tasks");
  assert.equal(post.payload.id, "native-1");
  assert.equal(post.payload.enabled, false);
});
test("course names and rooms are escaped rather than executed as HTML", async (t) => {
  const data = fixture();
  data.selected.days[0].courses[0].name = '<img src=x onerror="alert(1)">';
  const { $, w } = await setup(t, { data });
  assert.match($("calendar").textContent, /<img/);
  assert.equal(w.document.querySelectorAll(".course img").length, 0);
});
test("empty state guides first import without broken binding actions", async (t) => {
  const data = fixture();
  data.selected = null;
  data.bindings = [];
  const { $ } = await setup(t, { data });
  assert.equal($("empty").hidden, false);
  assert.equal($("binding-tools").hidden, true);
});
test("backend failure is visible and keeps a retry control", async (t) => {
  const { $ } = await setup(t, { failGet: true });
  assert.match($("banner").textContent, /测试加载错误/);
  assert.equal($("loading").hidden, true);
  assert.equal($("refresh").disabled, false);
});
test("scheduler fallback displays the active polling mode", async (t) => {
  const data = fixture();
  data.scheduler.available = false;
  data.scheduler.mode = "polling";
  data.scheduler.error = "已自动使用插件轮询提醒。";
  const { $ } = await setup(t, { data });
  assert.equal($("engine-label").textContent, "插件轮询");
  assert.match($("banner").textContent, /轮询/);
});
test("opening outside AstrBot explains the missing bridge", async (t) => {
  const { $ } = await setup(t, { connected: false });
  assert.match($("banner").textContent, /AstrBot/);
  assert.equal($("connection").textContent, "未连接");
});
test("export uses the authorized bridge download helper", async (t) => {
  const { click, calls } = await setup(t);
  await click("export-binding");
  const download = calls.find((c) => c.method === "DOWNLOAD");
  assert.deepEqual(JSON.parse(JSON.stringify(download.args)), [
    "export",
    { binding: "binding" },
    "classnote-calendar.ics",
  ]);
});
test("week navigation sends an ISO date seven days earlier", async (t) => {
  const { click, calls } = await setup(t);
  await click("previous-week");
  assert.equal(calls.at(-1).params.start, "2026-10-19");
});
