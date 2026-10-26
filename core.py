"""Validated timetable parsing and pure, timezone-aware occurrence calculation."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time, timedelta
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo


class TimetableError(ValueError):
    pass


def digest(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def clean(value, limit=120) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).strip()[:limit]


def bounded_int(value, low, high, field):
    if isinstance(value, bool):
        raise TimetableError(f"{field} 必须是整数。")
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise TimetableError(f"{field} 必须是整数。") from exc
    if str(value).strip() != str(n) or not low <= n <= high:
        raise TimetableError(f"{field} 必须在 {low}–{high} 之间。")
    return n


def iso_date(value) -> date:
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError) as exc:
        raise TimetableError("日期必须为 YYYY-MM-DD。") from exc


def clock(value) -> str:
    try:
        t = time.fromisoformat(str(value))
        if t.tzinfo or t.second or t.microsecond:
            raise ValueError
        return t.strftime("%H:%M")
    except (ValueError, TypeError) as exc:
        raise TimetableError(f"无效的节次时间：{clean(value)}，需要 HH:MM。") from exc


def extract_share_key(text: str) -> str:
    """Never visit a user URL; extract a key and use the one fixed API host."""
    text = unquote(text.strip())
    if len(text) > 20000:
        raise TimetableError("分享文本过长。")
    for url in re.findall(r"(?:https?|wakeup)://[^\s<>「」\"']+", text):
        parsed = urlsplit(url.rstrip("。），,)]}"))
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "wakeup" and not (
            host == "wakeup.fun" or host.endswith(".wakeup.fun")
        ):
            continue
        query = parse_qs(parsed.query)
        query.update(parse_qs(parsed.fragment.lstrip("?")))
        for label in ("key", "shareKey", "share_key"):
            candidate = query.get(label, [""])[0]
            if re.fullmatch(r"[A-Za-z0-9_-]{8,128}", candidate):
                return candidate
        match = re.search(r"(?:^|/)([0-9a-fA-F]{32})(?:/|$)", parsed.path)
        if match:
            return match[1]
    match = re.search(
        r"(?:分享口令(?:为)?|口令)\s*[：:为]?\s*[「『\"']?([A-Za-z0-9_-]{8,128})",
        text,
    )
    if match:
        return match[1]
    if re.fullmatch(r"[0-9a-fA-F]{32}", text):
        return text
    raise TimetableError(
        "未识别分享 key。请粘贴完整 WakeUp 分享文本、含 key 的链接或 32 位口令。"
    )


def unwrap_api(payload):
    if not isinstance(payload, dict):
        raise TimetableError("WakeUp 返回了非 JSON 对象。")
    if str(payload.get("status")) != "1":
        status = str(payload.get("status", "unknown"))
        if status == "5000004":
            raise TimetableError(
                "WakeUp 返回“版本过低”，当前分享协议不兼容旧分享接口。"
                "请使用 /课表 导入文本 粘贴 WakeUp 导出的备份文本；原课表未被替换。"
            )
        raise TimetableError(
            f"WakeUp 导入未成功（状态 {clean(status, 30)}）。"
            "请在 App 重新分享后尝试；也可能是分享协议不兼容。原课表未被替换。"
        )
    if not payload.get("data"):
        raise TimetableError("分享返回空课表，原课表未被替换。")
    return payload["data"]


def _parts(payload, depth=0):
    if depth > 4:
        raise TimetableError("课表 JSON 嵌套层数过多。")
    if isinstance(payload, str):
        payload = payload.strip().lstrip("\ufeff")
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            lines = payload.splitlines()
            if len(lines) < 5:
                raise TimetableError("不支持的课表文本，需要 WakeUp 的五段 JSON 备份。")
            try:
                # WakeUp 经典备份：版本行、时间数组、学期对象、课程数组、课次数组。
                return tuple(json.loads(lines[i]) for i in (2, 1, 3, 4))
            except json.JSONDecodeError as exc:
                raise TimetableError("WakeUp 备份中的 JSON 不完整。") from exc
        if isinstance(obj, str):
            return _parts(obj, depth + 1)
        return _parts(obj, depth + 1)
    if isinstance(payload, dict):
        if "status" in payload:
            return _parts(unwrap_api(payload), depth + 1)
        table = payload.get("tableInfo", payload)
        times = payload.get("timeList", payload.get("timeTable"))
        infos = payload.get("courseInfos", payload.get("courseList"))
        details = payload.get("courseDetails", payload.get("courseDetailList", infos))
        return table, times, infos, details
    raise TimetableError("不支持的 WakeUp 课表结构。")


def parse_timetable(payload) -> dict:
    table, times, infos, details = _parts(payload)
    if not isinstance(table, dict) or not all(
        isinstance(x, list) for x in (times, infos, details)
    ):
        raise TimetableError("课表缺少学期、节次或课程数组。")
    if not infos or not details or len(details) > 2000 or len(times) > 100:
        raise TimetableError("课表为空或超过大小限制。")
    start = iso_date(table.get("startDate"))
    monday = start - timedelta(days=start.weekday())
    warnings = []
    if start != monday:
        warnings.append(
            f"startDate 不是周一，已按所在周周一 {monday} 计算第 1 周，请核对。"
        )
    slots = {}
    for item in times:
        if not isinstance(item, dict):
            raise TimetableError("节次项必须为 JSON 对象。")
        n = bounded_int(item.get("node"), 1, 100, "节次")
        begin, end = clock(item.get("startTime")), clock(item.get("endTime"))
        if begin >= end or n in slots:
            raise TimetableError("节次时间倒置、跨日或节次编号重复，请修正课表。")
        slots[n] = (begin, end)
    if not all(isinstance(x, dict) for x in infos + details):
        raise TimetableError("课程项必须为 JSON 对象。")
    names = {str(x.get("id")): x for x in infos if "id" in x}
    courses = {}
    for d in details:
        info = names.get(str(d.get("id")), d)
        name = clean(d.get("courseName", info.get("courseName")))
        if not name:
            raise TimetableError("有课程缺少名称，未导入任何新数据。")
        if d.get("ownTime"):
            raise TimetableError(
                f"《{name}》使用了独立时间 ownTime，首版暂不支持。请改为节次时间后重新导入。"
            )
        day = bounded_int(d.get("day"), 1, 7, "星期")
        node = bounded_int(d.get("startNode"), 1, 100, "起始节次")
        step = bounded_int(d.get("step"), 1, 100, "连续节数")
        section = list(range(node, node + step))
        if any(n not in slots for n in section):
            raise TimetableError(f"《{name}》对应的节次作息缺失。")
        if any(slots[a][1] > slots[b][0] for a, b in zip(section, section[1:])):
            raise TimetableError(f"《{name}》对应的节次作息重叠或时间倒置。")
        explicit = d.get("weeks", d.get("stepWeekList"))
        if explicit is not None:
            if not isinstance(explicit, list) or not explicit:
                raise TimetableError(f"《{name}》的周次列表无效。")
            weeks = sorted({bounded_int(w, 1, 60, "教学周") for w in explicit})
        else:
            first = bounded_int(d.get("startWeek"), 1, 60, "起始周")
            last = bounded_int(d.get("endWeek"), first, 60, "结束周")
            kind = bounded_int(d.get("type", 0), 0, 2, "单双周类型")
            weeks = [
                w for w in range(first, last + 1) if kind == 0 or w % 2 == kind % 2
            ]
        if not weeks:
            raise TimetableError(f"《{name}》筛选单双周后没有有效周次。")
        c = {
            "name": name,
            "teacher": clean(d.get("teacher", info.get("teacher"))),
            "room": clean(d.get("room")),
            "day": day,
            "start": slots[node][0],
            "end": slots[node + step - 1][1],
            "node": node,
            "step": step,
            "weeks": weeks,
        }
        # Stable across repeated imports and changes to active-week ranges.
        c["id"] = digest({k: v for k, v in c.items() if k != "weeks"})
        if c["id"] in courses:
            c["weeks"] = sorted(set(c["weeks"]) | set(courses[c["id"]]["weeks"]))
        courses[c["id"]] = c
    return {
        "name": clean(table.get("tableName", "WakeUp课表")),
        "monday": monday.isoformat(),
        "courses": list(courses.values()),
        "warnings": warnings,
    }


def teaching_week(schedule, on: date) -> int:
    return (on - iso_date(schedule["monday"])).days // 7 + 1


def occurrences(schedule, on: date, tz: str, overrides=None) -> list[dict]:
    overrides = overrides or {}
    # Each override is a direct replacement, deliberately not recursive.
    source = overrides.get(on.isoformat(), on.isoformat())
    if source is None:
        return []
    source_date = iso_date(source)
    week = teaching_week(schedule, source_date)
    zone = ZoneInfo(tz)
    result = []
    if schedule.get("kind") == "calendar":
        for index in schedule["dates"].get(source_date.isoformat(), []):
            c = schedule["courses"][index]
            original_begin = datetime.fromisoformat(c["begin_iso"]).astimezone(zone)
            original_end = datetime.fromisoformat(c["finish_iso"]).astimezone(zone)
            if on == source_date:
                begin, end = original_begin, original_end
            else:
                shift = on - source_date
                begin, end = original_begin + shift, original_end + shift
            result.append({**c, "begin": begin, "finish": end, "week": None})
        return sorted(result, key=lambda c: (c["begin"], c["name"]))
    for c in schedule["courses"]:
        if c["day"] != source_date.isoweekday() or week not in c["weeks"]:
            continue
        begin = datetime.combine(on, time.fromisoformat(c["start"]), zone)
        end = datetime.combine(on, time.fromisoformat(c["end"]), zone)
        result.append({**c, "begin": begin, "finish": end, "week": week})
    return sorted(result, key=lambda c: (c["begin"], c["name"]))


def delivery_key(binding_id: str, occurrence: dict, minutes: int) -> str:
    return digest(
        [binding_id, occurrence["id"], occurrence["begin"].isoformat(), minutes]
    )


def reminder_leads(binding, occurrence):
    if (
        binding["schedule"].get("kind") == "calendar"
        and binding.get("respect_ics_alarms", True)
        and not binding.get("custom_lead", False)
        and occurrence.get("alarm_minutes")
    ):
        return occurrence["alarm_minutes"]
    return binding["lead_minutes"]


def reminder_key(binding, occurrence, minutes):
    """Keep delivery identity stable when only the display timezone changes."""
    zone = ZoneInfo(binding.get("key_timezone", binding["timezone"]))
    return delivery_key(
        binding["id"],
        {**occurrence, "begin": occurrence["begin"].astimezone(zone)},
        minutes,
    )


def due_reminders(binding: dict, now: datetime, grace_seconds=300) -> list[dict]:
    local = now.astimezone(ZoneInfo(binding["timezone"]))
    result = []
    # lead is bounded to 24h: yesterday/today/tomorrow cover midnight crossings.
    for delta in (-1, 0, 1):
        on = local.date() + timedelta(days=delta)
        for item in occurrences(
            binding["schedule"], on, binding["timezone"], binding["overrides"]
        ):
            if item["begin"] <= local:
                continue
            for minutes in reminder_leads(binding, item):
                fire = item["begin"] - timedelta(minutes=minutes)
                age = (local - fire).total_seconds()
                if 0 <= age <= grace_seconds:
                    result.append(
                        {
                            **item,
                            "fire": fire,
                            "lead": minutes,
                            "key": reminder_key(binding, item, minutes),
                        }
                    )
    return sorted(result, key=lambda x: (x["fire"], x["name"]))


def next_reminders(binding: dict, now: datetime, limit=5) -> list[dict]:
    local = now.astimezone(ZoneInfo(binding["timezone"]))
    # Full 60-week range also supports a far-future semester start.
    first = max(local.date(), iso_date(binding["schedule"]["monday"]))
    result = []
    for delta in range(552 if binding["schedule"].get("kind") == "calendar" else 422):
        on = first + timedelta(days=delta)
        for c in occurrences(
            binding["schedule"], on, binding["timezone"], binding["overrides"]
        ):
            for lead in reminder_leads(binding, c):
                fire = c["begin"] - timedelta(minutes=lead)
                if fire > local:
                    result.append({**c, "fire": fire, "lead": lead})
        # All reminders for a date can precede its midnight by at most 24h.
        if len(result) >= limit and delta >= 1:
            boundary = datetime.combine(on + timedelta(days=1), time.min, local.tzinfo)
            cutoff = sorted(result, key=lambda x: x["fire"])[limit - 1]["fire"]
            if boundary - timedelta(days=1) > cutoff:
                break
    return sorted(result, key=lambda x: x["fire"])[:limit]
