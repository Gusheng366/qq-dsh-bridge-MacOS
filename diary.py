#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进阶 1 · 日记

给她一个文件夹，剩下的她自己来。

设计原则（来自文档）：
  - 不要搞数据库，就用 .jsonl，一行一条，能双击打开、能 grep、能手改
  - 只给她「写一条」和「看最近几条」，不给删除和修改
  - 别替她写：不规定今天写什么，只规定一天最多几条
  - 别让她固定时间写，让她在"想说点什么"的时候写
  - 工具描述里不要出现"你应该记录今天发生的事"

文件布局：
  qq-workspace/日记/diary.jsonl      主日记（一行一条 JSON）
  qq-workspace/日记/README.txt       给人看的说明
"""

from __future__ import annotations

import json
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

MAX_PER_DAY_DEFAULT = 3

_DIARY_HEADER = """这是「日记」文件夹。

diary.jsonl 里每一行是一条日记，格式：
{"时间":"2026-10-11 03:20","心情":"还行","内容":"他今天没来"}

可以直接用文本编辑器打开看，也可以 grep 搜索：
  grep 梨 diary.jsonl

备份就是复制这一个文件。
"""


class Diary:
    def __init__(self, base_dir: Path, max_per_day: int = MAX_PER_DAY_DEFAULT):
        self.dir = Path(base_dir) / "日记"
        self.file = self.dir / "diary.jsonl"
        self.max_per_day = max_per_day
        self._ensure()

    def _ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if not self.file.exists():
            self.file.touch()
        readme = self.dir / "README.txt"
        if not readme.exists():
            readme.write_text(_DIARY_HEADER, "utf-8")

    # ── 读 ──────────────────────────────────────────────
    def recent(self, limit: int = 5) -> list[dict]:
        """最近 limit 条，按时间正序返回。"""
        entries = self._load_all()
        return entries[-limit:] if limit > 0 else []

    def _load_all(self) -> list[dict]:
        if not self.file.exists():
            return []
        entries: list[dict] = []
        try:
            for line in self.file.read_text("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("内容"):
                    entries.append(obj)
        except OSError:
            return []
        return entries

    def count_today(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        return sum(1 for e in self._load_all() if str(e.get("时间", "")).startswith(today))

    def can_write(self) -> bool:
        return self.count_today() < self.max_per_day

    # ── 写 ──────────────────────────────────────────────
    def append(self, content: str, mood: str = "") -> dict | None:
        """追加一条。返回写入的记录；超出当日上限或内容为空时返回 None。"""
        content = (content or "").strip()
        if not content:
            return None
        if not self.can_write():
            return None
        entry = {
            "时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "心情": (mood or "").strip(),
            "内容": content[:400],
        }
        try:
            with open(self.file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
        except OSError:
            return None
        return entry

    # ── 给模型看的 ──────────────────────────────────────
    def format_recent(self, limit: int = 5) -> str:
        entries = self.recent(limit)
        if not entries:
            return ""
        lines = []
        for e in entries:
            t = str(e.get("时间", ""))[5:]  # 去掉年份
            mood = e.get("心情") or ""
            mood_part = f"（{mood}）" if mood else ""
            lines.append(f"{t} {mood_part}{e.get('内容', '')}")
        return "\n".join(lines)

    def diary_prompt_snippet(self) -> str:
        """注入到 prompt 里的日记提示。只在"可以写"的时候提示，且语气不像任务。"""
        if not self.can_write():
            return ""
        quota = self.max_per_day - self.count_today()
        recent = self.format_recent(3)
        parts = [
            "【日记】",
            f"今天还可以写 {quota} 条。想写就写，不想写就算了，不用勉强。",
            "写的时候只写一句，不要说\"今天真是充实的一天\"这种总结，",
            "也不要汇报\"主人问了我什么问题\"。像潦草记一笔那样。",
        ]
        if recent:
            parts.append("最近写的：")
            parts.append(recent)
        parts.append(
            "如果要写，在回复最后单独一行写：DIARY: 心情|内容"
            "（心情可以空着，写成 DIARY: |那只梨没回来）"
        )
        return "\n".join(parts)


def parse_diary_from_reply(reply: str) -> tuple[str, dict | None]:
    """从回复里提取 DIARY: 行，返回（清理后的回复，日记内容）。

    模型可能写成：
        DIARY: 还行|那只梨没回来
        diary: |今天很热
        【DIARY】心情：还行 内容：那只梨没回来
    """
    if not reply:
        return reply, None

    # DIARY 可能出现在行首，也可能跟在正文后面（同一行）
    marker = re.compile(r"(?i)\bDIARY\s*[:：]|【DIARY】")
    m = marker.search(reply)
    if not m:
        return reply, None

    body = reply[m.end():].strip()
    # 日记体只占这一行，取到换行为止
    body = body.splitlines()[0].strip() if body else ""
    head = reply[: m.start()].strip()

    if not body:
        return head, None

    mood, _, content = body.partition("|")
    if not content:
        mood, content = "", body
    content = re.sub(r"(内\s*容)\s*[:：]", "", content).strip()
    mood = re.sub(r"(心\s*情)\s*[:：]", "", mood).strip()
    # 去掉可能残留的引号
    content = content.strip("\"'\u201c\u201d「」")

    if not content:
        return head, None
    return head, {"mood": mood[:20], "content": content[:400]}
