"""Dashboard bridge endpoints. AstrBot authenticates every extension API request."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request, stream_response

from .core import (
    TimetableError,
    bounded_int,
    clean,
    delivery_key,
    due_reminders,
    iso_date,
    next_reminders,
    occurrences,
    parse_timetable,
    reminder_key,
    reminder_leads,
)
from .ics import MAX_BYTES, parse_calendar
from .task_text import DEFAULT_REQUIREMENT, render_requirement, validate_requirement
from .wakeup import import_share

PLUGIN = "astrbot_plugin_classnote"
ACTIONS = {
    "提前",
    "暂停",
    "开启",
    "开学",
    "停课",
    "恢复",
    "调课",
    "测试",
    "删除",
    "configure",
}
SETTING_DEFAULTS = {
    "enabled": True,
    "admins_only": True,
    "allow_group": False,
    "auto_import_ics": True,
    "timezone": "Asia/Shanghai",
    "default_lead_minutes": [20],
    "use_llm_persona": True,
    "fallback_opener": "该准备去上课了，记得带齐需要的东西。",
    "catchup_seconds": 300,
    "scheduler_mode": "future_tasks",
    "task_requirement_template": DEFAULT_REQUIREMENT,
}


class Dashboard:
    def __init__(self, plugin):
        self.plugin = plugin
        self._handlers = []

    def close(self):
        routes = getattr(self.plugin.context, "registered_web_apis", None)
        if isinstance(routes, list):
            routes[:] = [route for route in routes if route[1] not in self._handlers]
        self._handlers.clear()

    def register(self):
        for route, method, handler in (
            ("state", "GET", self.state),
            ("targets", "GET", self.targets),
            ("import", "POST", self.import_schedule),
            ("action", "POST", self.action),
            ("settings", "POST", self.settings),
            ("tasks", "POST", self.tasks),
            ("export", "GET", self.export),
        ):

            async def endpoint(_handler=handler):
                if not self.plugin.store:
                    return error_response(
                        "课笺尚未初始化，请稍后刷新。", status_code=503
                    )
                try:
                    return await _handler()
                except (TimetableError, ZoneInfoNotFoundError) as exc:
                    return error_response(str(exc))
                except Exception as exc:
                    logger.error(
                        f"[classnote] dashboard request failed: {type(exc).__name__}"
                    )
                    return error_response(
                        "操作未完成，请检查插件日志后重试。", status_code=500
                    )

            self._handlers.append(endpoint)
            self.plugin.context.register_web_api(
                f"/{PLUGIN}/{route}", endpoint, [method], f"ClassNote {route}"
            )

    async def payload(self):
        body = await request.json(default={})
        if not isinstance(body, dict):
            raise TimetableError("请求必须为 JSON 对象。")
        return body

    def binding(self, binding_id):
        binding = self.plugin.store.get(str(binding_id or ""))
        if not binding:
            raise TimetableError("课表不存在，可能已在其他页面删除，请刷新。")
        return binding

    @staticmethod
    def event(binding):
        return SimpleNamespace(
            unified_msg_origin=binding["umo"],
            get_sender_id=lambda: binding["sender"],
            get_sender_name=lambda: binding["nickname"],
            get_platform_name=lambda: binding["platform"],
            get_group_id=lambda: "group" if binding["group"] else "",
        )

    async def state(self):
        bindings = self.plugin.store.all()
        selected_id = request.query.get("binding", "")
        selected = (
            self.binding(selected_id)
            if selected_id
            else (bindings[0] if bindings else None)
        )
        now = datetime.now(timezone.utc)
        summary = [
            {
                "id": b["id"],
                "name": b["schedule"]["name"],
                "nickname": b["nickname"] or b["sender"],
                "umo": b["umo"],
                "sender": b["sender"],
                "group": b["group"],
                "enabled": b["enabled"],
                "active": self.plugin._active(b),
                "timezone": b["timezone"],
                "kind": b["schedule"].get("kind", "weekly"),
                "count": len(b["schedule"]["courses"]),
            }
            for b in bindings
        ]
        detail = None
        if selected:
            local = now.astimezone(ZoneInfo(selected["timezone"]))
            start = request.query.get("start")
            first = (
                iso_date(start)
                if start
                else local.date() - timedelta(days=local.weekday())
            )
            days = []
            for delta in range(7):
                on = first + timedelta(days=delta)
                rows = occurrences(
                    selected["schedule"],
                    on,
                    selected["timezone"],
                    selected["overrides"],
                )
                days.append(
                    {
                        "date": on.isoformat(),
                        "today": on == local.date(),
                        "override": selected["overrides"].get(on.isoformat(), "none"),
                        "courses": [
                            {
                                "id": c["id"],
                                "name": c["name"],
                                "room": c["room"],
                                "teacher": c.get("teacher", ""),
                                "begin": c["begin"].isoformat(),
                                "finish": c["finish"].isoformat(),
                            }
                            for c in rows
                        ],
                    }
                )
            upcoming = []
            for c in next_reminders(selected, now, 30):
                key = reminder_key(selected, c, c["lead"])
                upcoming.append(
                    {
                        "key": key,
                        "name": c["name"],
                        "room": c["room"],
                        "lead": c["lead"],
                        "fire": c["fire"].isoformat(),
                        "begin": c["begin"].isoformat(),
                    }
                )
            detail = {
                k: v for k, v in selected.items() if k not in ("schedule", "epoch")
            }
            detail.update(
                name=selected["schedule"]["name"],
                kind=selected["schedule"].get("kind", "weekly"),
                monday=selected["schedule"]["monday"],
                warnings=selected["schedule"].get("warnings", []),
                days=days,
                start=first.isoformat(),
                upcoming=upcoming,
                active=self.plugin._active(selected),
            )
        return json_response(
            {
                "version": "0.4.0",
                "now": now.isoformat(),
                "bindings": summary,
                "selected": detail,
                "settings": {
                    k: self.plugin.config.get(k, v) for k, v in SETTING_DEFAULTS.items()
                },
                "scheduler": self.plugin.future_tasks.status(),
                "records": self.plugin.store.records(
                    selected["id"] if selected else None
                ),
            }
        )

    async def target_list(self):
        # Read session identifiers and labels only; never fetch conversation history.
        manager = getattr(self.plugin.context, "conversation_manager", None)
        if manager is None:
            return []
        rows, _ = await manager.get_filtered_conversations(
            page=1, page_size=500, include_history=False
        )
        targets = {}
        for row in rows:
            umo = str(getattr(row, "user_id", "") or "")
            if not umo or umo in targets:
                continue
            platform_id, _, _ = umo.partition(":")
            platform = self.plugin.context.get_platform_inst(platform_id)
            meta = platform.meta() if platform else None
            name = getattr(meta, "name", "")
            if name not in ("aiocqhttp", "qq_official", "qq_official_webhook"):
                continue
            targets[umo] = {
                "umo": umo,
                "platform": name,
                "title": clean(getattr(row, "title", ""), 80) or umo,
                "group": ":GroupMessage:" in umo,
            }
        return list(targets.values())

    async def targets(self):
        return json_response({"targets": await self.target_list()})

    async def import_schedule(self):
        body = await self.payload()
        kind = body.get("kind", "ics")
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            raise TimetableError("请选择日历文件或填写分享文本。")
        if len(content.encode("utf-8")) > MAX_BYTES:
            raise TimetableError("文件或文本超过 2 MiB。")
        if body.get("binding_id"):
            target = self.binding(body["binding_id"])
        else:
            targets = {item["umo"]: item for item in await self.target_list()}
            target = targets.get(str(body.get("umo", "")))
            if target is None:
                raise TimetableError("请选择已有的 QQ 会话；列表为空时先与 bot 对话。")
            sender = body.get("sender")
            if (
                not isinstance(sender, str)
                or not sender.strip()
                or len(sender) > 256
                or any(c.isspace() for c in sender)
            ):
                raise TimetableError(
                    "请填写有效的提醒对象账号标识（群聊用于 @ 本人）。"
                )
            target = {
                **target,
                "sender": sender.strip(),
                "nickname": clean(body.get("nickname") or sender, 40),
            }
        if target["group"] and not self.plugin.config.get("allow_group", False):
            raise TimetableError("群聊导入尚未开启，请先到设置中开放。")
        zone = target.get(
            "timezone", self.plugin.config.get("timezone", "Asia/Shanghai")
        )
        if kind == "ics":
            schedule = await asyncio.to_thread(
                parse_calendar,
                content,
                zone,
                clean(body.get("filename", "WakeUp 日历")),
            )
        elif kind == "share":
            if self.plugin.http is None:
                raise TimetableError("网络客户端尚未初始化，请稍后重试。")
            schedule = await import_share(self.plugin.http, content)
        elif kind == "backup":
            schedule = await asyncio.to_thread(parse_timetable, content)
        else:
            raise TimetableError("不支持的导入格式。")
        preview = {
            "name": schedule["name"],
            "count": len(schedule["courses"]),
            "kind": schedule.get("kind", "weekly"),
            "warnings": schedule.get("warnings", []),
            "first": schedule.get("first_date", schedule["monday"]),
            "last": schedule.get("last_date"),
            "target": target["umo"],
        }
        if body.get("confirm") is not True:
            return json_response({"preview": preview})
        response = await self.plugin._save_schedule(self.event(target), schedule)
        return json_response(
            {
                "message": response,
                "binding_id": self.plugin._binding_id(self.event(target)),
            }
        )

    async def action(self):
        body = await self.payload()
        binding = self.binding(body.get("binding_id"))
        action = body.get("action")
        if action not in ACTIONS:
            raise TimetableError("不支持的课表操作。")
        if action == "删除" and body.get("confirm") is not True:
            raise TimetableError("删除课表需要明确确认。")
        if not isinstance(body.get("argument", ""), str):
            raise TimetableError("操作参数必须是文本。")
        if action == "configure":
            source = body.get("lead_source")
            leads = self.plugin._lead(
                self.plugin.config.get("default_lead_minutes", [20])
                if source == "default"
                else body.get("lead_minutes", [])
            )
            if source not in ("calendar", "custom", "default") or (
                source == "calendar" and binding["schedule"].get("kind") != "calendar"
            ):
                raise TimetableError("请选择有效的提前提醒来源。")
            if not isinstance(body.get("enabled"), bool):
                raise TimetableError("提醒开关必须为布尔值。")
            monday = None
            if "monday" in body:
                monday = iso_date(body["monday"])
                if (
                    binding["schedule"].get("kind") == "calendar"
                    or monday.weekday() != 0
                ):
                    raise TimetableError("仅教学周课表可设置开学日期，且必须为周一。")
            async with self.plugin.send_lock:
                binding = self.binding(body.get("binding_id"))
                binding.update(
                    lead_minutes=leads,
                    custom_lead=source == "custom",
                    respect_ics_alarms=source == "calendar",
                    enabled=body["enabled"],
                )
                if monday:
                    binding["schedule"]["monday"] = monday.isoformat()
                self.plugin.store.save(binding)
            await self.plugin._sync_future()
            return json_response(
                {"message": "课表提醒设置已保存，任务已按新安排同步。"}
            )
        response = await self.plugin._dispatch(
            self.event(binding), action, body.get("argument", "")
        )
        return json_response({"message": response})

    async def settings(self):
        body = await self.payload()
        if not body or set(body) - set(SETTING_DEFAULTS):
            raise TimetableError("包含未知设置项。")
        values = {}
        for key, value in body.items():
            if isinstance(SETTING_DEFAULTS[key], bool):
                if not isinstance(value, bool):
                    raise TimetableError("开关必须为布尔值。")
            elif key == "timezone":
                if not isinstance(value, str):
                    raise TimetableError("时区必须是文本。")
                ZoneInfo(value)
            elif key == "default_lead_minutes":
                value = self.plugin._lead(value)
            elif key == "task_requirement_template":
                value = validate_requirement(value, template=True)
            elif key == "catchup_seconds":
                value = bounded_int(value, 30, 900, "补发窗口")
            elif key == "scheduler_mode":
                if value not in ("future_tasks", "polling"):
                    raise TimetableError("请选择未来任务或插件轮询。")
            elif key == "fallback_opener":
                if not isinstance(value, str) or not clean(value, 100):
                    raise TimetableError("请填写模型不可用时的备用提醒开头。")
                value = clean(value, 100)
            values[key] = value
        async with self.plugin.send_lock:
            previous = dict(self.plugin.config)
            self.plugin.config.update(values)
            try:
                save = getattr(self.plugin.config, "save_config", None)
                if save:
                    save()
            except BaseException:
                self.plugin.config.clear()
                self.plugin.config.update(previous)
                raise
        await self.plugin._sync_future()
        return json_response(
            {
                "message": "设置已保存并同步已有任务；个人提前量和日历自带提醒优先。"
                + (
                    f" {self.plugin.future_tasks.error}"
                    if self.plugin.future_tasks.error
                    else ""
                )
            }
        )

    async def tasks(self):
        body = await self.payload()
        scheduler = self.plugin.future_tasks
        if body.get("operation") == "requirement":
            reset = body.get("reset") is True
            text = "" if reset else validate_requirement(body.get("text"))
            async with scheduler.lock:
                jobs = (
                    await scheduler.manager.list_jobs() if scheduler.available else []
                )
                job = next(
                    (
                        j
                        for j in jobs
                        if j.job_id == body.get("id") and scheduler.owned(j)
                    ),
                    None,
                )
                if job is None:
                    raise TimetableError("课笺任务不存在，请刷新列表。")
                key = job.payload["key"]
                binding = self.binding(job.payload["binding_id"])
                item = next(
                    (
                        c
                        for c in (
                            due_reminders(binding, datetime.now(timezone.utc))
                            + next_reminders(binding, datetime.now(timezone.utc), 201)
                        )
                        if reminder_key(binding, c, c["lead"]) == key
                    ),
                    None,
                )
                if reset and item is None:
                    raise TimetableError("任务即将执行或已过期，请同步后重试。")
                record = self.plugin.store.tasks().get(key, {})
                if reset:
                    record.pop("requirement_override", None)
                    text = render_requirement(self.plugin.config, binding, item)
                else:
                    record["requirement_override"] = text
                updated = await scheduler.manager.update_job(
                    job.job_id, description=text
                )
                if updated is None:
                    raise TimetableError("任务已不存在，请刷新后重试。")
                record.update(binding_id=binding["id"], rendered_requirement=text)
                self.plugin.store.save_task(key, record)
            await scheduler.sync()
            return json_response(
                {
                    "message": "已恢复全局模板。"
                    if reset
                    else "此次提醒的任务需求已更新。"
                }
            )
        if body.get("operation") == "sync":
            await scheduler.sync(reset_deleted=body.get("reset_deleted") is True)
            return json_response({"message": "未来任务已同步。"})
        if body.get("operation") == "toggle":
            if not isinstance(body.get("enabled"), bool):
                raise TimetableError("任务开关必须为布尔值。")
            async with scheduler.lock:
                jobs = (
                    await scheduler.manager.list_jobs() if scheduler.available else []
                )
                job = next(
                    (
                        j
                        for j in jobs
                        if j.job_id == body.get("id") and scheduler.owned(j)
                    ),
                    None,
                )
                if job is None:
                    raise TimetableError("课笺任务不存在，请刷新列表。")
                await scheduler.manager.update_job(job.job_id, enabled=body["enabled"])
            await scheduler.sync()
            return json_response(
                {"message": "任务已开启。" if body["enabled"] else "此次提醒已暂停。"}
            )
        raise TimetableError("不支持的任务操作。")

    async def export(self):
        from icalendar import Alarm, Calendar, Event

        binding = self.binding(request.query.get("binding"))
        schedule = binding["schedule"]
        first = iso_date(schedule.get("first_date", schedule["monday"]))
        last = (
            iso_date(schedule["last_date"])
            if schedule.get("kind") == "calendar"
            else first + timedelta(weeks=60)
        )
        if binding["overrides"]:
            dates = [iso_date(day) for day in binding["overrides"]]
            first, last = min(first, *dates), max(last, *dates)
        if (last - first).days > 550:
            raise TimetableError(
                "课表与日期覆盖跨度超过 550 天，无法导出；请先清理旧日期覆盖。"
            )
        calendar = Calendar()
        calendar.add("prodid", "-//ClassNote//AstrBot//ZH")
        calendar.add("version", "2.0")
        calendar.add("x-wr-calname", schedule["name"])
        calendar.add("x-wr-timezone", binding["timezone"])
        count = 0
        for delta in range((last - first).days + 1):
            for course in occurrences(
                schedule,
                first + timedelta(days=delta),
                binding["timezone"],
                binding["overrides"],
            ):
                count += 1
                if count > 2000:
                    raise TimetableError(
                        "展开超过 2000 课次，无法导出为可重新导入的单份日历。"
                    )
                event = Event()
                event.add("uid", f"{delivery_key(binding['id'], course, 0)}@classnote")
                event.add("dtstamp", datetime.now(timezone.utc))
                event.add("summary", course["name"])
                event.add("location", course["room"])
                event.add("dtstart", course["begin"].astimezone(timezone.utc))
                event.add("dtend", course["finish"].astimezone(timezone.utc))
                for lead in reminder_leads(binding, course):
                    alarm = Alarm()
                    alarm.add("action", "DISPLAY")
                    alarm.add("description", "上课提醒")
                    alarm.add("trigger", -timedelta(minutes=lead))
                    event.add_component(alarm)
                calendar.add_component(event)
        if count == 0:
            raise TimetableError("课表没有可导出的课程，请核对日期和停课覆盖。")
        content = calendar.to_ical()
        if len(content) > MAX_BYTES:
            raise TimetableError("导出的日历超过 2 MiB，请减少课次后重试。")
        return stream_response(
            iter([content]),
            content_type="text/calendar",
            headers={
                "Content-Disposition": 'attachment; filename="classnote-calendar.ics"'
            },
        )
