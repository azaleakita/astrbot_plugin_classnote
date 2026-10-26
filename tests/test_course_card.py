"""Real PNG rendering and course selection, independent of reminder lead times."""

import unittest
from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from astrbot_plugin_classnote.core import (
    TimetableError,
    current_or_next_course,
    occurrences,
)
from astrbot_plugin_classnote.course_card import (
    card_settings,
    course_label,
    render_course_card,
)
from astrbot_plugin_classnote.ics import parse_calendar
from astrbot_plugin_classnote.tests.test_reminder import binding
from PIL import Image, ImageDraw


class CourseCardTests(unittest.TestCase):
    def setUp(self):
        self.binding = binding()
        self.now = datetime(2026, 9, 7, 7, 55, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.item = occurrences(
            self.binding["schedule"], self.now.date(), "Asia/Shanghai"
        )[0]

    def test_course_lookup_after_final_advance_reminder(self):
        item = current_or_next_course(self.binding, self.now)
        self.assertEqual(item["name"], "大学化学")
        self.assertEqual(course_label(item, self.now), "接下来")
        self.assertEqual(course_label(item, item["begin"]), "正在上课")

    def test_lookup_excludes_finished_and_cancelled_courses(self):
        self.binding["overrides"]["2026-09-07"] = None
        item = current_or_next_course(self.binding, self.now)
        self.assertGreater(item["begin"].date(), self.now.date())
        self.binding["overrides"] = {}
        item = current_or_next_course(self.binding, self.item["finish"])
        self.assertGreater(item["begin"].date(), self.now.date())

    def test_lookup_after_semester_returns_none(self):
        self.assertIsNone(
            current_or_next_course(self.binding, self.now.replace(year=2030))
        )

    def test_cross_midnight_calendar_keeps_ongoing_course(self):
        self.binding["schedule"] = parse_calendar(
            "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:night\n"
            "DTSTART:20260906T153000Z\nDTEND:20260906T163000Z\n"
            "SUMMARY:夜间实验\nEND:VEVENT\nEND:VCALENDAR"
        )
        now = self.now.replace(hour=0, minute=10)
        item = current_or_next_course(self.binding, now)
        self.assertEqual(item["name"], "夜间实验")
        self.assertIsNone(item["week"])
        self.assertEqual(course_label(item, now), "正在上课")

    def test_png_has_only_rounded_widget_and_correct_course_facts(self):
        text = []
        original = ImageDraw.ImageDraw.text

        def record(draw, xy, value, *args, **kwargs):
            text.append(value)
            return original(draw, xy, value, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, "text", record):
            data = render_course_card({"card_title": "吉林大学"}, self.item)
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.size, (1000, 430))
            self.assertEqual(image.getpixel((0, 0))[3], 0)
            self.assertEqual(image.getpixel((500, 200))[3], 255)
        with Image.open(BytesIO(data)) as image:
            image.verify()
        rendered = " ".join(text)
        for expected in (
            "吉林大学",
            "09.07",
            "周一",
            "第 1 周",
            "大学化学",
            "逸夫楼A201",
            "08:00 - 09:40",
        ):
            self.assertIn(expected, rendered)

    def test_long_titles_expand_to_two_lines_and_rooms_are_bounded(self):
        item = {
            **self.item,
            "name": "非常长的课程名称" * 20,
            "room": "超长教室位置" * 20,
        }
        data = render_course_card({"card_title": "名称很长的大学名称" * 2}, item)
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.size, (1000, 498))
            image.verify()

    def test_ics_card_shows_calendar_instead_of_invented_week(self):
        text = []
        original = ImageDraw.ImageDraw.text

        def record(draw, xy, value, *args, **kwargs):
            text.append(value)
            return original(draw, xy, value, *args, **kwargs)

        item = {
            **self.item,
            "week": None,
            "room": "",
            "finish": self.item["begin"] + timedelta(hours=20),
        }
        with patch.object(ImageDraw.ImageDraw, "text", record):
            render_course_card({}, item)
        rendered = " ".join(text)
        self.assertIn("日历课程", rendered)
        self.assertNotIn("第 ", rendered)
        self.assertIn("地点未填写", rendered)
        self.assertIn("09.07 08:00 - 09.08 04:00", rendered)

    def test_invalid_settings_rejected(self):
        for config in (
            {"reminder_format": "html"},
            {"card_title": ""},
            {"card_title": "x" * 21},
            {"card_title": 12},
        ):
            with self.subTest(config=config), self.assertRaises(TimetableError):
                card_settings(config)


if __name__ == "__main__":
    unittest.main()
