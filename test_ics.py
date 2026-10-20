"""Behavioral coverage for WakeUp-style exports, recurrence exceptions and QQ files."""

import tempfile
import types
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot_plugin_classnote.attachments import attachment_bytes
from astrbot_plugin_classnote.core import (
    TimetableError,
    due_reminders,
    next_reminders,
    occurrences,
)
from astrbot_plugin_classnote.ics import MAX_BYTES, parse_calendar
from astrbot_plugin_classnote.store import Store, migrate_legacy_database
from astrbot_plugin_classnote.tests.test_reminder import (
    binding,
    load_plugin_with_sdk_stubs,
)


def calendar(events, header=""):
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//ClassNote//Tests//ZH\r\n{header}{events}END:VCALENDAR\r\n"


def event(
    uid="course-one",
    start="DTSTART;TZID=Asia/Shanghai:20261019T080000",
    end="DTEND;TZID=Asia/Shanghai:20261019T094000",
    extra="",
    name="大学化学",
):
    return f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20261001T000000Z\r\nSUMMARY:{name}\r\nLOCATION:逸夫楼A201\r\n{start}\r\n{end}\r\n{extra}END:VEVENT\r\n"


ALARM = "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:上课提醒\r\nTRIGGER:-PT25M\r\nEND:VALARM\r\n"


def ics_binding(text):
    b = binding()
    b["schedule"] = parse_calendar(text)
    return b


class CalendarImportTests(unittest.TestCase):
    def test_wakeup_like_single_instance(self):
        s = parse_calendar(calendar(event(extra=ALARM)))
        self.assertEqual(s["kind"], "calendar")
        self.assertEqual(s["first_date"], "2026-10-19")
        self.assertEqual(s["courses"][0]["alarm_minutes"], [25])
        self.assertEqual(s["courses"][0]["room"], "逸夫楼A201")

    def test_biweekly_rule_preserves_irregular_dates(self):
        s = parse_calendar(
            calendar(event(extra="RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=4\r\n"))
        )
        self.assertEqual(
            list(s["dates"]), ["2026-10-19", "2026-11-02", "2026-11-16", "2026-11-30"]
        )

    def test_rdate_and_exdate(self):
        text = calendar(
            event(
                extra=(
                    "RRULE:FREQ=WEEKLY;COUNT=3\r\n"
                    "EXDATE;TZID=Asia/Shanghai:20261026T080000\r\n"
                    "RDATE;TZID=Asia/Shanghai:20261024T080000\r\n"
                )
            )
        )
        self.assertEqual(
            list(parse_calendar(text)["dates"]),
            ["2026-10-19", "2026-10-24", "2026-11-02"],
        )

    def test_recurrence_override_moves_one_class(self):
        original = event(extra="RRULE:FREQ=WEEKLY;COUNT=3\r\n")
        moved = event(
            start="DTSTART;TZID=Asia/Shanghai:20261027T100000",
            end="DTEND;TZID=Asia/Shanghai:20261027T114000",
            extra="RECURRENCE-ID;TZID=Asia/Shanghai:20261026T080000\r\n",
            name="大学化学补课",
        )
        s = parse_calendar(calendar(original + moved))
        self.assertNotIn("2026-10-26", s["dates"])
        self.assertIn("2026-10-27", s["dates"])
        self.assertEqual(s["courses"][1]["name"], "大学化学补课")

    def test_cancelled_recurrence_removed(self):
        original = event(extra="RRULE:FREQ=WEEKLY;COUNT=3\r\n")
        cancelled = event(
            start="DTSTART;TZID=Asia/Shanghai:20261026T080000",
            end="DTEND;TZID=Asia/Shanghai:20261026T094000",
            extra="RECURRENCE-ID;TZID=Asia/Shanghai:20261026T080000\r\nSTATUS:CANCELLED\r\n",
        )
        self.assertEqual(
            list(parse_calendar(calendar(original + cancelled))["dates"]),
            ["2026-10-19", "2026-11-02"],
        )

    def test_utc_to_shanghai(self):
        s = parse_calendar(
            calendar(
                event(start="DTSTART:20261019T000000Z", end="DTEND:20261019T014000Z")
            )
        )
        self.assertEqual(s["courses"][0]["start"], "08:00")

    def test_floating_uses_calendar_timezone(self):
        s = parse_calendar(
            calendar(
                event(start="DTSTART:20261019T080000", end="DTEND:20261019T094000"),
                "X-WR-TIMEZONE:Asia/Shanghai\r\n",
            )
        )
        self.assertEqual(s["courses"][0]["start"], "08:00")

    def test_duration_instead_of_dtend(self):
        s = parse_calendar(calendar(event(end="DURATION:PT1H40M")))
        self.assertEqual(s["courses"][0]["end"], "09:40")

    def test_folded_unicode_and_escaped_text(self):
        text = calendar(event(name="大学化学\\,实验\\n预习\r\n 与讨论"))
        name = parse_calendar(text)["courses"][0]["name"]
        self.assertIn("大学化学,实验", name)
        self.assertIn("预习与讨论", name)

    def test_duplicate_exports_deduplicate_even_if_uid_changes(self):
        s = parse_calendar(calendar(event() + event(uid="regenerated-uid")))
        self.assertEqual(len(s["courses"]), 1)

    def test_cross_midnight_keeps_finish_date(self):
        b = ics_binding(
            calendar(
                event(
                    start="DTSTART;TZID=Asia/Shanghai:20261019T233000",
                    end="DTEND;TZID=Asia/Shanghai:20261020T010000",
                )
            )
        )
        item = occurrences(b["schedule"], date(2026, 10, 19), "Asia/Shanghai")[0]
        self.assertEqual(item["finish"].date(), date(2026, 10, 20))

    def test_invalid_or_all_day_calendar_rejected(self):
        for text in (
            "not a calendar",
            calendar(
                event(
                    start="DTSTART;VALUE=DATE:20261019", end="DTEND;VALUE=DATE:20261020"
                )
            ),
            calendar(event(end="")),
            calendar(event(end="DTEND;TZID=Asia/Shanghai:20261019T070000")),
            calendar(event(start="DTSTART;TZID=No/SuchZone:20261019T080000")),
        ):
            with self.subTest(text=text[:70]), self.assertRaises(TimetableError):
                parse_calendar(text)

    def test_infinite_high_frequency_and_large_count_rejected(self):
        for rule in (
            "FREQ=WEEKLY",
            "FREQ=MINUTELY;COUNT=10",
            "FREQ=DAILY;COUNT=1001",
            "FREQ=WEEKLY;UNTIL=20300101T000000Z",
            "FREQ=DAILY;COUNT=2;BYSECOND=1,2",
        ):
            with self.subTest(rule=rule), self.assertRaises(TimetableError):
                parse_calendar(calendar(event(extra=f"RRULE:{rule}\r\n")))

    def test_size_limit(self):
        with self.assertRaises(TimetableError):
            parse_calendar(b"BEGIN:VCALENDAR" + b"x" * MAX_BYTES)

    def test_calendar_alarms_and_manual_override(self):
        b = ics_binding(calendar(event(extra=ALARM)))
        zone = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 10, 19, 7, 35, 5, tzinfo=zone)
        self.assertEqual(due_reminders(b, now)[0]["lead"], 25)
        b["custom_lead"] = True
        self.assertEqual(due_reminders(b, now), [])
        self.assertEqual(next_reminders(b, now, 1)[0]["lead"], 20)

    def test_cancellation_and_manual_move(self):
        s = parse_calendar(calendar(event()))
        self.assertEqual(
            occurrences(s, date(2026, 10, 19), "Asia/Shanghai", {"2026-10-19": None}),
            [],
        )
        moved = occurrences(
            s, date(2026, 10, 24), "Asia/Shanghai", {"2026-10-24": "2026-10-19"}
        )
        self.assertEqual(moved[0]["begin"].date(), date(2026, 10, 24))


class FileAndMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_attachment_reads_bounded_sdk_path(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "wakeup.ics"
            path.write_text(calendar(event()))
            component = types.SimpleNamespace(
                get_file=__import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(
                    return_value=str(path)
                )
            )
            data = await attachment_bytes(component, None)
            self.assertTrue(data.startswith(b"BEGIN:VCALENDAR"))

    async def test_database_migration_preserves_legacy_and_does_not_repeat(self):
        with tempfile.TemporaryDirectory() as root:
            legacy_path = (
                Path(root)
                / "plugin_data"
                / "astrbot_plugin_wakeup_reminder"
                / "timetables.sqlite3"
            )
            legacy_path.parent.mkdir(parents=True)
            old = Store(legacy_path)
            old.save(binding())
            target = migrate_legacy_database(root)
            migrated = Store(target)
            self.assertEqual(len(migrated.all()), 1)
            migrated.delete(binding()["id"])
            migrated.close()
            self.assertEqual(migrate_legacy_database(root), target)
            migrated = Store(target)
            self.assertEqual(migrated.all(), [])
            self.assertEqual(len(old.all()), 1)
            migrated.close()
            old.close()

    async def test_qq_file_import_end_to_end_and_failure_is_transactional(self):
        main = load_plugin_with_sdk_stubs()
        plugin = main.ClassNote(None, {"use_llm_persona": False})
        plugin.store = Store(":memory:")
        plugin.http = object()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "WakeUp.ics"
            path.write_text(calendar(event(extra=ALARM)))
            attachment = main.File("WakeUp.ics", str(path))
            stopped = []
            event_obj = types.SimpleNamespace(
                unified_msg_origin="qq:FriendMessage:10001",
                is_admin=lambda: True,
                get_group_id=lambda: "",
                get_sender_id=lambda: "10001",
                get_sender_name=lambda: "CREAM",
                get_platform_name=lambda: "aiocqhttp",
                get_messages=lambda: [attachment],
                get_message_str=lambda: "",
                stop_event=lambda: stopped.append(True),
                plain_result=lambda x: x,
            )
            replies = [reply async for reply in plugin.calendar_attachment(event_obj)]
            self.assertIn("1课次", replies[0])
            saved = plugin.store.get(plugin._binding_id(event_obj))
            self.assertEqual(saved["schedule"]["kind"], "calendar")
            self.assertTrue(stopped)
            path.write_text("invalid calendar")
            replies = [reply async for reply in plugin.calendar_attachment(event_obj)]
            self.assertIn("不是有效", replies[0])
            self.assertEqual(
                plugin.store.get(saved["id"])["revision"], saved["revision"]
            )
        plugin.store.close()

    async def test_import_wait_and_manual_alarm_switch(self):
        main = load_plugin_with_sdk_stubs()
        plugin = main.ClassNote(None, {"auto_import_ics": False})
        plugin.store = Store(":memory:")
        event_obj = types.SimpleNamespace(
            unified_msg_origin="qq:FriendMessage:10001",
            get_sender_id=lambda: "10001",
            get_messages=lambda: [],
        )
        reply = await plugin._dispatch(event_obj, "导入日历", "")
        self.assertIn("2分钟", reply)
        b = ics_binding(calendar(event(extra=ALARM)))
        b["id"] = plugin._binding_id(event_obj)
        plugin.store.save(b)
        await plugin._dispatch(event_obj, "提前", "10")
        self.assertTrue(plugin.store.get(b["id"])["custom_lead"])
        await plugin._dispatch(event_obj, "提前", "日历")
        self.assertFalse(plugin.store.get(b["id"])["custom_lead"])
        plugin.store.close()


if __name__ == "__main__":
    unittest.main()
