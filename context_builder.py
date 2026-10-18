#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
输入层：给模型拼一个轻量情景头，让它知道「什么时候、在哪个渠道、对方是谁、
刚才聊了什么」。

不采用 system 注入（每次请求都要重发，太贵），而是拼进同一条 user message。
"""

from __future__ import annotations

import re
from datetime import datetime

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def time_context(now: datetime | None = None) -> str:
    """返回「2026-02-14 23:12 周六」这样的可读时间。"""
    now = now or datetime.now()
    return f"{now:%Y-%m-%d %H:%M} {_WEEKDAYS[now.weekday()]}"


def _norm(text: str) -> str:
    """归一化文本，用于和自己发过的内容比对。"""
    return re.sub(r"\s+", "", text or "")


def build_context_message(
    user_text: str,
    history: list[dict] | None = None,
    own_sent: list[str] | None = None,
    partner_name: str = "",
    history_limit: int = 6,
    now: datetime | None = None,
) -> str:
    """拼装投递给 DSH 的完整 user message。

    结构：
        [情景头]
        （对话框里的最近几条，标好谁说的）
        [当前这句]

    情景头带有「不必汇报」的说明，避免模型把时间当成需要回应的内容。
    """
    lines: list[str] = []

    head = f"[{time_context(now)}，QQ 私聊"
    if partner_name:
        head += f"，对方备注：{partner_name}"
    head += "]"
    lines.append(head)
    lines.append("（以上只是背景，不必在回复里提到时间或来源）")

    # ── 最近几条对话 ──
    if history:
        own_norm = [_norm(x) for x in (own_sent or []) if x]
        recent: list[tuple[str, str]] = []
        for msg in history[-(history_limit + 4) :]:
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            is_self = bool(msg.get("is_self"))
            # 自己发的：在 own_sent 里能对上的标成「我」，其余跳过
            if is_self:
                norm = _norm(text)
                if own_norm and not any(
                    norm[:20] == o[:20] or o[:20] in norm for o in own_norm
                ):
                    continue
                recent.append(("我", text))
            else:
                recent.append(("对方", text))

        # 去掉最后一条（就是当前这句）
        if recent and recent[-1][1] == user_text.strip():
            recent.pop()

        recent = recent[-history_limit:]
        if recent:
            lines.append("")
            lines.append("【最近对话】")
            for who, text in recent:
                short = text.replace("\n", " ")
                if len(short) > 120:
                    short = short[:120] + "…"
                lines.append(f"{who}: {short}")

    # ── 当前这句 ──
    lines.append("")
    lines.append("【对方刚说】")
    lines.append(user_text.strip())

    return "\n".join(lines)
