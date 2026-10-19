#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
输出后处理：把「助手腔」的模型输出洗成「聊天腔」。

设计原则是白名单心态 —— 允许什么流出去，而不是屏蔽什么。
宁可留一点口语的不完美，也不要把它修成教科书。
"""

from __future__ import annotations

import re

# ── 客套话（整句丢弃）────────────────────────────────────────
_POLITE_HEAD_PATTERNS = [
    r"^当然可以[，,。！!]?",
    r"^当然[，,。！!]?",
    r"^好的[，,]\s*",
    r"^好的[。！!]?$",
    r"^(我|让我)?(来|帮你)?(看看|分析|解释|说明)(一下)?[。，,]\s*",
    r"^这个问题[，,]?",
    r"^关于这个问题[，,]?",
    r"^(首先|其次|另外|此外|最后)[，,]",
    r"^(需要说明的是|值得一提的是)[，,]?",
    r"^(简单来说|总的来说|总结一下)[，,:：]?",
]

_POLITE_TAIL_PATTERNS = [
    r"[。！!，,]?\s*希望(对你|这)?有帮助[。！!~～]*$",
    r"[。！!，,]?\s*还需要(我)?(帮你)?(做|提供)什么吗[?？~～]*$",
    r"[。！!，,]?\s*有(什么)?问题(随时)?(问我|找我)[。！!~～]*$",
    r"[。！!，,]?\s*(如|如果)有(任何)?(其他)?(问题|需要)[^。！？]*$",
    r"[。！!，,]?\s*你(还)?(想|要)?了解(些)?什么[?？~～]*$",
    r"[。！!，,]?\s*随时告诉我[。！!~～]*$",
]

# ── 自我暴露（整条丢弃，触发重新生成）────────────────────────
_AI_SELF_DISCLOSURE = re.compile(
    r"(作为一个\s*(AI|人工智能|语言模型|助手|智能体))"
    r"|(我是(一个)?(AI|人工智能|语言模型|DeepSeek|助手))"
    r"|(作为\s*DeepSeek)"
    r"|(我的训练数据)"
    r"|(我没有(情感|感情|意识|身体))",
    re.IGNORECASE,
)

# ── Markdown 清洗 ─────────────────────────────────────────────
_RE_CODE_FENCE = re.compile(r"```[\s\S]*?```")
_RE_INLINE_CODE = re.compile(r"`([^`]*)`")
_RE_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_RE_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_RE_HEADING = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_RE_LIST_MARKER = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_RE_ORDERED_MARKER = re.compile(r"^\s*\d+[.、)]\s+", re.MULTILINE)
_RE_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_RE_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", re.MULTILINE)
_RE_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_RE_HR = re.compile(r"^\s*[-*_]{3,}\s*$", re.MULTILINE)


def strip_markdown(text: str) -> str:
    """去掉 markdown 结构，只留文字。"""
    s = text
    s = _RE_CODE_FENCE.sub("", s)
    s = _RE_INLINE_CODE.sub(r"\1", s)
    s = _RE_BOLD.sub(r"\1", s)
    s = _RE_ITALIC.sub(r"\1", s)
    s = _RE_HEADING.sub("", s)
    s = _RE_TABLE_SEP.sub("", s)
    s = _RE_TABLE_ROW.sub("", s)
    s = _RE_LIST_MARKER.sub("", s)
    s = _RE_ORDERED_MARKER.sub("", s)
    s = _RE_LINK.sub(r"\1", s)
    s = _RE_HR.sub("", s)
    return s


def strip_polite(text: str) -> str:
    """去掉开头的客套与结尾的追问。"""
    s = text.strip()

    # 头部：反复剥离，直到没有可剥的
    for _ in range(3):
        before = s
        for pat in _POLITE_HEAD_PATTERNS:
            s = re.sub(pat, "", s, count=1).strip()
        if s == before:
            break

    # 尾部：切成句子处理
    sentences = re.split(r"(?<=[。！？!?~～\n])", s)
    sentences = [x for x in sentences if x.strip()]
    changed = True
    while changed and sentences:
        changed = False
        last = sentences[-1].strip()
        for pat in _POLITE_TAIL_PATTERNS:
            if re.search(pat, last):
                sentences.pop()
                changed = True
                break
    s = "".join(sentences).strip()

    # 兜底：如果整句就是"好的"/"当然可以"
    if s in ("好的", "当然可以", "当然", "明白了", "理解"):
        s = ""
    return s


# ── 推理泄漏（思考草稿混进正文）────────────────────────────
# 表现：中文回复后面紧跟一大段英文思考，或出现自我对话痕迹：
#   "我哪看得见图 就一个占位 Hmm, but instruction says don't say ..."
_REASONING_MARKERS = re.compile(
    r"(?i)"
    r"\b(?:hmm+|wait|actually|let me|i should|i need to|i think|"
    r"instruction says|the instruction|but instruction|"
    r"on second thought|okay so|alright so|let's see|"
    r"that said|however,|alternatively|i could|i'll just|"
    r"maybe i|perhaps i|trying to|need to be careful)\b"
)

# 英文句子里夹着中文指令引号，是典型的"复述规则"泄漏
_REASONING_QUOTE = re.compile(r"[\"“「'].{0,80}?(?:不要|别说|说|问|解释).{0,80}?[\"”」']")


def _english_word_ratio(text: str) -> float:
    if not text:
        return 0.0
    letters = sum(1 for ch in text if ch.isalpha())
    ascii_letters = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if letters == 0:
        return 0.0
    return ascii_letters / letters


def strip_reasoning(text: str) -> str:
    """剥掉混入正文的推理草稿。

    策略：按句子切，从第一个"看起来像英文思考"的句子开始，
    把后面的全部丢掉——真正的聊天回复不会在中途切成英文自言自语。
    """
    if not text:
        return ""

    # 按中英文句末与换行切句，保留分隔符
    parts = re.split(r"(?<=[。！？!?~～\n])", text)
    parts = [p for p in parts if p]

    keep: list[str] = []
    for part in parts:
        segment = part.strip()
        if not segment:
            continue

        # 该句英文占比很高，且出现思考标记 → 从这里断开
        ratio = _english_word_ratio(segment)
        if ratio > 0.5 and _REASONING_MARKERS.search(segment):
            break
        # 出现"复述规则"式的引号内容
        if ratio > 0.3 and _REASONING_QUOTE.search(segment):
            break
        # 整句几乎全是英文（不是中英混排的短词），也视为泄漏
        if ratio > 0.85 and len(segment) > 12:
            break

        keep.append(part)

    result = "".join(keep).strip()
    return result


def is_self_disclosure(text: str) -> bool:
    """是否暴露了 AI 身份。"""
    return bool(_AI_SELF_DISCLOSURE.search(text))


def clean_reply(raw: str) -> str:
    """完整清洗流程。返回空串表示这条不该发。"""
    if not raw or not raw.strip():
        return ""

    s = strip_markdown(raw)
    s = strip_reasoning(s)
    if not s.strip():
        return ""
    # 折叠多余空白（但保留中文标点）
    s = re.sub(r"[ \t]+", " ", s)
    # 聊天不做多行：换行压平成句子
    s = re.sub(r"\n{2,}", "。", s)
    s = re.sub(r"\n", "，", s)
    # 标点冗余清理：冒号/逗号/句号后紧跟逗号
    s = re.sub(r"([：:，,、])\s*[，,]", r"\1", s)
    s = re.sub(r"([。！？!?])\s*[，,]", r"\1", s)
    s = re.sub(r"^[，,。！？!?、：:]+", "", s)
    s = s.strip()

    if is_self_disclosure(s):
        return ""

    s = strip_polite(s)
    if not s:
        return ""

    # 去掉包裹用的引号
    if len(s) >= 2 and s[0] in "“\"「" and s[-1] in "”\"」":
        s = s[1:-1].strip()

    return s


# ── 拆条 ──────────────────────────────────────────────────────
_SENTENCE_END = re.compile(r"(?<=[。！？!?~～])")


def split_messages(text: str, max_chars: int = 60, max_parts: int = 3) -> list[str]:
    """把一段回复拆成 1~max_parts 条短消息。

    规则：
      - 短于 max_chars 且不含多个句子 → 原样一条
      - 否则按句末标点切，逐句装箱到不超过 max_chars
      - 超过 max_parts 时把剩余合并，避免刷屏
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    # 先按换行分段，再按句末切
    pieces: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        pieces.extend([p for p in _SENTENCE_END.split(line) if p.strip()])

    if not pieces:
        return [text[:max_chars]]

    parts: list[str] = []
    cur = ""
    for p in pieces:
        if cur and len(cur) + len(p) > max_chars:
            parts.append(cur)
            cur = p
        else:
            cur += p
    if cur:
        parts.append(cur)

    # 超长单句：硬切
    final: list[str] = []
    for p in parts:
        while len(p) > max_chars * 1.5:
            final.append(p[:max_chars])
            p = p[max_chars:]
        if p.strip():
            final.append(p)

    # 条数控制：把尾部合并
    if len(final) > max_parts:
        head = final[: max_parts - 1]
        tail = "".join(final[max_parts - 1 :])
        final = head + [tail]

    return [x.strip() for x in final if x.strip()]
