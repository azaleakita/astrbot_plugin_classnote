"""Import a finite iCalendar snapshot without inventing weekly course rules."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from math import ceil
from zoneinfo import ZoneInfo

import recurring_ical_events
from icalendar import Calendar

from .core import TimetableError, clean, digest

MAX_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 10000
MAX_SPAN_DAYS = 550


def _datetime(prop, floating_zone, label):
    value = getattr(prop, "dt", None)
    if not isinstance(value, datetime):
        raise TimetableError(f"{label}缺少精确上课时间；暂不导入全天课程。")
    tzid = getattr(prop, "params", {}).get("TZID")
    if value.tzinfo is None:
        if tzid:
            try:
                return value.replace(tzinfo=ZoneInfo(str(tzid)))
            except (ValueError, KeyError) as exc:
                raise TimetableError(f"无法识别日历时区：{clean(tzid)}。") from exc
        return value.replace(tzinfo=floating_zone)
    return value


def _alarm_minutes(event):
    result = set()
    for alarm in event.walk("VALARM"):
        trigger = alarm.get("TRIGGER")
        if (
            not trigger
            or str(trigger.params.get("RELATED", "START")).upper() != "START"
        ):
            continue
        delta = getattr(trigger, "dt", None)
        if isinstance(delta, timedelta):
            seconds = -delta.total_seconds()
            if 60 <= seconds <= 86400 and seconds % 60 == 0:
                result.add(int(seconds // 60))
    if len(result) > 3:
        raise TimetableError("日历中单次课的有效提醒超过3个，请精简后重新导出。")
    return sorted(result, reverse=True)


def _validate_recurrence(event, start, floating_zone):
    """Reject infinite/high-frequency rules before entering a recurrence iterator."""
    rule = event.get("RRULE")
    if not rule:
        return 1
    if isinstance(rule, list):
        raise TimetableError("单个课次包含多个 RRULE，暂不支持。")
    frequency = str(rule.get("FREQ", [""])[0]).upper()
    if frequency not in {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}:
        raise TimetableError("日历包含小时、分钟或其他不适合作为课程的重复频率。")
    if any(key in rule for key in ("BYSECOND", "BYMINUTE", "BYHOUR")):
        raise TimetableError("课程重复规则不可在一天内展开多个小时或分钟。")
    interval = int(rule.get("INTERVAL", [1])[0])
    if not 1 <= interval <= 366:
        raise TimetableError("日历重复间隔超出支持范围。")
    count = rule.get("COUNT")
    until = rule.get("UNTIL")
    if not count and not until:
        raise TimetableError("日历包含无限重复课程，请先设置学期截止日期再导出。")
    if count:
        count = int(count[0])
        if not 1 <= count <= 1000:
            raise TimetableError("单条课程重复次数必须为1至1000次。")
    if until:
        end = until[0]
        if isinstance(end, datetime):
            end_date = (
                end.astimezone(floating_zone).date() if end.tzinfo else end.date()
            )
        elif isinstance(end, date):
            end_date = end
        else:
            raise TimetableError("无法识别课程重复截止日期。")
        days = (end_date - start.astimezone(floating_zone).date()).days
        if not 0 <= days <= MAX_SPAN_DAYS:
            raise TimetableError("重复课程必须在550天内结束。")
        if frequency == "DAILY":
            estimate = ceil(days / interval) + 1
        elif frequency == "WEEKLY":
            estimate = (ceil(days / (7 * interval)) + 1) * len(rule.get("BYDAY", [1]))
        elif frequency == "MONTHLY":
            days_in_month = (
                len(rule["BYMONTHDAY"])
                if "BYMONTHDAY" in rule
                else (31 if "BYDAY" in rule else 1)
            )
            estimate = (ceil(days / (28 * interval)) + 1) * days_in_month
        else:
            estimate = (ceil(days / (365 * interval)) + 1) * (
                366
                if any(k in rule for k in ("BYDAY", "BYYEARDAY", "BYMONTHDAY"))
                else 1
            )
        return min(count, estimate) if count else estimate
    return count


def parse_calendar(
    content: bytes | str, timezone="Asia/Shanghai", filename="WakeUp日历"
) -> dict:
    """Expand RRULE/RDATE/EXDATE/RECURRENCE-ID to exact, bounded occurrences."""
    if isinstance(content, str):
        content = content.lstrip("\ufeff").encode("utf-8")
    if not content or len(content) > MAX_BYTES:
        raise TimetableError("日历为空或超过2MiB。")
    content = content.removeprefix(b"\xef\xbb\xbf")
    if not content.lstrip().upper().startswith(b"BEGIN:VCALENDAR"):
        raise TimetableError(
            "不是有效的 .ics 日历文件，请在 WakeUp 选择“导出为日历文件”。"
        )
    output_zone = ZoneInfo(timezone)
    try:
        cal = Calendar.from_ical(content)
        floating_tz_name = str(cal.get("X-WR-TIMEZONE", timezone))
        floating_zone = ZoneInfo(floating_tz_name)
        source_events = cal.walk("VEVENT")
        if not source_events or len(source_events) > 2000:
            raise TimetableError("日历没有课程，或原始事件超过2000条。")
        budget = 0
        any_alarm = False
        for event in source_events:
            if event.errors:
                raise TimetableError("日历含有无法解析的课程字段，未替换原课表。")
            if str(event.get("STATUS", "")).upper() == "CANCELLED":
                continue
            if not str(event.get("SUMMARY", "")).strip():
                raise TimetableError("日历有课程缺少名称。")
            start = _datetime(event.get("DTSTART"), floating_zone, "课程")
            if not event.get("DTEND") and not event.get("DURATION"):
                raise TimetableError("日历有课程缺少结束时间或持续时长。")
            if event.get("DTEND"):
                end = _datetime(event.get("DTEND"), floating_zone, "课程结束")
                duration = end - start
            else:
                duration = event.decoded("DURATION")
            if not isinstance(duration, timedelta) or not timedelta(
                0
            ) < duration <= timedelta(days=1):
                raise TimetableError("原始日历有课程时间倒置、零时长或持续超过24小时。")
            budget += _validate_recurrence(event, start, floating_zone)
            if budget > MAX_EVENTS:
                raise TimetableError("日历重复规则的预计规模过大，请按单个学期导出。")
            any_alarm |= bool(_alarm_minutes(event))
        courses = {}
        # The library handles folded UTF-8 lines, escaped TEXT and recurrence overrides.
        iterator = recurring_ical_events.of(cal, skip_bad_series=False).all()
        for index, event in enumerate(iterator):
            if index >= MAX_EVENTS:
                raise TimetableError("日历展开超过10000课次，请按单个学期导出。")
            if str(event.get("STATUS", "")).upper() == "CANCELLED":
                continue
            begin = _datetime(event.get("DTSTART"), floating_zone, "课程").astimezone(
                output_zone
            )
            if event.get("DTEND"):
                finish = _datetime(
                    event.get("DTEND"), floating_zone, "课程结束"
                ).astimezone(output_zone)
            else:
                duration = event.decoded("DURATION")
                if not isinstance(duration, timedelta):
                    raise TimetableError("日历持续时长无效。")
                finish = begin + duration
            if not timedelta(0) < finish - begin <= timedelta(days=1):
                raise TimetableError("日历有课次时间倒置、零时长或持续超过24小时。")
            name, room = clean(event.get("SUMMARY")), clean(event.get("LOCATION"))
            item = {
                "name": name,
                "room": room,
                "teacher": "",
                "date": begin.date().isoformat(),
                "start": begin.strftime("%H:%M"),
                "end": finish.strftime("%H:%M"),
                "begin_iso": begin.isoformat(),
                "finish_iso": finish.isoformat(),
                "alarm_minutes": _alarm_minutes(event),
                # Independent of an exporter's regenerated UID: repeated export stays idempotent.
                "id": digest(
                    ["ics", name, room, begin.isoformat(), finish.isoformat()]
                ),
            }
            courses[item["id"]] = item
        ordered = sorted(courses.values(), key=lambda c: (c["begin_iso"], c["name"]))
        if not ordered:
            raise TimetableError("日历中没有有效的定时课程，原课表未被替换。")
        first, last = (
            date.fromisoformat(ordered[0]["date"]),
            date.fromisoformat(ordered[-1]["date"]),
        )
        if (last - first).days > MAX_SPAN_DAYS:
            raise TimetableError("课表跨度超过550天，请按学期拆分后导入。")
        dates = {}
        for i, course in enumerate(ordered):
            dates.setdefault(course["date"], []).append(i)
        return {
            "kind": "calendar",
            "name": clean(cal.get("X-WR-CALNAME", filename)),
            "monday": (first - timedelta(days=first.weekday())).isoformat(),
            "courses": ordered,
            "dates": dates,
            "first_date": first.isoformat(),
            "last_date": last.isoformat(),
            "has_alarms": any_alarm,
            "warnings": ["日历按实际日期提醒；原始教学周起点未从日历中推断。"],
        }
    except TimetableError:
        raise
    except Exception as exc:
        raise TimetableError(
            "日历格式、重复规则或时区无法解析，原课表未被替换。"
        ) from exc
