"""Offline behavioral tests. Run from the parent directory with unittest discovery."""

import asyncio
import importlib
import json
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from astrbot_plugin_classnote.core import (
    TimetableError,
    delivery_key,
    due_reminders,
    extract_share_key,
    next_reminders,
    occurrences,
    parse_timetable,
    teaching_week,
    unwrap_api,
)
from astrbot_plugin_classnote.persona import current_persona, generate_opener
from astrbot_plugin_classnote.store import Store
from astrbot_plugin_classnote.wakeup import import_share


def raw_payload(kind=0):
    return {
        "tableInfo": {"startDate": "2026-09-07", "tableName": "2026秋"},
        "timeList": [
            {"node": 1, "startTime": "08:00", "endTime": "08:45"},
            {"node": 2, "startTime": "08:55", "endTime": "09:40"},
        ],
        "courseInfos": [{"id": 1, "courseName": "大学化学", "teacher": "李老师"}],
        "courseDetails": [
            {
                "id": 1,
                "day": 1,
                "startNode": 1,
                "step": 2,
                "startWeek": 1,
                "endWeek": 16,
                "type": kind,
                "room": "逸夫楼A201",
            }
        ],
    }


def binding(kind=0):
    return {
        "id": "binding-one",
        "umo": "qq1:FriendMessage:12345",
        "sender": "12345",
        "nickname": "CREAM",
        "platform": "aiocqhttp",
        "group": False,
        "timezone": "Asia/Shanghai",
        "lead_minutes": [20],
        "enabled": True,
        "overrides": {},
        "schedule": parse_timetable(raw_payload(kind)),
        "revision": 1,
    }


class CalendarTests(unittest.TestCase):
    def test_odd_even_are_teaching_weeks(self):
        odd, even = binding(1), binding(2)
        self.assertEqual(
            len(occurrences(odd["schedule"], date(2026, 9, 7), "Asia/Shanghai")), 1
        )
        self.assertEqual(
            occurrences(odd["schedule"], date(2026, 9, 14), "Asia/Shanghai"), []
        )
        self.assertEqual(
            len(occurrences(even["schedule"], date(2026, 9, 14), "Asia/Shanghai")), 1
        )
        self.assertEqual(teaching_week(odd["schedule"], date(2026, 9, 14)), 2)

    def test_irregular_weeks_override_ranges(self):
        raw = raw_payload()
        raw["courseDetails"][0]["weeks"] = [1, 4, 9]
        s = parse_timetable(raw)
        self.assertEqual(s["courses"][0]["weeks"], [1, 4, 9])
        self.assertEqual(occurrences(s, date(2026, 9, 14), "Asia/Shanghai"), [])

    def test_large_class_one_occurrence(self):
        c = occurrences(binding()["schedule"], date(2026, 9, 7), "Asia/Shanghai")
        self.assertEqual([(x["start"], x["end"]) for x in c], [("08:00", "09:40")])

    def test_gaps_weekday_and_term_end(self):
        s = binding()["schedule"]
        for on in (date(2026, 9, 6), date(2026, 9, 8), date(2027, 1, 4)):
            self.assertEqual(occurrences(s, on, "Asia/Shanghai"), [])

    def test_real_times_utc_server(self):
        b = binding()
        jobs = due_reminders(b, datetime(2026, 9, 6, 23, 40, 10, tzinfo=timezone.utc))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["begin"].hour, 8)

    def test_cross_midnight_lead(self):
        b = binding()
        b["lead_minutes"] = [600]
        jobs = due_reminders(
            b, datetime(2026, 9, 6, 22, 0, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["begin"].date(), date(2026, 9, 7))

    def test_missed_and_started_are_skipped(self):
        b = binding()
        zone = ZoneInfo("Asia/Shanghai")
        self.assertEqual(due_reminders(b, datetime(2026, 9, 7, 7, 46, tzinfo=zone)), [])
        self.assertEqual(due_reminders(b, datetime(2026, 9, 7, 8, 0, tzinfo=zone)), [])

    def test_cancellation_and_reschedule_use_source_week(self):
        s = binding(1)["schedule"]
        self.assertEqual(
            occurrences(s, date(2026, 9, 7), "Asia/Shanghai", {"2026-09-07": None}), []
        )
        # Week 2 Saturday may borrow week 1 Monday despite week 2 being even.
        moved = occurrences(
            s, date(2026, 9, 19), "Asia/Shanghai", {"2026-09-19": "2026-09-07"}
        )
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]["week"], 1)
        self.assertEqual(moved[0]["begin"].date(), date(2026, 9, 19))

    def test_stable_key_after_reimport(self):
        b = binding()
        item = occurrences(b["schedule"], date(2026, 9, 7), "Asia/Shanghai")[0]
        raw = raw_payload()
        raw["courseDetails"][0]["endWeek"] = 20
        new = occurrences(parse_timetable(raw), date(2026, 9, 7), "Asia/Shanghai")[0]
        self.assertEqual(
            delivery_key(b["id"], item, 20), delivery_key(b["id"], new, 20)
        )

    def test_next_changes_with_week(self):
        b = binding(1)
        jobs = next_reminders(
            b, datetime(2026, 9, 8, tzinfo=ZoneInfo("Asia/Shanghai")), 1
        )
        self.assertEqual(jobs[0]["begin"].date(), date(2026, 9, 21))

    def test_non_monday_normalization_visible(self):
        raw = raw_payload()
        raw["tableInfo"]["startDate"] = "2026-09-09"
        s = parse_timetable(raw)
        self.assertEqual(s["monday"], "2026-09-07")
        self.assertTrue(s["warnings"])


class ImportTests(unittest.TestCase):
    def test_legacy_five_lines_and_api_envelope(self):
        r = raw_payload()
        text = "\n".join(
            json.dumps(x, ensure_ascii=False)
            for x in [
                1,
                r["timeList"],
                r["tableInfo"],
                r["courseInfos"],
                r["courseDetails"],
            ]
        )
        s = parse_timetable({"status": 1, "data": text})
        self.assertEqual(s["courses"][0]["name"], "大学化学")

    def test_flat_object(self):
        r = raw_payload()
        flat = {
            **r["tableInfo"],
            "timeList": r["timeList"],
            "courseList": [
                {
                    **r["courseInfos"][0],
                    **r["courseDetails"][0],
                    "stepWeekList": [2, 4, 6],
                }
            ],
        }
        self.assertEqual(parse_timetable(flat)["courses"][0]["weeks"], [2, 4, 6])

    def test_key_text_and_url(self):
        key = "a" * 32
        self.assertEqual(extract_share_key(f"这是分享。分享口令为「{key}」"), key)
        self.assertEqual(
            extract_share_key(f"https://i.wakeup.fun/share_schedule/get?key={key}"), key
        )
        self.assertEqual(extract_share_key(key), key)
        with self.assertRaises(TimetableError):
            extract_share_key(f"http://127.0.0.1/?key={key}")

    def test_unsupported_protocol_is_explicit(self):
        with self.assertRaisesRegex(TimetableError, "协议不兼容"):
            unwrap_api({"status": 5000004, "message": "当前版本过低"})

    def test_missing_dates_weeks_slots_and_own_time_rejected(self):
        for mutation in (
            lambda r: r["tableInfo"].pop("startDate"),
            lambda r: r["timeList"].pop(),
            lambda r: r["courseDetails"][0].pop("startWeek"),
            lambda r: r["courseDetails"][0].update(ownTime=True),
            lambda r: r["courseDetails"][0].update(type=7),
        ):
            r = raw_payload()
            mutation(r)
            with self.assertRaises(TimetableError):
                parse_timetable(r)


class StoreTests(unittest.TestCase):
    def test_persistence_revisions_and_delivery_claims(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "test.sqlite3"
            store = Store(path)
            b = store.save(binding())
            self.assertEqual(b["revision"], 1)
            self.assertTrue(store.claim("k", b["id"], 1000))
            self.assertFalse(store.claim("k", b["id"], 1001))
            store.finish("k", 1002)
            store.close()
            store = Store(path)
            self.assertEqual(store.get(b["id"])["schedule"], b["schedule"])
            self.assertFalse(store.claim("k", b["id"], 1200))
            self.assertEqual(store.save(b)["revision"], 2)
            store.delete(b["id"])
            self.assertEqual(store.all(), [])
            store.close()

    def test_retry_backoff_limit_and_stale_lease(self):
        s = Store(":memory:")
        self.assertTrue(s.claim("k", "b", 1000))
        self.assertFalse(s.claim("k", "b", 1100))
        self.assertTrue(s.claim("k", "b", 1121))
        s.finish("k", 1122, "failed", "TimeoutError")
        self.assertFalse(s.claim("k", "b", 1140))
        self.assertTrue(s.claim("k", "b", 1153))
        s.finish("k", 1154, "failed")
        self.assertFalse(s.claim("k", "b", 1250))
        s.close()


class PersonalityTests(unittest.IsolatedAsyncioTestCase):
    def make_context(self):
        context = types.SimpleNamespace(
            conversation_manager=types.SimpleNamespace(
                get_curr_conversation_id=AsyncMock(return_value="cid"),
                get_conversation=AsyncMock(
                    return_value=types.SimpleNamespace(persona_id="Monika")
                ),
            ),
            persona_manager=types.SimpleNamespace(
                get_persona=AsyncMock(
                    return_value=types.SimpleNamespace(
                        system_prompt="你是Monika，语气温柔，不使用emoji。",
                        begin_dialogs=[],
                    )
                ),
                get_default_persona_v3=AsyncMock(return_value={"prompt": "default"}),
            ),
            get_current_chat_provider_id=AsyncMock(return_value="session-model"),
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(
                    completion_text="呐，该准备去上课了~"
                )
            ),
            get_config=lambda umo: {},
        )
        return context

    async def test_live_session_persona_and_model_injected(self):
        c = self.make_context()
        text = await generate_opener(c, binding(), "fallback")
        self.assertIn("呐", text)
        kwargs = c.llm_generate.call_args.kwargs
        self.assertIn("Monika", kwargs["system_prompt"])
        self.assertEqual(kwargs["chat_provider_id"], "session-model")
        self.assertNotIn("contexts", kwargs)
        c.persona_manager.get_default_persona_v3.assert_not_awaited()

    async def test_task_requirement_guides_generation_without_removing_persona(self):
        c = self.make_context()
        await generate_opener(
            c, binding(), "fallback", requirement="请使用轻松俏皮的语气，提醒带上水杯。"
        )
        prompt = c.llm_generate.call_args.kwargs["system_prompt"]
        self.assertIn("Monika", prompt)
        self.assertIn("提醒带上水杯", prompt)
        self.assertIn("不要写课程名", prompt)

    async def test_session_override_resolver_has_priority(self):
        c = self.make_context()
        c.persona_manager.resolve_selected_persona = AsyncMock(
            return_value=("active", {"prompt": "ACTIVE"}, None, False)
        )
        await generate_opener(c, binding(), "fallback")
        self.assertIn("ACTIVE", c.llm_generate.call_args.kwargs["system_prompt"])
        c.persona_manager.get_persona.assert_not_awaited()

    async def test_persona_disabled_respects_none_marker(self):
        c = self.make_context()
        c.conversation_manager.get_conversation.return_value.persona_id = "[%None]"
        pid, p = await current_persona(c, binding())
        self.assertEqual((pid, p), ("[%None]", {}))

    async def test_wrong_fact_and_long_output_rejected(self):
        c = self.make_context()
        for text in ("明天没课。", "还有20分钟。", "x" * 101):
            c.llm_generate.return_value.completion_text = text
            with self.assertRaises(ValueError):
                await generate_opener(c, binding(), "fallback")

    async def test_model_disabled_costs_no_tokens(self):
        c = self.make_context()
        self.assertEqual(
            await generate_opener(c, binding(), "固定风格", False), "固定风格"
        )
        c.llm_generate.assert_not_awaited()


class NetworkContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_host_no_redirect_and_realistic_response(self):
        body = json.dumps({"status": 1, "data": raw_payload()}).encode()

        class Content:
            async def iter_chunked(self, size):
                yield body[:20]
                yield body[20:]

        class Response:
            status = 200
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

        class Session:
            def get(self, url, **kwargs):
                self.url, self.kwargs = url, kwargs
                return Response()

        session = Session()
        result = await import_share(session, "a" * 32)
        self.assertEqual(len(result["courses"]), 1)
        self.assertEqual(session.url, "https://i.wakeup.fun/share_schedule/get")
        self.assertFalse(session.kwargs["allow_redirects"])


def load_plugin_with_sdk_stubs():
    """Offline doubles are limited to public SDK contracts; not a real AstrBot load."""
    modules = {}
    for name in (
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.message_components",
        "astrbot.api.star",
        "astrbot.core",
        "astrbot.core.utils",
        "astrbot.core.utils.astrbot_path",
    ):
        modules[name] = types.ModuleType(name)

    class Star:
        def __init__(self, context):
            self.context = context

    class Chain:
        def __init__(self):
            self.chain = []

    class Plain:
        def __init__(self, text):
            self.text = text

    class At:
        def __init__(self, qq):
            self.qq = qq

    class File:
        def __init__(self, name, file=""):
            self.name, self.location = name, file

        async def get_file(self, allow_return_url=False):
            return self.location

    class Reply:
        def __init__(self, chain=None):
            self.chain = chain or []

    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = types.SimpleNamespace(
        error=lambda *_: None, warning=lambda *_: None
    )
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].MessageChain = Chain
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        command=lambda _: lambda f: f,
        event_message_type=lambda _: lambda f: f,
        EventMessageType=types.SimpleNamespace(ALL="all"),
    )
    modules["astrbot.api.message_components"].At = At
    modules["astrbot.api.message_components"].Plain = Plain
    modules["astrbot.api.message_components"].File = File
    modules["astrbot.api.message_components"].Reply = Reply
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].register = lambda *_: lambda c: c
    modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path = lambda: (
        "/tmp/unused-in-test"
    )
    with patch.dict(sys.modules, modules):
        return importlib.import_module("astrbot_plugin_classnote.main")


class PluginContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.main = load_plugin_with_sdk_stubs()
        self.context = PersonalityTests().make_context()
        self.context.send_message = AsyncMock(return_value=True)
        self.plugin = self.main.ClassNote(self.context, {"use_llm_persona": False})
        self.plugin.store = Store(":memory:")
        self.b = self.plugin.store.save(binding())

    async def asyncTearDown(self):
        self.plugin.store.close()

    def upcoming(self):
        item = occurrences(self.b["schedule"], date(2026, 9, 7), "Asia/Shanghai")[0]
        item["fire"] = item["begin"] - timedelta(minutes=20)
        return item

    async def test_send_uses_exact_umo_and_facts(self):
        self.assertTrue(await self.plugin._send(self.b, self.upcoming(), preview=True))
        args = self.context.send_message.call_args.args
        self.assertEqual(args[0], self.b["umo"])
        text = args[1].chain[0].text
        for expected in ("大学化学", "08:00", "09:40", "逸夫楼A201", "【测试提醒】"):
            self.assertIn(expected, text)

    async def test_platform_false_is_failure(self):
        self.context.send_message.return_value = False
        with self.assertRaises(RuntimeError):
            await self.plugin._send(self.b, self.upcoming(), preview=True)

    async def test_revision_change_prevents_stale_send(self):
        self.plugin.store.save({**self.b, "enabled": False})
        self.assertFalse(await self.plugin._send(self.b, self.upcoming(), preview=True))
        self.context.send_message.assert_not_awaited()

    async def test_model_failure_still_delivers_facts(self):
        self.plugin.config["use_llm_persona"] = True
        self.context.llm_generate.side_effect = RuntimeError("offline")
        await self.plugin._send(self.b, self.upcoming(), preview=True)
        self.assertIn(
            "大学化学", self.context.send_message.call_args.args[1].chain[0].text
        )

    async def test_delete_reimport_prevents_old_inflight_send(self):
        self.plugin.store.delete(self.b["id"])
        fresh = self.plugin.store.save(binding())
        self.assertEqual(fresh["revision"], self.b["revision"])
        self.assertNotEqual(fresh["epoch"], self.b["epoch"])
        self.assertFalse(await self.plugin._send(self.b, self.upcoming(), preview=True))
        self.context.send_message.assert_not_awaited()

    async def test_background_loop_deduplicates_across_ticks(self):
        self.plugin.config["scheduler_mode"] = "polling"

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                instant = cls(2026, 9, 6, 23, 40, 10, tzinfo=timezone.utc)
                return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

        ticks = 0

        async def advance(_):
            nonlocal ticks
            ticks += 1
            if ticks >= 2:
                raise asyncio.CancelledError

        with (
            patch.object(self.main, "datetime", FixedDateTime),
            patch.object(self.main.asyncio, "sleep", advance),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin._run()
        self.assertEqual(self.context.send_message.await_count, 1)
        self.assertEqual(self.plugin.store.latest(self.b["id"])[0], "sent")

    async def test_group_at_is_scoped_to_owner(self):
        self.b = self.plugin.store.save({**self.b, "group": True})
        await self.plugin._send(self.b, self.upcoming(), preview=True)
        chain = self.context.send_message.call_args.args[1].chain
        self.assertEqual(chain[0].qq, self.b["sender"])

    async def test_invalid_import_does_not_replace_existing(self):
        event = types.SimpleNamespace(
            unified_msg_origin=self.b["umo"],
            get_sender_id=lambda: self.b["sender"],
        )
        with self.assertRaises(TimetableError):
            await self.plugin._dispatch(event, "导入文本", '{"not-a-timetable":true}')
        self.assertEqual(
            self.plugin.store.get(self.b["id"])["revision"], self.b["revision"]
        )


if __name__ == "__main__":
    unittest.main()
