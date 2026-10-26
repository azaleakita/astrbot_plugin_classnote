"""Render one course locally, using the bundled OFL Chinese font."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path

from .core import TimetableError, clean

FORMATS = ("image", "text", "both")
FONT = Path(__file__).parent / "assets" / "fonts" / "NotoSansSC-Bold.ttf"
ACCENT = "#ff2459"


def card_settings(config):
    mode = config.get("reminder_format", "image")
    title = config.get("card_title", "课笺")
    if mode not in FORMATS:
        raise TimetableError("提醒形式必须为 image、text 或 both。")
    if not isinstance(title, str) or not clean(title, 20) or len(title) > 20:
        raise TimetableError("卡片抬头需为 1–20 字，例如学校名称。")
    return mode, clean(title, 20)


def render_course_card(config, item, label="接下来") -> bytes:
    """No browser, network, temp file, or model is involved in image rendering."""
    from PIL import Image, ImageDraw, ImageFont

    _, title = card_settings(config)

    def font(size):
        return ImageFont.truetype(str(FONT), size)

    def ellipsis(text, size, width):
        text = clean(text, 180)
        face = font(size)
        if face.getlength(text) <= width:
            return text, face
        while text and face.getlength(text + "…") > width:
            text = text[:-1]
        return text + "…", face

    # A long course title may occupy two lines; the location/time stay readable.
    name = clean(item["name"], 180)
    title_face = font(52)
    lines = [""]
    for index, char in enumerate(name):
        if title_face.getlength(lines[-1] + char) > 840:
            if len(lines) == 2:
                lines[-1] = ellipsis(lines[-1] + name[index:], 52, 840)[0]
                break
            lines.append("")
        lines[-1] += char
    extra = 68 * (len(lines) - 1)
    width, height = 1000, 430 + extra
    card = Image.new("RGBA", (width, height))
    # Subtle charcoal gradient and hairline rim, matching the supplied widget.
    gradient = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(gradient)
    for y in range(height):
        tone = round(33 - 21 * y / (height - 1))
        draw.line((0, y, width, y), fill=(tone, tone, tone, 255))
    mask = Image.new("L", (width, height))
    ImageDraw.Draw(mask).rounded_rectangle((1, 1, width - 2, height - 2), 62, fill=255)
    card.paste(gradient, (0, 0), mask)
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle(
        (1, 1, width - 2, height - 2), 62, outline="#444444", width=2
    )

    begin, finish = item["begin"], item["finish"]
    week = item.get("week")
    week_text = f"第 {week} 周" if isinstance(week, int) and week > 0 else "日历课程"
    header = font(32)
    right = width - 52 - header.getlength(week_text)
    heading, _ = ellipsis(title, 32, max(120, right - 380))
    x = 52
    for text, color in (
        (heading + " | ", "#f5f5f5"),
        (f"{begin.month:02d}.{begin.day:02d} ", "#f5f5f5"),
        ("周" + "一二三四五六日"[begin.weekday()], ACCENT),
    ):
        draw.text((x, 43), text, font=header, fill=color)
        x += header.getlength(text)
    draw.text((right, 43), week_text, font=header, fill="#f5f5f5")
    draw.text((52, 127), label, font=font(32), fill="#f5f5f5")
    draw.rounded_rectangle((52, 193, 62, 371 + extra), 5, fill=ACCENT)
    for index, line in enumerate(lines):
        draw.text((80, 179 + 68 * index), line, font=title_face, fill="#ffffff")
    room, room_face = ellipsis(item.get("room") or "地点未填写", 34, 840)
    draw.text((80, 261 + extra), room, font=room_face, fill="#f5f5f5")
    time_text = (
        f"{begin:%H:%M} - {finish:%H:%M}"
        if begin.date() == finish.date()
        else f"{begin:%m.%d %H:%M} - {finish:%m.%d %H:%M}"
    )
    draw.text((80, 314 + extra), time_text, font=font(36), fill="#f5f5f5")
    output = BytesIO()
    card.save(output, format="PNG", optimize=True)
    return output.getvalue()


def course_label(item, now: datetime):
    return "正在上课" if item["begin"] <= now < item["finish"] else "接下来"
