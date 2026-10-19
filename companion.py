#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进阶 5 · 让她自己决定今天想干什么

四件事：
  ① 主动找你 —— 随机间隔检查，安静够久才有概率发
  ② 记「印象」—— 短，几个字，定期重写
  ③ 图库 —— 她自己挑图并写理由
  ④ 时间感 —— 知道几点、上次说话多久前、今天周几

设计要点（来自文档）：
  - 检查间隔必须随机，固定间隔会像闹钟
  - 正在聊天时不打断（安静够久才考虑）
  - 印象要短，长了就是小作文
  - 时间感不用写代码，注入提示即可
"""

from __future__ import annotations

import json
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ── ④ 时间感 ────────────────────────────────────────────────
def time_feel(last_user_time: datetime | None, now: datetime | None = None) -> str:
    """生成「现在几点 / 距上次说话多久 / 今天周几」的提示。"""
    now = now or datetime.now()
    hour = now.hour
    if 0 <= hour < 5:
        period = "凌晨"
    elif hour < 9:
        period = "早上"
    elif hour < 12:
        period = "上午"
    elif hour < 14:
        period = "中午"
    elif hour < 18:
        period = "下午"
    elif hour < 23:
        period = "晚上"
    else:
        period = "深夜"

    bits = [f"现在是{period} {now:%H:%M}，{_WEEKDAYS[now.weekday()]}"]

    if last_user_time:
        delta = now - last_user_time
        mins = int(delta.total_seconds() // 60)
        if mins < 1:
            rel = "刚刚"
        elif mins < 60:
            rel = f"{mins} 分钟前"
        elif mins < 60 * 24:
            hours = mins // 60
            rel = f"{hours} 小时前"
        else:
            days = mins // (60 * 24)
            rel = f"{days} 天前"
        bits.append(f"上次说话是{rel}")

    return "；".join(bits) + "。你会自然注意到这些，但不必特意说出来。"


# ── ② 印象 ──────────────────────────────────────────────────
class Impressions:
    """对某个人的印象：几个字一条，可定期重写。"""

    def __init__(self, base_dir: Path, target: str):
        self.dir = Path(base_dir) / "印象"
        self.dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w\u4e00-\u9fff-]", "_", target)[:40]
        self.file = self.dir / f"{safe}.json"
        self.target = target

    def load(self) -> dict:
        if not self.file.exists():
            return {"target": self.target, "updated": "", "traits": []}
        try:
            data = json.loads(self.file.read_text("utf-8"))
            if isinstance(data, dict):
                data.setdefault("traits", [])
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"target": self.target, "updated": "", "traits": []}

    def save(self, traits: list[str], note: str = "") -> None:
        clean = []
        for t in traits:
            t = str(t).strip()[:30]
            if t and t not in clean:
                clean.append(t)
        clean = clean[:8]
        payload = {
            "target": self.target,
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "traits": clean,
        }
        if note:
            payload["note"] = note[:200]
        try:
            self.file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), "utf-8"
            )
        except OSError:
            pass

    def format(self) -> str:
        data = self.load()
        traits = data.get("traits") or []
        if not traits:
            return ""
        return "、".join(traits)

    def prompt_snippet(self) -> str:
        text = self.format()
        if not text:
            return ""
        return (
            f"【你对{self.target}的印象】{text}\n"
            "印象是你自己形成的判断，别当成事实去核对。"
        )


def parse_impression_from_reply(reply: str) -> tuple[str, list[str] | None]:
    """从回复中提取 IMPRESSION: 行。"""
    if not reply:
        return reply, None
    marker = re.compile(r"(?i)\bIMPRESSION\s*[:：]|【IMPRESSION】")
    m = marker.search(reply)
    if not m:
        return reply, None
    body = reply[m.end():].strip().splitlines()[0].strip() if reply[m.end():].strip() else ""
    head = reply[: m.start()].strip()
    if not body:
        return head, None
    parts = re.split(r"[、,，;；|]", body)
    traits = [p.strip().strip("\"\u201c\u201d") for p in parts if p.strip()]
    return head, (traits or None)


# ── ③ 图库 ──────────────────────────────────────────────────
class Gallery:
    """她自己的相册：她自己决定往里放什么，并写理由。"""

    def __init__(self, base_dir: Path):
        self.dir = Path(base_dir) / "图库"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index = self.dir / "index.jsonl"

    def add(self, title: str, reason: str, source: str = "") -> dict:
        entry = {
            "时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "标题": (title or "").strip()[:60],
            "理由": (reason or "").strip()[:200],
            "来源": (source or "").strip()[:200],
        }
        try:
            with open(self.index, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
        except OSError:
            return {}
        return entry

    def all(self) -> list[dict]:
        if not self.index.exists():
            return []
        out = []
        try:
            for line in self.index.read_text("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
        except OSError:
            pass
        return out

    def format(self, limit: int = 8) -> str:
        items = self.all()[-limit:]
        if not items:
            return ""
        return "\n".join(
            f"- {i.get('标题', '')}（{i.get('理由', '')}）" for i in items
        )


def parse_gallery_from_reply(reply: str) -> tuple[str, dict | None]:
    """从回复中提取 GALLERY: 标题|理由 行。"""
    if not reply:
        return reply, None
    marker = re.compile(r"(?i)\bGALLERY\s*[:：]|【GALLERY】")
    m = marker.search(reply)
    if not m:
        return reply, None
    body = reply[m.end():].strip().splitlines()[0].strip() if reply[m.end():].strip() else ""
    head = reply[: m.start()].strip()
    if not body:
        return head, None
    title, _, reason = body.partition("|")
    title = title.strip().strip("\"\u201c\u201d「」")
    if not title:
        return head, None
    return head, {"title": title[:60], "reason": reason.strip()[:200]}


# ── ① 主动找你 ──────────────────────────────────────────────
class Proactive:
    """主动找人聊天的调度器。

    参数（文档参考值）：
      开关            开
      多久检查一次     45 ~ 110 分钟（随机）
      安静多久才考虑    15 分钟
      每次真发的概率    0.35
      深夜打折         不打折
    """

    def __init__(
        self,
        enabled: bool = True,
        check_min_minutes: float = 45,
        check_max_minutes: float = 110,
        idle_minutes: float = 15,
        probability: float = 0.35,
        quiet_hours: str = "",
    ):
        self.enabled = enabled
        self.check_min = check_min_minutes * 60
        self.check_max = check_max_minutes * 60
        self.idle_seconds = idle_minutes * 60
        self.probability = probability
        self.quiet_hours = quiet_hours
        self._next_check_at = 0.0

    def _schedule_next(self, now_ts: float) -> None:
        self._next_check_at = now_ts + random.uniform(self.check_min, self.check_max)

    def _in_quiet(self, now: datetime) -> bool:
        if not self.quiet_hours:
            return False
        try:
            start_s, end_s = self.quiet_hours.split("-")
            sh, sm = map(int, start_s.split(":"))
            eh, em = map(int, end_s.split(":"))
            t = now.time()
            start = t.replace(hour=sh, minute=sm, second=0, microsecond=0)
            end = t.replace(hour=eh, minute=em, second=0, microsecond=0)
            if start <= end:
                return start <= t <= end
            return t >= start or t <= end
        except Exception:
            return False

    def should_fire(
        self, now_ts: float, last_activity_ts: float, partner_is_typing: bool = False
    ) -> bool:
        """该不该现在主动发一条。

        注意：调用方应在"没有待处理消息"时才调这个。
        """
        if not self.enabled:
            return False
        if self._next_check_at == 0.0:
            self._schedule_next(now_ts)
            return False
        if now_ts < self._next_check_at:
            return False

        # 到点了，先排下一次（无论这次发不发）
        self._schedule_next(now_ts)

        # 刚聊完就不打扰
        if now_ts - last_activity_ts < self.idle_seconds:
            return False
        if partner_is_typing:
            return False

        now = datetime.fromtimestamp(now_ts)
        if self._in_quiet(now):
            return False

        return random.random() < self.probability

    def prompt_snippet(self) -> str:
        return (
            "【你现在可以主动说点什么】\n"
            "没人跟你说话，你想起一些事。想找他聊就聊一句，不想就什么也别做。\n"
            "如果要发，直接写要说的话；不想发就只回复 SILENT 四个字母。\n"
            "别提「主动」「打扰」这类词，像自然想起他一样开口。"
        )
