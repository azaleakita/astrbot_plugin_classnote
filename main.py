"""AstrBot plugin entry point. Commands only manage the caller's current-session timetable."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, File, Image, Plain, Reply
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .attachments import attachment_bytes
from .core import (
    TimetableError,
    bounded_int,
    clean,
    current_or_next_course,
    digest,
    due_reminders,
    iso_date,
    next_reminders,
    occurrences,
    parse_timetable,
    teaching_week,
)
from .course_card import card_settings, course_label, render_course_card
from .future_tasks import FutureTasks
from .ics import parse_calendar
from .persona import generate_opener
from .store import Store, migrate_legacy_database
from .task_text import DEFAULT_REQUIREMENT, validate_requirement
from .wakeup import import_share

HELP = """课笺 · ClassNote
/课表 导入日历（随后发送WakeUp导出的.ics文件，也可直接发送文件）
/课表 导入 <WakeUp分享文本、口令或含key的链接>
/课表 导入文本 <WakeUp备份原始文本或JSON>
/课表 状态 / 今日 / 明日 / 本周 / 下次
/课表 图片（当前或下一节课的单课卡片）
/课表 提前 20 或 /课表 提前 30,10
/课表 提前 日历（恢复使用ICS自带的提前提醒）
/课表 提前 默认（跟随插件设置）
/课表 开学 2026-09-07（第一教学周周一）
/课表 停课 2026-10-01（当天全部课程）
/课表 调课 2026-10-10 2026-10-05
  表示10月10日改上10月5日的课程；原日期不自动停课
/课表 恢复 2026-10-10（移除当天覆盖）
/课表 暂停 / 开启 / 测试 / 删除
提醒投递到导入时的当前会话；群聊需先在插件设置开放。
WakeUp导入是课表快照，App后续修改需重新分享导入。"""


@register(
    "astrbot_plugin_classnote",
    "CREAM",
    "课笺：WakeUp日历与分享课表提醒，沿用当前会话人格",
    "0.5.1",
)
class ClassNote(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.store = None
        self.http = None
        self.task = None
        self.send_lock = asyncio.Lock()
        self.delivery_semaphore = asyncio.Semaphore(4)
        self.pending_imports = {}
        self.future_tasks = FutureTasks(self)
        self.dashboard = None
        self.dashboard_error = ""

    def _setup_dashboard(self):
        if not callable(getattr(self.context, "register_web_api", None)):
            self.dashboard_error = (
                "当前 AstrBot 不支持插件页面接口；QQ 课表和提醒仍可用。"
            )
            return
        try:
            from .webui import Dashboard

            self.dashboard = Dashboard(self)
            self.dashboard.register()
        except Exception as exc:
            if self.dashboard:
                self.dashboard.close()
            self.dashboard = None
            self.dashboard_error = "管理页未启用，请检查日志；QQ 课表和提醒仍可用。"
            logger.warning(
                f"[classnote] dashboard unavailable: {type(exc).__name__}: {exc}"
            )

    async def initialize(self):
        zone = str(self.config.get("timezone", "Asia/Shanghai"))
        ZoneInfo(zone)  # Invalid configuration must fail visibly at load time.
        self._lead(self.config.get("default_lead_minutes", [20]))
        card_settings(self.config)
        validate_requirement(
            self.config.get("task_requirement_template", DEFAULT_REQUIREMENT),
            template=True,
        )
        bounded_int(self.config.get("catchup_seconds", 300), 30, 900, "补发窗口")
        if self.config.get("scheduler_mode", "future_tasks") not in (
            "future_tasks",
            "polling",
        ):
            raise TimetableError("scheduler_mode 必须为 future_tasks 或 polling。")
        database = migrate_legacy_database(get_astrbot_data_path())
        self.store = Store(database)
        self.http = aiohttp.ClientSession()
        self._setup_dashboard()
        await self._sync_future(rebind=True)
        self.task = asyncio.create_task(self._run(), name="classnote-reminder-loop")

    async def terminate(self):
        if self.dashboard:
            self.dashboard.close()
            self.dashboard = None
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.http:
            await self.http.close()
        if self.store:
            try:
                await self.future_tasks.close()
            except Exception as exc:
                logger.warning(f"[classnote] task cleanup failed: {type(exc).__name__}")
            self.store.close()
            self.store = None

    async def _sync_future(self, **options):
        if not self.store:
            return
        try:
            await self.future_tasks.sync(**options)
        except Exception as exc:
            logger.warning(f"[classnote] future task sync failed: {type(exc).__name__}")

    async def _apply_binding_defaults(self):
        """Refresh inherited settings, preserving explicit personal advance times."""
        zone = str(self.config.get("timezone", "Asia/Shanghai"))
        ZoneInfo(zone)
        leads = self._lead(self.config.get("default_lead_minutes", [20]))
        async with self.send_lock:
            for binding in self.store.all():
                updated = {**binding, "timezone": zone}
                if binding["timezone"] != zone:
                    updated.setdefault("key_timezone", binding["timezone"])
                if not binding.get("custom_lead", False):
                    updated["lead_minutes"] = leads
                if updated != binding:
                    self.store.save(updated)

    def _binding_id(self, event):
        return digest([event.unified_msg_origin, str(event.get_sender_id())])

    def _get_binding(self, event):
        b = self.store.get(self._binding_id(event))
        if not b:
            raise TimetableError(
                "还没有导入当前会话的课表。发送 /课表 导入 加上分享文本即可。"
            )
        return b

    def _permission_error(self, event):
        if self.config.get("admins_only", True) and not event.is_admin():
            return "当前课表插件仅向 AstrBot 管理员开放。"
        if event.get_group_id() and not self.config.get("allow_group", False):
            return "当前仅开放私聊课表提醒，请私聊机器人导入。"
        return None

    def _calendar_files(self, event):
        files = []
        for component in event.get_messages():
            candidates = (
                component.chain or [] if isinstance(component, Reply) else [component]
            )
            files.extend(
                item
                for item in candidates
                if isinstance(item, File)
                and str(item.name or "").lower().endswith((".ics", ".ical"))
            )
        if len(files) > 1:
            raise TimetableError("请一次只发送一份日历，避免替换错课表。")
        return files

    async def _import_calendar_attachment(self, event, component):
        previous = self.store.get(self._binding_id(event)) or {}
        zone = previous.get(
            "timezone", str(self.config.get("timezone", "Asia/Shanghai"))
        )
        content = await attachment_bytes(component, self.http)
        schedule = await asyncio.to_thread(
            parse_calendar, content, zone, clean(component.name)
        )
        return await self._save_schedule(event, schedule)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def calendar_attachment(self, event: AstrMessageEvent):
        """A .ics attachment itself is the explicit import action; text commands use dispatch."""
        if self._permission_error(event) or not self.store or not self.http:
            return
        if re.search(r"(?:^|\s)[/!]?课表(?:\s|$)", event.get_message_str()):
            return
        try:
            files = self._calendar_files(event)
            if not files:
                return
            pending = self.pending_imports.get(self._binding_id(event), 0)
            if (
                not self.config.get("auto_import_ics", True)
                and pending < asyncio.get_running_loop().time()
            ):
                return
            event.stop_event()
            self.pending_imports.pop(self._binding_id(event), None)
            response = await self._import_calendar_attachment(event, files[0])
        except TimetableError as exc:
            response = str(exc)
        except Exception as exc:
            logger.warning(
                f"[classnote] attachment import failed: {type(exc).__name__}"
            )
            response = "日历导入未完成，旧课表仍保留。请检查文件格式及适配器日志。"
        yield event.plain_result(response)

    def _lead(self, value):
        parts = (
            value
            if isinstance(value, list)
            else re.split(r"[,，\s]+", str(value).strip())
        )
        result = sorted(
            {bounded_int(v, 1, 1440, "提前分钟数") for v in parts}, reverse=True
        )
        if not 1 <= len(result) <= 3:
            raise TimetableError("请设置1至3个提前时间，例如 30,10，范围1至1440分钟。")
        return result

    def _active(self, binding):
        return (
            bool(self.config.get("enabled", True))
            and binding["enabled"]
            and (not binding["group"] or bool(self.config.get("allow_group", False)))
        )

    @filter.command("课表")
    async def timetable(self, event: AstrMessageEvent):
        """导入课表、查询课程及设置提醒。"""
        event.stop_event()
        if error := self._permission_error(event):
            yield event.plain_result(error)
            return
        if not self.store or not self.http:
            yield event.plain_result("课表插件尚未初始化完成，请稍后重试。")
            return
        # Read the original message so multiline backup JSON keeps all newlines.
        raw = event.get_message_str().split("课表", 1)[-1].strip()
        pieces = raw.split(maxsplit=1)
        action, argument = (pieces + [""])[:2] if pieces else ("帮助", "")
        try:
            response = await self._dispatch(event, action, argument)
        except (TimetableError, ZoneInfoNotFoundError) as exc:
            response = str(exc)
        except Exception as exc:
            logger.error(f"[classnote] command failed: {type(exc).__name__}")
            response = "操作未完成，已有课表仍保留。请检查插件日志中的错误类型。"
        if isinstance(response, list):
            yield event.chain_result(response)
        else:
            yield event.plain_result(response)

    async def _dispatch(self, event, action, argument):
        if action in ("帮助", "help", ""):
            return HELP
        if action == "导入日历":
            if argument.lstrip("\ufeff").upper().startswith("BEGIN:VCALENDAR"):
                previous = self.store.get(self._binding_id(event)) or {}
                zone = previous.get(
                    "timezone", str(self.config.get("timezone", "Asia/Shanghai"))
                )
                schedule = await asyncio.to_thread(parse_calendar, argument, zone)
                return await self._save_schedule(event, schedule)
            files = self._calendar_files(event)
            if files:
                return await self._import_calendar_attachment(event, files[0])
            loop = asyncio.get_running_loop()
            now = loop.time()
            self.pending_imports = {
                key: expiry
                for key, expiry in self.pending_imports.items()
                if expiry > now
            }
            self.pending_imports[self._binding_id(event)] = now + 120
            return "请在2分钟内发送 WakeUp 导出的 .ics 日历文件。不要发送截图或文件在你电脑上的路径。"
        if action in ("导入", "导入文本"):
            if not argument:
                raise TimetableError(
                    "请在指令后粘贴分享文本，或使用 /课表 导入文本 粘贴备份内容。"
                )
            if len(argument.encode()) > 2 * 1024 * 1024:
                raise TimetableError("课表文本超过2MiB。")
            # Validate all data before replacing a previous timetable.
            schedule = (
                await import_share(self.http, argument)
                if action == "导入"
                else parse_timetable(argument)
            )
            return await self._save_schedule(event, schedule)
        if action == "删除":
            async with self.send_lock:
                self.store.delete(self._binding_id(event))
            await self._sync_future()
            return "已删除你在当前会话的课表及提醒记录。"
        binding = self._get_binding(event)
        now = datetime.now(ZoneInfo(binding["timezone"]))
        if action in ("图片", "卡片"):
            item = await asyncio.to_thread(current_or_next_course, binding, now)
            if not item:
                return "当前课表没有正在进行或尚未开始的课程。"
            try:
                data = await asyncio.to_thread(
                    render_course_card, dict(self.config), item, course_label(item, now)
                )
                return [Image.fromBytes(data)]
            except Exception as exc:
                logger.warning(
                    f"[classnote] card fallback: {type(exc).__name__}: {exc}"
                )
                return "课程图片暂时不可用，已改为文字：\n" + self._course_text(item)
        if action == "状态":
            state = self.store.latest(binding["id"])
            latest = (
                f"{state[0]}，尝试{state[1]}次，{state[2] or '无错误类型记录'}"
                if state
                else "暂无"
            )
            calendar = binding["schedule"].get("kind") == "calendar"
            date_info = (
                f"日历日期：{binding['schedule']['first_date']}至{binding['schedule']['last_date']}\n"
                if calendar
                else f"第{teaching_week(binding['schedule'], now.date())}教学周；第一周周一：{binding['schedule']['monday']}\n"
            )
            return (
                f"课表：{binding['schedule']['name']}\n"
                + date_info
                + f"提醒：{'开启' if self._active(binding) else '暂停或配置已关闭'}；"
                f"默认提前{binding['lead_minutes']}分钟；时区{binding['timezone']}\n"
                f"提醒来源：{'日历自带，缺失时使用默认值' if calendar and binding.get('respect_ics_alarms', True) and not binding.get('custom_lead', False) else '个人提前量' if binding.get('custom_lead', False) else '全局默认设置'}\n"
                f"最近投递：{latest}\n日期覆盖：{json.dumps(binding['overrides'], ensure_ascii=False)}"
                + f"\n调度：{'AstrBot 未来任务' if self.future_tasks.effective_mode == 'future_tasks' else '插件轮询'}"
                + (f"\n{self.future_tasks.error}" if self.future_tasks.error else "")
                + (f"\n{self.dashboard_error}" if self.dashboard_error else "")
            )
        if action in ("今日", "明日", "本周"):
            first = now.date() + timedelta(days=1 if action == "明日" else 0)
            if action == "本周":
                first -= timedelta(days=first.weekday())
            rows = []
            for offset in range(7 if action == "本周" else 1):
                on = first + timedelta(days=offset)
                for c in occurrences(
                    binding["schedule"], on, binding["timezone"], binding["overrides"]
                ):
                    rows.append(
                        f"{on:%m-%d} {c['start']}–{c['end']} {c['name']} · {c['room'] or '地点未填'}"
                    )
            return "\n".join(rows[:80]) if rows else "所查日期没有安排课程。"
        if action == "下次":
            upcoming = next_reminders(binding, now)
            if not upcoming:
                return "当前课表已没有未来提醒。请核对开学日期及有效周次。"
            prefix = (
                "" if self._active(binding) else "提醒目前暂停，以下仅为计划时间。\n"
            )
            return prefix + "\n".join(
                f"{c['fire']:%m-%d %H:%M} 提醒：{c['name']}（{c['begin']:%H:%M}上课）"
                for c in upcoming
            )
        if action == "测试":
            upcoming = next_reminders(binding, now, 1)
            if not upcoming:
                return "没有可预览的未来课程，请先核对课表。"
            if not await self._send(binding, upcoming[0], preview=True):
                return "测试期间课表发生变化，请重新发送 /课表 测试。"
            return "已通过主动消息接口发送测试提醒；此测试不会消耗正式提醒。"
        async with self.send_lock:
            # Refresh inside the lock, so concurrent commands cannot overwrite each other.
            binding = self._get_binding(event)
            if action == "提前":
                if argument.strip() == "默认":
                    binding["custom_lead"] = False
                    binding["respect_ics_alarms"] = False
                    binding["lead_minutes"] = self._lead(
                        self.config.get("default_lead_minutes", [20])
                    )
                elif argument.strip() == "日历":
                    if binding["schedule"].get("kind") != "calendar":
                        raise TimetableError("当前不是日历课表，请先导入 .ics 文件。")
                    binding["custom_lead"] = False
                    binding["respect_ics_alarms"] = True
                else:
                    binding["lead_minutes"] = self._lead(argument)
                    binding["custom_lead"] = True
            elif action in ("暂停", "开启"):
                binding["enabled"] = action == "开启"
            elif action == "开学":
                if binding["schedule"].get("kind") == "calendar":
                    raise TimetableError(
                        "日历按真实日期提醒，不通过开学日期平移；请用调课或重新导出日历。"
                    )
                start = iso_date(argument)
                if start.weekday() != 0:
                    raise TimetableError("请填写第一教学周的周一日期。")
                binding["schedule"]["monday"] = start.isoformat()
            elif action == "停课":
                binding["overrides"][iso_date(argument).isoformat()] = None
            elif action == "恢复":
                binding["overrides"].pop(iso_date(argument).isoformat(), None)
            elif action == "调课":
                dates = argument.split()
                if len(dates) != 2:
                    raise TimetableError(
                        "格式：/课表 调课 实际上课日期 参照原日期；原日期不自动停课。"
                    )
                actual, source = map(iso_date, dates)
                if actual == source:
                    raise TimetableError("实际日期与参照日期相同，无需调课。")
                binding["overrides"][actual.isoformat()] = source.isoformat()
            else:
                return HELP
            self.store.save(binding)
        await self._sync_future()
        return "已更新。发送 /课表 下次 或 /课表 本周 可核对新安排。"

    async def _save_schedule(self, event, schedule):
        await self._apply_binding_defaults()
        async with self.send_lock:
            previous = self.store.get(self._binding_id(event)) or {}
            binding = self.store.save(
                {
                    "id": self._binding_id(event),
                    "umo": event.unified_msg_origin,
                    "sender": str(event.get_sender_id()),
                    "nickname": clean(event.get_sender_name(), 40),
                    "platform": event.get_platform_name(),
                    "group": bool(event.get_group_id()),
                    "timezone": previous.get(
                        "timezone", str(self.config.get("timezone", "Asia/Shanghai"))
                    ),
                    "lead_minutes": previous.get(
                        "lead_minutes",
                        self._lead(self.config.get("default_lead_minutes", [20])),
                    ),
                    "key_timezone": previous.get(
                        "key_timezone",
                        previous.get(
                            "timezone",
                            str(self.config.get("timezone", "Asia/Shanghai")),
                        ),
                    ),
                    # Legacy bindings have no flag: treat their configured advance time as deliberate.
                    "custom_lead": previous.get("custom_lead", bool(previous)),
                    "respect_ics_alarms": previous.get("respect_ics_alarms", True),
                    "enabled": previous.get("enabled", True),
                    "schedule": schedule,
                    "overrides": previous.get("overrides", {}),
                }
            )
        calendar = schedule.get("kind") == "calendar"
        date_info = (
            f"日历范围：{schedule['first_date']}至{schedule['last_date']}；按真实日期提醒。"
            if calendar
            else f"第一周周一：{schedule['monday']}。"
        )
        unit = "课次" if calendar else "条排课"
        alarm = (
            "优先使用日历自带提前提醒，可用 /课表 提前 覆盖。"
            if calendar
            and schedule.get("has_alarms")
            and binding.get("respect_ics_alarms", True)
            and not binding["custom_lead"]
            else f"提前{','.join(map(str, binding['lead_minutes']))}分钟提醒。"
        )
        notice = "\n" + "\n".join(schedule["warnings"]) if schedule["warnings"] else ""
        await self._sync_future()
        return (
            f"课笺已收好《{schedule['name']}》，共{len(schedule['courses'])}{unit}。\n"
            f"{date_info}\n时区：{binding['timezone']}；当前{'开启' if binding['enabled'] else '暂停'}。\n"
            f"{alarm}\n保留{len(binding['overrides'])}条日期覆盖。\n"
            "发送 /课表 下次 核对，或 /课表 测试 检查主动投递与人格。" + notice
        )

    @staticmethod
    def _course_text(item):
        return (
            f"{item['name']}\n"
            f"{item['begin']:%m月%d日 %H:%M}–"
            + item["finish"].strftime(
                "%m月%d日 %H:%M"
                if item["finish"].date() != item["begin"].date()
                else "%H:%M"
            )
            + f" · {item['room'] or '地点未填写'}"
        )

    async def _send(self, binding, item, preview=False):
        settings = (
            self.config.get("use_llm_persona", True),
            self.config.get("fallback_opener"),
            self.config.get("task_requirement_template"),
            self.config.get("reminder_format", "image"),
            self.config.get("card_title", "课笺"),
        )
        fallback = clean(
            self.config.get("fallback_opener", "该准备去上课了，记得带齐需要的东西。"),
            100,
        )
        try:
            # Bound the whole personality/provider pipeline, not only model generation.
            async with asyncio.timeout(10):
                opener = await generate_opener(
                    self.context,
                    binding,
                    fallback,
                    bool(self.config.get("use_llm_persona", True)),
                    requirement=await self.future_tasks.requirement(binding, item),
                )
        except Exception as exc:
            logger.warning(f"[classnote] persona fallback: {type(exc).__name__}")
            opener = fallback
        mode, _ = card_settings(self.config)
        card = None
        if mode != "text":
            try:
                data = await asyncio.to_thread(
                    render_course_card,
                    dict(self.config),
                    item,
                    "测试提醒" if preview else "课程提醒",
                )
                card = Image.fromBytes(data)
            except Exception as exc:
                logger.warning(
                    f"[classnote] card fallback: {type(exc).__name__}: {exc}"
                )
        # Re-check after the await: pause/import/delete/override may have happened meanwhile.
        async with self.send_lock:
            if settings != (
                self.config.get("use_llm_persona", True),
                self.config.get("fallback_opener"),
                self.config.get("task_requirement_template"),
                self.config.get("reminder_format", "image"),
                self.config.get("card_title", "课笺"),
            ):
                return False
            live = self.store.get(binding["id"])
            if (
                not live
                or live["revision"] != binding["revision"]
                or live.get("epoch") != binding.get("epoch")
            ):
                return False
            if not preview:
                now = datetime.now(timezone.utc)
                if not self._active(live) or now >= item["begin"]:
                    return False
                grace = bounded_int(
                    self.config.get("catchup_seconds", 300), 30, 900, "补发窗口"
                )
                if (now - item["fire"]).total_seconds() > grace:
                    return False
            content = ("【测试提醒】\n" if preview else "") + opener
            if mode in ("text", "both") or card is None:
                content += "\n" + self._course_text(item)
            chain = MessageChain()
            if binding["group"]:
                chain.chain.append(At(qq=binding["sender"]))
            chain.chain.append(Plain(content))
            if card is not None:
                chain.chain.append(card)
            # send_message returns False if it cannot find the destination platform.
            async with asyncio.timeout(20):
                delivered = await self.context.send_message(binding["umo"], chain)
            if delivered is False:
                raise RuntimeError("destination_platform_unavailable")
        return True

    async def _deliver(self, binding, item):
        now = datetime.now(timezone.utc)
        grace = bounded_int(
            self.config.get("catchup_seconds", 300), 30, 900, "补发窗口"
        )
        if (
            now < item["fire"]
            or now >= item["begin"]
            or (now - item["fire"]).total_seconds() > grace
        ):
            return False
        detail = {
            "name": item["name"],
            "fire": item["fire"].isoformat(),
            "begin": item["begin"].isoformat(),
            "room": item["room"],
        }
        if not self.store.claim(item["key"], binding["id"], now.timestamp(), detail):
            return False
        try:
            sent = await self._send(binding, item)
            self.store.finish(
                item["key"], datetime.now().timestamp(), "sent" if sent else "failed"
            )
            return sent
        except Exception as exc:
            self.store.finish(
                item["key"], datetime.now().timestamp(), "failed", type(exc).__name__
            )
            logger.warning(f"[classnote] delivery failed: {type(exc).__name__}")
            return False

    async def _run(self):
        last_prune = 0.0
        last_sync = 0.0
        while True:
            try:
                now = datetime.now(timezone.utc)
                stamp = now.timestamp()
                await self._apply_binding_defaults()
                grace = bounded_int(
                    self.config.get("catchup_seconds", 300), 30, 900, "补发窗口"
                )
                bindings = self.store.all()
                semaphore = asyncio.Semaphore(4)

                async def process(binding, item):
                    async with semaphore:
                        await self._deliver(binding, item)

                work = [
                    process(b, item)
                    for b in bindings
                    if self._active(b) and self.future_tasks.effective_mode == "polling"
                    for item in due_reminders(b, now, grace)
                ]
                if work:
                    await asyncio.gather(*work)
                if stamp - last_sync >= 30:
                    await self._sync_future()
                    last_sync = stamp
                if stamp - last_prune > 86400:
                    self.store.prune(stamp)
                    last_prune = stamp
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[classnote] scheduler error: {type(exc).__name__}")
            await asyncio.sleep(10)
