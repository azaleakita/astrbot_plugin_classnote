"""Validated task requirement templates shared by scheduling and delivery."""

from string import Formatter

from .core import TimetableError

DEFAULT_REQUIREMENT = (
    "请沿用当前会话的 bot 人格，自然地提醒 {nickname} 准备上课。\n"
    "课程：{course}；日期：{date}；时间：{start}–{end}；"
    "地点：{room}；提前 {lead} 分钟；时区：{timezone}。"
)
FIELDS = {"nickname", "course", "date", "start", "end", "room", "lead", "timezone"}


def validate_requirement(value, *, template=False):
    limit = 2000 if template else 4000
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise TimetableError(f"任务需求必须是1至{limit}字的非空文本。")
    if template:
        try:
            for _, field, spec, conversion in Formatter().parse(value):
                if field is not None and (field not in FIELDS or spec or conversion):
                    raise ValueError("unsupported placeholder")
        except ValueError as exc:
            raise TimetableError(
                "任务需求模板含无效占位符。可用："
                + "、".join("{" + field + "}" for field in sorted(FIELDS))
                + "；字面花括号请写成 {{ 或 }}。"
            ) from exc
    return value.strip()


def render_requirement(config, binding, item):
    template = validate_requirement(
        config.get("task_requirement_template", DEFAULT_REQUIREMENT), template=True
    )
    return validate_requirement(
        template.format_map(
            {
                "nickname": binding["nickname"] or binding["sender"],
                "course": item["name"],
                "date": item["begin"].strftime("%Y-%m-%d"),
                "start": item["begin"].strftime("%H:%M"),
                "end": item["finish"].strftime("%H:%M"),
                "room": item.get("room") or "未注明",
                "lead": item.get("lead", binding["lead_minutes"][0]),
                "timezone": binding["timezone"],
            }
        )
    )
