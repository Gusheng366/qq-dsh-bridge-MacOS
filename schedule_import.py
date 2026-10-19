#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""课程表导入器：把 ICS 展开成「只有未来的」扁平课表。

用法：
    python3 schedule_import.py <课表.ics>
    python3 schedule_import.py <课表.ics> --out qq-workspace/课程表

产出三个文件（默认写到 ./qq-workspace/课程表/）：
    schedule.json   给桥接读的结构化数据
    schedule.md     给人看的课表
    未来课程.ics    清理过的日历文件，可再导入手机 / 日历 App

设计取舍：
  - 源文件用 RRULE 周重复表达，本脚本把它展开成一条条独立课程，
    并且只保留「今天及以后」的，这样「今天之前的课程」就是真的没了。
  - 为什么展开：AI 读 RRULE 容易算错日期；展开后每条都是确定的日子，
    查「几号上课」不会出错。89 条的体量也完全可接受。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 课程时间 → 节次（源文件 DESCRIPTION 里有，这里作为兜底）
DEFAULT_PERIODS = [
    ("08:00", "09:40", "第1-2节"),
    ("10:00", "11:40", "第3-4节"),
    ("14:30", "16:10", "第5-6节"),
    ("16:30", "18:10", "第7-8节"),
    ("19:00", "20:40", "第9-10节"),
]


def _unescape(text: str) -> str:
    """ICS 文本转义还原。"""
    return (
        text.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _field(block: str, key: str) -> str:
    """取某个字段（忽略 TZID 等参数）。"""
    m = re.search(rf"^{re.escape(key)}(?:;[^:]*)?:(.+)$", block, re.M)
    return _unescape(m.group(1).strip()) if m else ""


def _parse_dt(raw: str) -> datetime | None:
    """解析 DTSTART / DTEND 的值。"""
    m = re.match(r"(\d{8})T(\d{6})", raw)
    if not m:
        m2 = re.match(r"(\d{8})", raw)
        if m2:
            return datetime.strptime(m2.group(1), "%Y%m%d")
        return None
    return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")


def _split_location(location: str) -> tuple[str, str]:
    """'明辨楼406 徐海峰' → ('明辨楼406', '徐海峰')。"""
    parts = [p for p in location.replace("\u3000", " ").split(" ") if p.strip()]
    if len(parts) >= 2:
        return " ".join(parts[:-1]), parts[-1]
    return location.strip(), ""


def _period_of(start: datetime, end: datetime, desc: str) -> str:
    """优先用 DESCRIPTION 里的「第X-Y节」，否则按时间猜。"""
    m = re.search(r"第\s*\d+\s*-\s*\d+\s*节", desc)
    if m:
        return m.group(0).replace(" ", "")
    hhmm = f"{start:%H:%M}"
    for s, _e, p in DEFAULT_PERIODS:
        if s == hhmm:
            return p
    return ""


def parse_ics(path: Path) -> list[dict]:
    """解析 ICS，展开重复事件，返回（未过滤的）课程实例列表。"""
    src = path.read_text("utf-8", errors="replace")
    blocks = re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", src, re.S)
    classes: list[dict] = []

    for block in blocks:
        name = _field(block, "SUMMARY").strip()
        if not name:
            continue
        location = _field(block, "LOCATION")
        desc = _field(block, "DESCRIPTION")
        room, teacher = _split_location(location)

        start = _parse_dt(_field(block, "DTSTART"))
        end = _parse_dt(_field(block, "DTEND"))
        if start is None:
            continue
        if end is None:
            end = start + timedelta(minutes=100)
        duration = end - start

        rrule = _field(block, "RRULE")
        freq_weekly = bool(re.search(r"FREQ=WEEKLY", rrule, re.I))
        interval = 1
        m_int = re.search(r"INTERVAL=(\d+)", rrule)
        if m_int:
            interval = max(1, int(m_int.group(1)))

        m_until = re.search(r"UNTIL=(\d{8})", rrule)
        # UNTIL 的日期即最后一个有效日（含）
        until = datetime.strptime(m_until.group(1), "%Y%m%d") if m_until else None

        if not freq_weekly or until is None:
            # 单次事件
            classes.append(
                {
                    "start": start,
                    "end": start + duration,
                    "name": name,
                    "room": room,
                    "teacher": teacher,
                    "period": _period_of(start, start + duration, desc),
                }
            )
            continue

        cur = start
        guard = 0
        while cur.date() <= until.date() and guard < 400:
            classes.append(
                {
                    "start": cur,
                    "end": cur + duration,
                    "name": name,
                    "room": room,
                    "teacher": teacher,
                    "period": _period_of(cur, cur + duration, desc),
                }
            )
            cur += timedelta(weeks=interval)
            guard += 1

    classes.sort(key=lambda c: (c["start"], c["name"]))
    return classes


def render_json(classes: list[dict], source: str, out: Path) -> None:
    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": source,
        "range": {
            "from": classes[0]["start"].strftime("%Y-%m-%d") if classes else "",
            "to": classes[-1]["start"].strftime("%Y-%m-%d") if classes else "",
        },
        "count": len(classes),
        "classes": [
            {
                "date": c["start"].strftime("%Y-%m-%d"),
                "weekday": WEEKDAYS[c["start"].weekday()],
                "start": c["start"].strftime("%H:%M"),
                "end": c["end"].strftime("%H:%M"),
                "name": c["name"],
                "room": c["room"],
                "teacher": c["teacher"],
                "period": c["period"],
            }
            for c in classes
        ],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")


def render_md(classes: list[dict], source: str, out: Path) -> None:
    by_day: dict[str, list[dict]] = {}
    for c in classes:
        by_day.setdefault(c["start"].strftime("%Y-%m-%d"), []).append(c)

    lines = [
        "# 课程表（未来）",
        "",
        f"- 来源：`{source}`",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 范围：{classes[0]['start']:%Y-%m-%d} ~ {classes[-1]['start']:%Y-%m-%d}"
        if classes
        else "- 范围：无",
        f"- 共 {len(classes)} 节，{len(by_day)} 个上课日",
        "",
        "---",
        "",
    ]
    for day in sorted(by_day):
        d = datetime.strptime(day, "%Y-%m-%d")
        lines.append(f"## {day} {WEEKDAYS[d.weekday()]}")
        lines.append("")
        lines.append("| 时间 | 课程 | 地点 | 老师 | 节次 |")
        lines.append("|---|---|---|---|---|")
        for c in by_day[day]:
            lines.append(
                f"| {c['start']:%H:%M}-{c['end']:%H:%M} | {c['name']} | "
                f"{c['room']} | {c['teacher']} | {c['period']} |"
            )
        lines.append("")
    out.write_text("\n".join(lines), "utf-8")


def render_ics(classes: list[dict], out: Path) -> None:
    """生成扁平 ICS：每条课程独立，没有 RRULE，只有未来。"""
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//qq-dsh-bridge//schedule-import//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:课程表（未来）",
        "X-WR-TIMEZONE:Asia/Shanghai",
        "BEGIN:VTIMEZONE",
        "TZID:Asia/Shanghai",
        "BEGIN:STANDARD",
        "TZNAME:CST",
        "TZOFFSETFROM:+0800",
        "TZOFFSETTO:+0800",
        "DTSTART:19700101T000000",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]
    for i, c in enumerate(classes, 1):
        s, e = c["start"], c["end"]
        summary = c["name"].replace(",", "\\,").replace(";", "\\;")
        location = f"{c['room']} {c['teacher']}".strip()
        desc_parts = []
        if c["period"]:
            desc_parts.append(c["period"])
        desc_parts.append(location)
        description = "\\n".join(desc_parts)

        lines += [
            "BEGIN:VEVENT",
            f"UID:qqdsh-course-{i:03d}-{s:%Y%m%dT%H%M}",
            f"DTSTAMP:{stamp}",
            f"SUMMARY:{summary}",
            f"DTSTART;TZID=Asia/Shanghai:{s:%Y%m%dT%H%M%S}",
            f"DTEND;TZID=Asia/Shanghai:{e:%Y%m%dT%H%M%S}",
            f"LOCATION:{location}",
            f"DESCRIPTION:{description}",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "TRIGGER;RELATED=START:-PT20M",
            f"DESCRIPTION:{summary}@{c['room']}",
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    out.write_text("\r\n".join(lines) + "\r\n", "utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="把 ICS 课表展开成只有未来的扁平课表")
    ap.add_argument("ics", help="源 ICS 文件")
    ap.add_argument(
        "--out",
        default="qq-workspace/课程表",
        help="输出目录（默认 qq-workspace/课程表）",
    )
    ap.add_argument(
        "--include-past-days",
        type=int,
        default=0,
        help="额外保留今天之前几天的课程（默认 0，即删掉今天之前的）",
    )
    args = ap.parse_args()

    src_path = Path(args.ics).expanduser()
    if not src_path.exists():
        print(f"找不到文件：{src_path}", file=sys.stderr)
        return 1

    all_classes = parse_ics(src_path)
    if not all_classes:
        print("没有解析到任何课程", file=sys.stderr)
        return 1

    today = datetime.now().date() - timedelta(days=max(0, args.include_past_days))
    kept = [c for c in all_classes if c["start"].date() >= today]
    dropped = len(all_classes) - len(kept)

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    render_json(kept, src_path.name, out_dir / "schedule.json")
    render_md(kept, src_path.name, out_dir / "schedule.md")
    render_ics(kept, out_dir / "未来课程.ics")

    print(f"源事件展开后共 {len(all_classes)} 节")
    print(f"删除今天之前的 {dropped} 节，保留 {len(kept)} 节")
    if kept:
        print(f"范围：{kept[0]['start']:%Y-%m-%d} ~ {kept[-1]['start']:%Y-%m-%d}")
    print(f"输出目录：{out_dir.resolve()}")
    print("  - schedule.json   （桥接读取）")
    print("  - schedule.md     （人看）")
    print("  - 未来课程.ics    （可导入日历）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
