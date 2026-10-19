#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""课程表：让聊天对象知道「几号有课」。

数据由 `schedule_import.py` 从 ICS 生成，落在 `qq-workspace/课程表/schedule.json`。
本模块只做读取与摘要，不改数据。

注入策略（每轮对话都会带上，所以要短）：
  - 今天的课（标出已上 / 还没上）
  - 明天的课
  - 下次上课是哪天
  - 未来 7 天里有课的日子

约 200~300 字，够 AI 自然地接话（「你今天下午还有课呢」），
又不会像日程助手那样把整张表念一遍。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


class Schedule:
    def __init__(self, base_dir: Path):
        self.path = Path(base_dir) / "课程表" / "schedule.json"
        self._data: dict | None = None
        self._loaded_at = 0.0

    # ── 加载 ──────────────────────────────────────
    def load(self, force: bool = False) -> bool:
        """读 schedule.json。文件变了会自动重载（按 mtime）。"""
        if not self.path.exists():
            self._data = None
            return False
        try:
            mtime = self.path.stat().st_mtime
            if not force and self._data is not None and mtime == self._loaded_at:
                return True
            self._data = json.loads(self.path.read_text("utf-8"))
            self._loaded_at = mtime
            return True
        except (OSError, json.JSONDecodeError):
            self._data = None
            return False

    @property
    def ok(self) -> bool:
        return self.load() and bool(self._data)

    def _classes(self) -> list[dict]:
        if not self.ok:
            return []
        return self._data.get("classes", [])  # type: ignore[union-attr]

    # ── 查询 ──────────────────────────────────────
    def classes_on(self, d: date) -> list[dict]:
        key = d.strftime("%Y-%m-%d")
        return [c for c in self._classes() if c.get("date") == key]

    def next_class(self, now: datetime | None = None) -> dict | None:
        """下一次还没开始的课。"""
        now = now or datetime.now()
        for c in self._classes():
            try:
                start = datetime.strptime(f"{c['date']} {c['start']}", "%Y-%m-%d %H:%M")
            except (KeyError, ValueError):
                continue
            if start > now:
                return c
        return None

    def days_with_class(self, start: date, days: int) -> list[tuple[date, int]]:
        """[start, start+days) 内有课的日子与节数。"""
        out: list[tuple[date, int]] = []
        for i in range(days):
            d = start + timedelta(days=i)
            n = len(self.classes_on(d))
            if n:
                out.append((d, n))
        return out

    # ── 摘要 ──────────────────────────────────────
    @staticmethod
    def _fmt_class(c: dict) -> str:
        """一行课的骨架：10:00-11:40 广域承载网架构与技术（明辨楼406）。"""
        base = f"{c.get('start', '')}-{c.get('end', '')} {c.get('name', '')}"
        if c.get("room"):
            base += f"（{c['room']}）"
        return base

    @staticmethod
    def _state_of(c: dict, now: datetime) -> str:
        """这一节相对「现在」的状态。

        必须把「下课时间」讲清楚——否则模型会拿后面那节课的时间当现在，
        例如上午还在上课却回成下午的下课时间。
        """
        try:
            start = datetime.strptime(f"{c['date']} {c['start']}", "%Y-%m-%d %H:%M")
            end = datetime.strptime(f"{c['date']} {c['end']}", "%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            return ""
        if now >= end:
            return "已下课"
        if start <= now < end:
            left = int((end - now).total_seconds() // 60)
            hint = f"（还有 {left} 分钟）" if left > 0 else ""
            return f"★正在上，{c['end']} 下课{hint}"
        return "还没开始"

    def prompt_snippet(self, now: datetime | None = None) -> str:
        """生成注入到对话里的课表摘要。没有数据时返回空串。"""
        if not self.ok:
            return ""
        now = now or datetime.now()
        today = now.date()

        lines: list[str] = [f"现在 {now:%H:%M}"]

        # 今天
        todays = self.classes_on(today)
        if todays:
            lines.append(
                f"今天（{today.month}月{today.day}日 {WEEKDAYS[today.weekday()]}）{len(todays)}节："
            )
            for c in todays:
                state = self._state_of(c, now)
                item = "  " + self._fmt_class(c)
                if state:
                    item += f" —— {state}"
                lines.append(item)
        else:
            lines.append("今天没课")

        # 明天
        tomorrow = today + timedelta(days=1)
        tmr = self.classes_on(tomorrow)
        if tmr:
            lines.append(
                f"明天（{tomorrow.month}月{tomorrow.day}日 {WEEKDAYS[tomorrow.weekday()]}）{len(tmr)}节："
            )
            for c in tmr:
                lines.append("  " + self._fmt_class(c))

        # 下次上课
        nxt = self.next_class(now)
        if nxt:
            d = datetime.strptime(nxt["date"], "%Y-%m-%d")
            delta = (d.date() - today).days
            when = (
                "今天"
                if delta == 0
                else ("明天" if delta == 1 else f"{d.month}月{d.day}日{WEEKDAYS[d.weekday()]}")
            )
            lines.append(f"下一节：{when} {nxt['start']}-{nxt['end']} {nxt['name']}")
        elif not todays and not tmr:
            lines.append("近期没有课了")

        # 未来 7 天
        week = self.days_with_class(today, 7)
        week = [(d, n) for d, n in week if d != today and d != tomorrow]
        if week:
            parts = [f"{d.month}/{d.day}{WEEKDAYS[d.weekday()]}({n}节)" for d, n in week]
            lines.append("这周还有课：" + "、".join(parts))

        if not lines:
            return ""

        return (
            "【课表】\n"
            + "\n".join(lines)
            + "\n★几条规矩：每节课都有开始和下课时间；说「现在」时要看清哪一节"
            "正在上（★标记）、几点下课，不要把后面那节的时间当成现在。"
            "这些你心里有数就行，别像日程助手那样汇报，也别主动念整张表。"
        )
