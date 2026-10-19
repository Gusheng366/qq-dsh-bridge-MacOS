#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ-Desktop ↔ DeepSeek Harness 桥接
读取：纯 Accessibility API，全程不切屏
发送：短暂激活 QQ → 写入+回车 → 立即还原原前台应用（约 1~1.5 秒）

安全约束：
  - 只在备注为 QQ_TARGET 指定的那一个会话内工作，其它会话一律不碰
  - 打字保护：当前应用有输入焦点时延后发送
  - 发送频率受限
  - 支持纯读模式（QQ_READ_ONLY=1）

配置见 .env.example / README.md 的「配置」一节。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import random
import re
import signal
import subprocess
import threading
import uuid

import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

from dsh_api import DshApi
from companion import (
    Gallery,
    Impressions,
    Proactive,
    parse_gallery_from_reply,
    parse_impression_from_reply,
    time_feel,
)
from context_builder import build_context_message
from diary import Diary, parse_diary_from_reply
from vision import (
    ScreenshotStore,
    build_image_hint,
    capture_window,
    has_image_placeholder,
)
from reply_style import clean_reply, split_messages
from schedule import Schedule

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent


def _load_env_file() -> None:
    """加载 BASE_DIR/.env（若存在）。已存在的环境变量优先。"""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text("utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # 空值不覆盖：允许 .env 里留空表示“用默认”
            if key and value and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_env_file()

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
LOG_FILE = DATA_DIR / "bridge.log"
STATE_FILE = DATA_DIR / "state.json"
SID_FILE = DATA_DIR / "dsh-session.txt"
PID_FILE = DATA_DIR / "bridge.pid"

TARGET_REMARK = os.environ.get("QQ_TARGET", "你的备注名")
POLL_MIN_S = float(os.environ.get("QQ_POLL_MIN", "5"))
POLL_MAX_S = float(os.environ.get("QQ_POLL_MAX", "10"))
SEND_GAP_MIN_S = float(os.environ.get("QQ_SEND_GAP_MIN", "3"))
SEND_GAP_MAX_S = float(os.environ.get("QQ_SEND_GAP_MAX", "5"))
MAX_SENDS_PER_HOUR = int(os.environ.get("QQ_MAX_SENDS_PER_HOUR", "60"))
# QQ 服务端的硬性风控：单聊每分钟不要超过 20 条。宁可自己先刹车，也别等被限速。
MAX_SENDS_PER_MINUTE = int(os.environ.get("QQ_MAX_SENDS_PER_MINUTE", "20"))
# 留一点余量，实际按这个数刹车（超了腾讯会整段不回包）
SEND_LIMIT_MARGIN = int(os.environ.get("QQ_SEND_LIMIT_MARGIN", "18"))
MAX_REPLY_CHARS = int(os.environ.get("QQ_MAX_REPLY_CHARS", "1500"))
DSH_TIMEOUT_S = int(os.environ.get("QQ_DSH_TIMEOUT", "600"))
DSH_BASE_URL = os.environ.get("QQ_DSH_URL", "http://127.0.0.1:3080")
QQ_SESSION_CWD = Path(
    os.environ.get("QQ_SESSION_CWD", str(BASE_DIR / "qq-workspace"))
)
READ_ONLY = os.environ.get("QQ_READ_ONLY", "0") == "1"
# 打字保护：检测到用户正在输入时，最多等待多少秒
# 静默时段 "23:00-08:00"，留空则不启用
QUIET_HOURS = os.environ.get("QQ_QUIET_HOURS", "").strip()
# 拆条：单条最大字数
SPLIT_MAX_CHARS = int(os.environ.get("QQ_SPLIT_MAX_CHARS", "60"))
# 拆条后随机只回第一条的概率（模拟真人在忙）
SHORT_REPLY_PROBABILITY = float(os.environ.get("QQ_SHORT_REPLY_PROB", "0.10"))
# 推理档位：low / high / max，留空则不设置（聊天建议 low，减少"分析腔"）
REASONING_EFFORT = os.environ.get("QQ_REASONING_EFFORT", "low").strip()
# 模型链：主模型在前，失败自动降级；恢复后自动切回。
# 格式 provider/model[:effort]，逗号分隔
MODEL_CHAIN_RAW = os.environ.get(
    "QQ_MODEL_CHAIN",
    "workbuddy/deepseek-v4.1-flash:low,"
    "workbuddy/space-bunny:low,"
    "workbuddy/glm-5.3-flash:low",
)
# 探测某个模型能否出字的最长等待（秒）
MODEL_PROBE_TIMEOUT = float(os.environ.get("QQ_MODEL_PROBE_TIMEOUT", "20"))
# 探测成功后多久内信任（秒）
MODEL_HEALTH_OK_TTL = float(os.environ.get("QQ_MODEL_HEALTH_OK_TTL", "600"))
# 探测失败后多久内不再试它（秒）；过了就用主模型再试一次
MODEL_HEALTH_FAIL_TTL = float(os.environ.get("QQ_MODEL_HEALTH_FAIL_TTL", "600"))
# 后台多久复检一次主模型（秒），用于额度恢复后自动切回
MODEL_REFRESH_INTERVAL_S = float(os.environ.get("QQ_MODEL_REFRESH_INTERVAL", "90"))

# 多久没检测到变化就轻点一下会话项，促使 QQ 重绘 AX 树（秒）
AX_POKE_AFTER_S = float(os.environ.get("QQ_AX_POKE_AFTER", "45"))
# 同一轮里重试读取的次数（AX 树可能刚重建完还没填好）
READ_RETRIES = int(os.environ.get("QQ_READ_RETRIES", "3"))

# 会话轮数上限：超过后自动新建会话并注入摘要，0 表示不限制
MAX_SESSION_TURNS = int(os.environ.get("QQ_MAX_SESSION_TURNS", "40"))
# 投递时附带的历史消息条数
CONTEXT_HISTORY = int(os.environ.get("QQ_CONTEXT_HISTORY", "6"))

# ── 进阶 1：日记 ──────────────────────────────────
DIARY_ENABLED = os.environ.get("QQ_DIARY_ENABLED", "1") == "1"
DIARY_MAX_PER_DAY = int(os.environ.get("QQ_DIARY_MAX_PER_DAY", "3"))

# ── 进阶 5：主动找人 ──────────────────────────────
PROACTIVE_ENABLED = os.environ.get("QQ_PROACTIVE_ENABLED", "1") == "1"
PROACTIVE_CHECK_MIN = float(os.environ.get("QQ_PROACTIVE_CHECK_MIN", "45"))
PROACTIVE_CHECK_MAX = float(os.environ.get("QQ_PROACTIVE_CHECK_MAX", "110"))
PROACTIVE_IDLE_MINUTES = float(os.environ.get("QQ_PROACTIVE_IDLE_MIN", "15"))
PROACTIVE_PROBABILITY = float(os.environ.get("QQ_PROACTIVE_PROB", "0.35"))

# ── 进阶 5：印象与图库 ────────────────────────────
IMPRESSIONS_ENABLED = os.environ.get("QQ_IMPRESSIONS_ENABLED", "1") == "1"
GALLERY_ENABLED = os.environ.get("QQ_GALLERY_ENABLED", "1") == "1"

# 会话列表滞后时，多久去读一次聊天记录做兜底（秒）
DEEP_CHECK_INTERVAL_S = float(os.environ.get("QQ_DEEP_CHECK_INTERVAL", "8"))

# 每条消息发送前的延迟区间（秒）。设成 0 0 就是收到即发
TYPING_DELAY_MIN_S = float(os.environ.get("QQ_TYPING_DELAY_MIN", "0.0"))
TYPING_DELAY_MAX_S = float(os.environ.get("QQ_TYPING_DELAY_MAX", "0.3"))

# 秒回模式：故意慢一拍的触发概率与最长延迟
SLOW_REPLY_PROBABILITY = float(os.environ.get("QQ_SLOW_REPLY_PROB", "0.05"))
SLOW_REPLY_MAX_S = float(os.environ.get("QQ_SLOW_REPLY_MAX", "6"))

# 打字保护：系统空闲时间低于这个秒数才算「你在用电脑」
TYPING_IDLE_THRESHOLD_S = float(os.environ.get("QQ_TYPING_IDLE_SEC", "3"))
# 打字保护最多等多久
TYPING_GUARD_MAX_WAIT_S = float(os.environ.get("QQ_TYPE_GUARD_MAX_WAIT", "20"))

# ── 课程表 ────────────────────────────────────────
SCHEDULE_ENABLED = os.environ.get("QQ_SCHEDULE_ENABLED", "1") == "1"

# ── 图片理解 ──────────────────────────────────────
VISION_ENABLED = os.environ.get("QQ_VISION_ENABLED", "1") == "1"
SCREENSHOT_KEEP_DAYS = int(os.environ.get("QQ_SCREENSHOT_KEEP_DAYS", "2"))

# ----------------------------------------------------------------------------
# Accessibility / CoreGraphics 绑定
# ----------------------------------------------------------------------------
HIS = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
)
CF = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
CG = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
ENC = 0x08000100

CF.CFStringCreateWithCString.restype = ctypes.c_void_p
CF.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
CF.CFStringGetCString.restype = ctypes.c_bool
CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
CF.CFGetTypeID.restype = ctypes.c_ulong
CF.CFGetTypeID.argtypes = [ctypes.c_void_p]
CF.CFStringGetTypeID.restype = ctypes.c_ulong
CF.CFBooleanGetTypeID.restype = ctypes.c_ulong
CF.CFArrayGetTypeID.restype = ctypes.c_ulong
CF.CFNumberGetTypeID.restype = ctypes.c_ulong
CF.CFArrayGetCount.restype = ctypes.c_long
CF.CFArrayGetCount.argtypes = [ctypes.c_void_p]
CF.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
CF.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
CF.CFBooleanGetValue.restype = ctypes.c_bool
CF.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
CF.CFNumberGetValue.restype = ctypes.c_bool
CF.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
CF.CFRelease.restype = None
CF.CFRelease.argtypes = [ctypes.c_void_p]

HIS.AXUIElementCopyAttributeValue.restype = ctypes.c_int
HIS.AXUIElementCopyAttributeValue.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
]
HIS.AXUIElementSetAttributeValue.restype = ctypes.c_int
HIS.AXUIElementSetAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
HIS.AXUIElementCreateApplication.restype = ctypes.c_void_p
HIS.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
HIS.AXUIElementPerformAction.restype = ctypes.c_int
HIS.AXUIElementPerformAction.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
HIS.AXIsProcessTrusted.restype = ctypes.c_bool
HIS.AXUIElementCreateSystemWide.restype = ctypes.c_void_p

CG.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
CG.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
CG.CGEventKeyboardSetUnicodeString.restype = None
CG.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
CG.CGEventPostToPid.restype = None
CG.CGEventPostToPid.argtypes = [ctypes.c_int, ctypes.c_void_p]
CG.CGEventSetFlags.restype = None
CG.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_ulong]

TRUE = ctypes.c_void_p.in_dll(CF, "kCFBooleanTrue")
FALSE = ctypes.c_void_p.in_dll(CF, "kCFBooleanFalse")


def cs(s: str) -> ctypes.c_void_p:
    return CF.CFStringCreateWithCString(None, s.encode("utf-8"), ENC)


def tostr(ref):
    if not ref:
        return None
    tid = CF.CFGetTypeID(ref)
    if tid == CF.CFStringGetTypeID():
        buf = ctypes.create_string_buffer(32768)
        if CF.CFStringGetCString(ref, buf, 32768, ENC):
            return buf.value.decode("utf-8", "replace")
        return None
    if tid == CF.CFBooleanGetTypeID():
        return str(CF.CFBooleanGetValue(ref))
    if tid == CF.CFNumberGetTypeID():
        v = ctypes.c_longlong()
        if CF.CFNumberGetValue(ref, 4, ctypes.byref(v)):
            return str(v.value)
        return None
    if tid == CF.CFArrayGetTypeID():
        n = CF.CFArrayGetCount(ref)
        vals = []
        for i in range(min(n, 5)):
            s = tostr(CF.CFArrayGetValueAtIndex(ref, i))
            if s:
                vals.append(s)
        return "[" + ", ".join(vals) + f"] x{n}"
    return None


def ax_get(el, attr: str):
    out = ctypes.c_void_p()
    err = HIS.AXUIElementCopyAttributeValue(el, cs(attr), ctypes.byref(out))
    return out if err == 0 else None


def ax_str(el, attr: str):
    return tostr(ax_get(el, attr))


def ax_children(el):
    ref = ax_get(el, "AXChildren")
    if not ref:
        return []
    try:
        if CF.CFGetTypeID(ref) != CF.CFArrayGetTypeID():
            return []
    except Exception:
        return []
    return [CF.CFArrayGetValueAtIndex(ref, i) for i in range(CF.CFArrayGetCount(ref))]


# ----------------------------------------------------------------------------
# 日志
# ----------------------------------------------------------------------------
DEBUG_ENABLED = os.environ.get("QQ_DEBUG", "0") == "1"


def log(msg: str, level: str = "INFO") -> None:
    if level == "DEBUG" and not DEBUG_ENABLED:
        return
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} [{level}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
    except OSError:
        pass


def acquire_instance_lock() -> bool:
    """单实例锁：同一时刻只允许一个桥接进程。"""
    import fcntl

    lock_path = DATA_DIR / "bridge.lock"
    try:
        fh = open(lock_path, "a+")
    except OSError:
        return True
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stale = "未知"
        if PID_FILE.exists():
            try:
                stale = PID_FILE.read_text("utf-8").strip()
            except Exception:
                pass
        print(f"❌ 另一个桥接实例已在运行（PID {stale}），本次退出。", flush=True)
        return False
    globals()["_lock_fh"] = fh
    return True


# ----------------------------------------------------------------------------
# 前台应用管理
# ----------------------------------------------------------------------------
def front_app() -> tuple[str, int] | None:
    """返回当前前台应用的 (名称, pid)。"""
    try:
        name_out = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to get name of first process whose frontmost is true',
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        name = name_out.stdout.strip()
        if not name or name_out.returncode != 0:
            return None
        pid_out = subprocess.run(
            ["pgrep", "-x", name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pids = [int(p) for p in pid_out.stdout.split() if p.isdigit()]
        if not pids:
            return None
        return name, pids[0]
    except Exception:
        return None


def activate_app(pid: int) -> bool:
    """激活指定 pid 的应用（不经过 open -a，避免重新启动）。"""
    try:
        out = subprocess.run(
            [
                "osascript",
                "-e",
                f'tell application "System Events" to set frontmost of (first process whose unix id is {pid}) to true',
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.returncode == 0
    except Exception:
        return False


_IOKIT = None


def system_idle_seconds() -> float:
    """系统空闲秒数（最近一次键盘/鼠标输入到现在）。

    IOKit 的 HIDIdleTime 是纳秒。
    """
    global _IOKIT
    try:
        if _IOKIT is None:
            import ctypes.util as _cu

            _IOKIT = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/IOKit.framework/IOKit"
            )
            _IOKIT.IOServiceGetMatchingService.restype = ctypes.c_uint32
            _IOKIT.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
            _IOKIT.IOServiceMatching.restype = ctypes.c_void_p
            _IOKIT.IOServiceMatching.argtypes = [ctypes.c_char_p]
            _IOKIT.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
            _IOKIT.IORegistryEntryCreateCFProperty.argtypes = [
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
            ]
            _IOKIT.IOObjectRelease.restype = ctypes.c_int
            _IOKIT.IOObjectRelease.argtypes = [ctypes.c_uint32]

        service = _IOKIT.IOServiceGetMatchingService(
            0, _IOKIT.IOServiceMatching(b"IOHIDSystem")
        )
        if not service:
            return 0.0
        try:
            # kCFAllocatorDefault=0
            prop = _IOKIT.IORegistryEntryCreateCFProperty(
                service, cs("HIDIdleTime"), None, 0
            )
            if not prop:
                return 0.0
            try:
                v = ctypes.c_longlong(0)
                CF.CFNumberGetValue.restype = ctypes.c_bool
                CF.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
                if CF.CFNumberGetValue(prop, 4, ctypes.byref(v)):  # kCFNumberSInt64Type
                    return v.value / 1e9
            finally:
                CF.CFRelease(prop)
        finally:
            _IOKIT.IOObjectRelease(service)
    except Exception:
        pass
    return 0.0


def user_is_typing(idle_threshold: float = 0.0) -> bool:
    """在「发送会打断你」的意义上，用户是否正在忙。

    只看系统空闲时间：最近几秒内有键入/鼠标移动才算忙。
    """
    threshold = idle_threshold if idle_threshold > 0 else TYPING_IDLE_THRESHOLD_S
    return system_idle_seconds() < threshold


def wait_until_user_idle(max_wait: float = 0.0) -> tuple[bool, float]:
    """等到用户不再操作电脑。

    规则（用户指定）：发送前必须先确认"不在打字、不在移动鼠标"；
    一旦空闲下来就**立刻**发送，不额外拖延。

    - max_wait <= 0 表示一直等到空闲为止（没有上限）
    - 返回 (是否等到, 实际等待秒数)
    """
    started = time.time()
    announced = False
    while _running:
        idle = system_idle_seconds()
        if idle >= TYPING_IDLE_THRESHOLD_S:
            waited = time.time() - started
            if waited > 0.5:
                log(f"  你在忙，已等待 {waited:.1f}s，现在空闲了，立即发送")
            return True, waited
        if not announced:
            log("  检测到你正在用电脑，等你不操作了再发", "INFO")
            announced = True
        if max_wait > 0 and time.time() - started >= max_wait:
            log(f"  等待超时（{max_wait:.0f}s），仍在你操作中，放弃本次发送", "WARN")
            return False, time.time() - started
        time.sleep(0.2)
    return False, time.time() - started


# ----------------------------------------------------------------------------
# QQ 访问
# ----------------------------------------------------------------------------
class QQ:
    def __init__(self) -> None:
        self.pid: int | None = None
        self.app = None
        self.window = None

    def refresh(self) -> bool:
        try:
            pid = int(subprocess.check_output(["pgrep", "-x", "QQ"], text=True).split()[0])
        except Exception:
            self.pid = None
            self.app = None
            self.window = None
            return False

        # 注意：不要切 AXManualAccessibility=FALSE 再开——实测会把 QQ 的
        # 无障碍树彻底关掉，之后连 AXWindows 下的内容都读不出来。
        # 只在首次（或 pid 变化时）创建应用对象，之后复用。
        now = time.time()
        if self.pid != pid or self.app is None:
            self.pid = pid
            self.app = HIS.AXUIElementCreateApplication(pid)
            self._app_born = now

        HIS.AXUIElementSetAttributeValue(self.app, cs("AXManualAccessibility"), TRUE)
        wins = ax_get(self.app, "AXWindows")
        if not wins:
            time.sleep(0.3)
            wins = ax_get(self.app, "AXWindows")
        if not wins:
            self.window = None
            return False
        self.window = CF.CFArrayGetValueAtIndex(wins, 0)
        return True

    def _walk(self, el, predicate, depth=0, max_depth=22):
        if depth > max_depth:
            return None
        try:
            if predicate(el):
                return el
        except Exception:
            pass
        for child in ax_children(el):
            found = self._walk(child, predicate, depth + 1, max_depth)
            if found:
                return found
        return None

    def _collect(self, el, predicate, out, depth=0, max_depth=22):
        if depth > max_depth:
            return
        try:
            if predicate(el):
                out.append(el)
        except Exception:
            pass
        for child in ax_children(el):
            self._collect(child, predicate, out, depth + 1, max_depth)

    @staticmethod
    def _has_class(el, name):
        return name in (ax_str(el, "AXDOMClassList") or "")

    def find_session_item(self, remark: str):
        if self.window is None:
            return None
        items: list = []
        self._collect(self.window, lambda e: self._has_class(e, "recent-contact-item"), items)
        for item in items:
            name = self._session_name(item)
            if name and name.strip() == remark:
                return item
        return None

    def _session_name(self, item) -> str | None:
        holder = self._walk(item, lambda e: self._has_class(e, "item__info"), 0, 8)
        if not holder:
            return None
        for child in ax_children(holder):
            if ax_str(child, "AXRole") == "AXStaticText":
                return ax_str(child, "AXValue")
        return None

    def session_info(self, item) -> dict:
        name = self._session_name(item) or ""
        time_txt = ""
        preview = ""
        holder = self._walk(item, lambda e: self._has_class(e, "item__info"), 0, 8)
        if holder:
            for child in ax_children(holder):
                cls = ax_str(child, "AXDOMClassList") or ""
                if "secondary-info" in cls:
                    for t in ax_children(child):
                        if ax_str(t, "AXRole") == "AXStaticText":
                            time_txt = ax_str(t, "AXValue") or ""
                elif "summary-main" in cls:
                    for t in ax_children(child):
                        if ax_str(t, "AXRole") == "AXStaticText":
                            preview = ax_str(t, "AXValue") or ""
        return {"name": name, "time": time_txt, "preview": preview}

    def open_session(self, item) -> bool:
        for action in ("AXPress", "AXConfirm"):
            if HIS.AXUIElementPerformAction(item, cs(action)) == 0:
                return True
        return False

    def read_chat_messages(self) -> list[str]:
        """兼容接口：只返回文本片段列表。"""
        return [m["text"] for m in self.read_chat_structured()]

    def read_chat_structured(self) -> list[dict]:
        """按消息气泡读取聊天记录，带发送方方向。

        结果形如 [{"is_self": False, "text": "你好", "time": "23:07"}, ...]
        QQ 用 class 标记方向：message-container--self 为自己发的。
        """
        if self.window is None:
            return []
        area = self._walk(self.window, lambda e: self._has_class(e, "chat-msg-area"), 0, 20)
        if not area:
            return []

        items: list = []
        self._collect(area, lambda e: self._has_class(e, "ml-item"), items, 0, 22)

        messages: list[dict] = []
        time_pat = re.compile(r"^\d{1,2}:\d{2}$")

        for item in items:
            is_self = (
                self._walk(item, lambda e: self._has_class(e, "message-container--self"), 0, 6)
                is not None
            )
            texts: list[str] = []
            stamp: list[str] = []

            def grab(el, d=0):
                if d > 14:
                    return
                role = ax_str(el, "AXRole")
                val = ax_str(el, "AXValue")
                if role in ("AXStaticText", "AXTextField") and val and val.strip():
                    segment = val.strip()
                    if time_pat.match(segment):
                        stamp.append(segment)
                    else:
                        texts.append(segment)
                elif role == "AXImage":
                    desc = (ax_str(el, "AXDescription") or "").strip()
                    title = (ax_str(el, "AXTitle") or "").strip()
                    cls = ax_str(el, "AXDOMClassList") or ""
                    if "emoji" in cls.lower() or "sticker" in cls.lower() or "表情" in desc:
                        texts.append("[表情包]")
                    elif desc and desc not in ("图像", "图片", "image"):
                        texts.append(f"[图片：{desc}]")
                    else:
                        texts.append("[图片]")
                elif role == "AXButton":
                    # QQ 的图片/文件消息有时是按钮形式
                    desc = (ax_str(el, "AXDescription") or "").strip()
                    cls = ax_str(el, "AXDOMClassList") or ""
                    if any(k in cls.lower() for k in ("image", "pic", "media", "file")):
                        texts.append(f"[{desc or '图片/文件'}]")
                for c in ax_children(el):
                    grab(c, d + 1)

            grab(item)
            body = "\n".join(texts).strip()
            if not body:
                continue
            messages.append(
                {
                    "is_self": is_self,
                    "text": body,
                    "time": stamp[0] if stamp else "",
                }
            )

        return messages

    def read_chat_last_incoming(self) -> str:
        """轻量读法：只取聊天记录里最后一条「对方」消息的文本。

        用于会话列表摘要滞后时做兜底检测，避免每次都读全量记录。
        """
        if self.window is None:
            return ""
        area = self._walk(
            self.window, lambda e: self._has_class(e, "chat-msg-area"), 0, 20
        )
        if not area:
            return ""
        items: list = []
        self._collect(area, lambda e: self._has_class(e, "ml-item"), items, 0, 22)
        if not items:
            return ""

        import re as _re

        time_pat = _re.compile(r"^\d{1,2}:\d{2}$")
        for item in reversed(items):
            is_self = (
                self._walk(
                    item, lambda e: self._has_class(e, "message-container--self"), 0, 6
                )
                is not None
            )
            if is_self:
                continue
            texts: list[str] = []

            def grab(el, d=0):
                if d > 12:
                    return
                role = ax_str(el, "AXRole")
                if role == "AXStaticText":
                    v = (ax_str(el, "AXValue") or "").strip()
                    if v and not time_pat.match(v):
                        texts.append(v)
                elif role == "AXImage":
                    texts.append("[图片]")
                for c in ax_children(el):
                    grab(c, d + 1)

            grab(item)
            body = "\n".join(texts).strip()
            if body:
                return body
        return ""

    def input_box(self):
        if self.window is None:
            return None
        container = self._walk(self.window, lambda e: self._has_class(e, "msg-input"), 0, 24)
        root = container or self.window
        return self._walk(root, lambda e: ax_str(e, "AXRole") == "AXTextArea", 0, 12)


# ----------------------------------------------------------------------------
# 发送（短暂激活 + 还原）
# ----------------------------------------------------------------------------
def type_via_cg(pid: int, text: str) -> None:
    """用 CGEventPostToPid 投递文本（QQ 已在前台时有效）。"""
    buf = text.encode("utf-16-le")
    for down in (True, False):
        ev = CG.CGEventCreateKeyboardEvent(None, 0, down)
        CG.CGEventKeyboardSetUnicodeString(ev, len(text), ctypes.c_char_p(buf))
        CG.CGEventPostToPid(pid, ev)
        CF.CFRelease(ev)
        time.sleep(0.05)


def press_key(pid: int, keycode: int, flags: int = 0, pause: float = 0.03) -> None:
    """投递单个按键（可带修饰键 flags）。"""
    for down in (True, False):
        ev = CG.CGEventCreateKeyboardEvent(None, keycode, down)
        if flags:
            CG.CGEventSetFlags(ev, flags)
        CG.CGEventPostToPid(pid, ev)
        CF.CFRelease(ev)
        time.sleep(pause)


def clear_input(pid: int) -> None:
    """全选后删除，清空输入框（Cmd+A / Delete）。"""
    CMD = 0x00100000  # kCGEventFlagMaskCommand
    A_KEY = 0  # 'a'
    DELETE_KEY = 51  # delete/backspace
    press_key(pid, A_KEY, CMD, 0.08)
    press_key(pid, DELETE_KEY, 0, 0.05)


def _qq_is_frontmost(qq: "QQ") -> bool:
    """QQ 现在是不是前台应用。

    发送的每一步都依赖这个前提：一旦用户点了别的窗口，
    键盘事件会被系统路由走，消息就发不出去。
    """
    try:
        cur = front_app()
        return bool(cur and cur[1] == qq.pid)
    except Exception:
        return False


def _send_one(qq: "QQ", text: str, max_attempts: int = 3) -> bool:
    """写入一条消息并回车，带竞态重试。

    竞态来源：桥接刚把 QQ 拉到前台，用户恰好点了屏幕，
    焦点被抢走 → 键盘事件投递不到输入框 → 消息丢失。
    处理办法：每次发送前确认 QQ 仍在前台，失败就重新拉前台再试。
    """
    for attempt in range(1, max_attempts + 1):
        # 发送前确认前台
        if not _qq_is_frontmost(qq):
            log(f"  QQ 已不在前台（第 {attempt} 次），重新激活", "WARN")
            if not activate_app(qq.pid):
                time.sleep(0.4)
                continue
            time.sleep(0.45)

        qq.refresh()
        box = qq.input_box()
        if box is None:
            log(f"  找不到输入框（第 {attempt} 次），重试", "WARN")
            time.sleep(0.5)
            continue

        HIS.AXUIElementPerformAction(box, cs("AXPress"))
        time.sleep(0.18)

        # 聚焦之后、输入之前再确认一次（用户点击的高发窗口）
        if not _qq_is_frontmost(qq):
            log(f"  聚焦后被抢走焦点（第 {attempt} 次），重试", "WARN")
            time.sleep(0.3)
            continue

        clear_input(qq.pid)
        time.sleep(0.12)
        type_via_cg(qq.pid, text)
        time.sleep(0.28)

        # 回车前最后确认：如果此时焦点丢了，宁可重来也别发半截
        if not _qq_is_frontmost(qq):
            log(f"  回车前焦点被抢（第 {attempt} 次），清空重试", "WARN")
            try:
                activate_app(qq.pid)
                time.sleep(0.4)
                qq.refresh()
                b2 = qq.input_box()
                if b2 is not None:
                    HIS.AXUIElementPerformAction(b2, cs("AXPress"))
                    time.sleep(0.1)
                    clear_input(qq.pid)
            except Exception:
                pass
            time.sleep(0.3)
            continue

        press_key(qq.pid, 36)  # Return
        time.sleep(0.35)

        # 验证：输入框清空说明已发出
        qq.refresh()
        box2 = qq.input_box()
        residual = (ax_str(box2, "AXValue") or "").strip()
        if residual:
            log(f"  输入框仍有残留（第 {attempt} 次）：{residual[:30]!r}", "WARN")
            time.sleep(0.4)
            continue
        return True

    log(f"  发送失败，{max_attempts} 次尝试均未成功", "ERROR")
    return False


def typing_delay(text: str, split_mode: bool = False) -> float:
    """按字数估算「打字」耗时，带长尾随机。

    基础 4~8 字/秒；多条之间的停顿较短，首次回复前的"读消息"时间较长。
    """
    chars = max(1, len(text))
    # 4~8 字/秒
    cps = random.uniform(4.0, 8.0)
    base = chars / cps

    if split_mode:
        # 条与条之间：秒回模式下压到最小
        delay = max(0.2, min(base, 0.6))
        return random.uniform(delay * 0.7, delay * 1.2)

    # 首次回复：加一点"看到消息"的时间（秒回模式下要小）
    delay = base + random.uniform(0.4, 1.2)
    # 少量概率故意慢一拍，像在忙别的事
    if random.random() < SLOW_REPLY_PROBABILITY:
        delay += random.uniform(3.0, 8.0)
    return min(delay, SLOW_REPLY_MAX_S)


def send_messages(
    qq: "QQ", parts: list[str], rate_guard=None
) -> list[str]:
    """分多条发送，只在开头激活一次 QQ，末尾还原原前台应用。

    rate_guard: 可选回调，每条发送前调用；返回 False 表示应当停止本次批量发送。
    返回实际发送成功的文本列表（供回环防护逐条记录）。
    """
    if not parts:
        return []

    original = front_app()
    sent: list[str] = []

    try:
        if not qq.refresh():
            log("发送前无法定位 QQ", "ERROR")
            return []

        item = qq.find_session_item(TARGET_REMARK)
        if item is None:
            log("发送前找不到目标会话", "ERROR")
            return []
        qq.open_session(item)
        time.sleep(0.2)

        # 只在需要发消息时激活一次
        if not activate_app(qq.pid):
            log("激活 QQ 失败", "ERROR")
            return []
        time.sleep(0.4)

        for idx, part in enumerate(parts):
            # 每条发送前检查频率（QQ 每分钟硬限制）
            if rate_guard is not None and not rate_guard():
                log(f"第 {idx + 1}/{len(parts)} 条因速率限制跳过", "WARN")
                break

            # 每条发送前都重新确认 QQ 在前台（用户可能中途点了别处）
            if not _qq_is_frontmost(qq):
                log(f"第 {idx + 1} 条发送前焦点已丢，重新激活 QQ", "WARN")
                activate_app(qq.pid)
                time.sleep(0.45)

            ok = _send_one(qq, part)
            if ok:
                sent.append(part)
            else:
                log(f"第 {idx + 1}/{len(parts)} 条发送失败", "ERROR")
                if not sent:
                    break

            # 多条之间的模拟打字停顿（最后一条不等）
            if idx < len(parts) - 1:
                pause = typing_delay(part, split_mode=True)
                log(f"  等待 {pause:.1f}s 后发送下一条")
                time.sleep(pause)
    finally:
        if original:
            orig_name, orig_pid = original
            if orig_pid != qq.pid:
                activate_app(orig_pid)
                time.sleep(0.12)

    return sent


def send_reply(qq: "QQ", text: str) -> bool:
    """兼容旧接口：发送单条文本。"""
    return bool(send_messages(qq, [text]))


# ----------------------------------------------------------------------------
# DSH
# ----------------------------------------------------------------------------
def pick_session(api: "DshApi") -> str:
    """返回 QQ 专用会话：已记录且仍存在则复用，否则新建一个独立会话。"""
    if SID_FILE.exists():
        sid = SID_FILE.read_text("utf-8").strip()
        if sid and _session_exists(api, sid):
            log(f"复用 QQ 专用会话: {sid}")
            return sid
        log(f"记录的会话已不存在（{sid}），将新建")

    QQ_SESSION_CWD.mkdir(parents=True, exist_ok=True)
    status, data = api.rpc("session/create", {"request": {"cwd": str(QQ_SESSION_CWD)}})
    ok = (data.get("result") or {}).get("ok")
    if status != 200 or not ok:
        raise RuntimeError(f"创建会话失败: {(data.get('result') or {}).get('error')}")
    sid = data["result"]["value"]["sessionId"]
    # 起一个明确标题，方便在网页里区分
    api.rpc(
        "session/rename",
        {"request": {"sessionId": sid, "title": f"QQ 聊天（{TARGET_REMARK}）"}},
    )
    SID_FILE.write_text(sid, "utf-8")
    log(f"已创建 QQ 专用会话: {sid}")
    return sid


def _session_exists(api: "DshApi", sid: str) -> bool:
    status, data = api.rpc("session/list", {"_request": {}})
    if status != 200:
        return False
    items = (data.get("result") or {}).get("value", {}).get("items") or []
    return any(it.get("sessionId") == sid for it in items)


def _read_turns(api: "DshApi", sid: str) -> list[dict]:
    status, data = api.rpc("session/projections", {"request": {"sessionId": sid}})
    if status != 200:
        return []
    values = ((data.get("result") or {}).get("value") or {}).get("values") or {}
    return values.get("turnOutline") or []


def count_turns(api: "DshApi", sid: str) -> int:
    """当前会话已进行的轮数。"""
    return len(_read_turns(api, sid))


def maybe_rotate_session(
    api: "DshApi", sid: str, max_turns: int, summary_hint: str = ""
) -> str:
    """轮数达到上限时新建会话，并把「之前聊过什么」作为开场白注入。

    返回（可能被替换后的）会话 ID。
    """
    if max_turns <= 0:
        return sid
    turns = count_turns(api, sid)
    if turns < max_turns:
        return sid

    log(f"会话已达 {turns} 轮（上限 {max_turns}），准备新建以控制上下文成本")

    # 取最近几轮作为交接摘要
    outline = _read_turns(api, sid)[-4:]
    snippets: list[str] = []
    for t in outline:
        p = (t.get("prompt") or "").strip().replace("\n", " ")
        r = (t.get("response") or "").strip().replace("\n", " ")
        if not p:
            continue
        snippets.append(f"对方: {p[:60]} / 我: {r[:60]}")
    carry = "；".join(snippets)

    QQ_SESSION_CWD.mkdir(parents=True, exist_ok=True)
    status, data = api.rpc("session/create", {"request": {"cwd": str(QQ_SESSION_CWD)}})
    ok = (data.get("result") or {}).get("ok")
    if status != 200 or not ok:
        log("新建会话失败，继续使用旧会话", "WARN")
        return sid

    new_sid = data["result"]["value"]["sessionId"]
    api.rpc(
        "session/rename",
        {"request": {"sessionId": new_sid, "title": f"QQ 聊天（{TARGET_REMARK}）"}},
    )
    SID_FILE.write_text(new_sid, "utf-8")
    log(f"已切换到新会话: {new_sid}")

    if carry:
        seed = (
            "[背景] 这是你和同一个人的连续聊天，之前聊过这些（不必复述，"
            "只是让你接得上话）：" + carry
        )
        api.rpc(
            "session/prompt",
            {
                "request": {
                    "sessionId": new_sid,
                    "requestId": str(uuid.uuid4()),
                    "mode": "queue",
                    "content": [{"type": "text", "text": seed}],
                }
            },
        )
        log("已注入历史摘要")

    apply_reasoning_effort(api, new_sid)
    return new_sid


def resolve_model(api: "DshApi", sid: str) -> tuple[str, str]:
    """确定会话该用哪个 provider/model：优先现有选择，其次 DSH 默认。"""
    provider, model = "", ""

    status, data = api.rpc("session/projections", {"request": {"sessionId": sid}})
    if status == 200:
        values = ((data.get("result") or {}).get("value") or {}).get("values") or {}
        sel = values.get("modelSelection") or {}
        for key in ("next", "lastUsed"):
            cur = sel.get(key) or {}
            if cur.get("provider") and cur.get("model"):
                provider, model = cur["provider"], cur["model"]
                break

    if not (provider and model):
        status, data = api.rpc("session/modelCatalog", {})
        if status == 200:
            default = ((data.get("result") or {}).get("value") or {}).get("default") or {}
            provider = default.get("provider") or ""
            model = default.get("model") or ""

    return provider, model


def apply_reasoning_effort(api: "DshApi", sid: str) -> None:
    """把会话的 reasoningEffort 调到配置档位，降低「分析腔」。

    selectModel 必须同时给出 provider 与 model，否则会覆盖成默认值。
    """
    if not REASONING_EFFORT:
        return
    provider, model = resolve_model(api, sid)
    if not (provider and model):
        log("无法确定会话模型，跳过 reasoningEffort 设置", "WARN")
        return

    status, data = api.rpc(
        "session/selectModel",
        {
            "request": {
                "sessionId": sid,
                "provider": provider,
                "model": model,
                "reasoningEffort": REASONING_EFFORT,
            }
        },
    )
    ok = (data.get("result") or {}).get("ok")
    if status == 200 and ok:
        selected = ((data.get("result") or {}).get("value") or {}).get("selected") or {}
        log(
            f"会话模型 {selected.get('provider', provider)}/{selected.get('model', model)}"
            f" @ {selected.get('reasoningEffort', REASONING_EFFORT)}"
        )
    else:
        log(
            f"设置 reasoningEffort 失败（不影响使用）: "
            f"{(data.get('result') or {}).get('error') or status}",
            "WARN",
        )


# ── 模型链：主模型不可用时自动降级，恢复后自动切回 ──────────────
def parse_model_chain(raw: str) -> list[tuple[str, str, str]]:
    """解析 `provider/model[:effort],...` 为三元组列表。"""
    chain: list[tuple[str, str, str]] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        effort = ""
        if ":" in item:
            item, _, effort = item.rpartition(":")
        if "/" not in item:
            continue
        provider, _, model = item.partition("/")
        provider, model = provider.strip(), model.strip()
        if not (provider and model):
            continue
        chain.append((provider, model, effort.strip() or REASONING_EFFORT))
    return chain


# 解析成实际使用的链（放在函数之后，避免定义顺序问题）
MODEL_CHAIN = parse_model_chain(MODEL_CHAIN_RAW)


def set_session_model(
    api: "DshApi", sid: str, provider: str, model: str, effort: str = ""
) -> bool:
    """给会话指定模型。selectModel 必须同时给 provider 与 model。"""
    req: dict = {"sessionId": sid, "provider": provider, "model": model}
    if effort:
        req["reasoningEffort"] = effort
    status, data = api.rpc("session/selectModel", {"request": req})
    return status == 200 and bool((data.get("result") or {}).get("ok"))


def ensure_probe_session(api: "DshApi", state: dict) -> str:
    """健康检查用的独立会话，放在 data/ 下以避开人格 AGENTS.md。"""
    sid = state.get("probe_session_id")
    if sid and _session_exists(api, sid) and count_turns(api, sid) < 30:
        return sid

    probe_cwd = DATA_DIR / "probe"
    probe_cwd.mkdir(parents=True, exist_ok=True)
    status, data = api.rpc("session/create", {"request": {"cwd": str(probe_cwd)}})
    ok = (data.get("result") or {}).get("ok")
    if status != 200 or not ok:
        raise RuntimeError("无法创建健康检查会话")
    sid = data["result"]["value"]["sessionId"]
    api.rpc(
        "session/rename",
        {"request": {"sessionId": sid, "title": "模型健康检查"}},
    )
    state["probe_session_id"] = sid
    save_state(state)
    return sid


def probe_model(
    api: "DshApi", item: tuple[str, str, str], probe_sid: str, timeout: float
) -> bool:
    """给指定模型发一句极短的探测，看能不能出字。"""
    provider, model, effort = item
    if not set_session_model(api, probe_sid, provider, model, effort):
        return False

    before_turns = max([t.get("turn", 0) for t in _read_turns(api, probe_sid)], default=0)
    ping = f"在吗 {uuid.uuid4().hex[:6]}"
    status, data = api.rpc(
        "session/prompt",
        {
            "request": {
                "sessionId": probe_sid,
                "requestId": str(uuid.uuid4()),
                "mode": "queue",
                "content": [{"type": "text", "text": ping}],
            }
        },
    )
    if status != 200 or not (data.get("result") or {}).get("ok"):
        return False

    deadline = time.time() + timeout
    while time.time() < deadline and _running:
        time.sleep(1.2)
        for t in reversed(_read_turns(api, probe_sid)):
            if t.get("turn", 0) <= before_turns:
                break
            if (t.get("response") or "").strip():
                return True
    return False


def pick_working_model(
    api: "DshApi", state: dict
) -> tuple[str, str, str] | None:
    """按链顺序挑一个能用的模型。

    主模型失败后会被记进 model_health，FAIL_TTL 内不再探测它；
    过了 TTL 会重新试一次，成功就自动切回主模型。
    """
    health = state.setdefault("model_health", {})
    now = time.time()

    # ── 快路径：只用「缓存里确认可用」的模型，零探测延迟 ──
    # 否则每次缓存过期都要白等一次 20s 探测，回复就会被拖慢。
    for item in MODEL_CHAIN:
        provider, model, _effort = item
        rec = health.get(f"{provider}/{model}") or {}
        if rec.get("ok") is True and now - float(rec.get("at") or 0) < MODEL_HEALTH_OK_TTL:
            return item

    try:
        probe_sid = ensure_probe_session(api, state)
    except Exception as exc:  # noqa: BLE001
        log(f"健康检查会话不可用，直接按链顺序尝试：{exc}", "WARN")
        probe_sid = ""

    for item in MODEL_CHAIN:
        provider, model, effort = item
        key = f"{provider}/{model}"
        rec = health.get(key) or {}
        ok_flag = rec.get("ok")
        checked_at = float(rec.get("at") or 0)
        ttl = MODEL_HEALTH_OK_TTL if ok_flag else MODEL_HEALTH_FAIL_TTL

        if ok_flag is not None and now - checked_at < ttl:
            if ok_flag:
                return item
            continue  # 最近确认失败，且还在冷却期

        if not probe_sid:
            # 没法探测就只能盲试，由调用方的空回复判定兜底
            return item

        good = probe_model(api, item, probe_sid, MODEL_PROBE_TIMEOUT)
        health[key] = {"ok": good, "at": time.time()}
        save_state(state)
        if good:
            if key != MODEL_CHAIN[0][0] + "/" + MODEL_CHAIN[0][1]:
                log(f"主模型不可用，降级到 {key}")
            elif rec.get("ok") is False:
                log(f"主模型已恢复：{key}")
            return item
        log(f"模型 {key} 探测失败，尝试下一个", "WARN")

    log("模型链里所有模型都不可用", "ERROR")
    return None


def start_health_refresher(api: "DshApi", state: dict) -> None:
    """后台线程：定期重新探测链首（主模型），让它恢复后能被自动切回。

    必须放后台——同步探测一次要 20 秒，会把回复拖慢。
    """

    def loop() -> None:
        while _running:
            time.sleep(MODEL_REFRESH_INTERVAL_S)
            if not _running:
                break
            try:
                if not MODEL_CHAIN:
                    continue
                provider, model, _effort = MODEL_CHAIN[0]
                key = f"{provider}/{model}"
                rec = state.get("model_health", {}).get(key) or {}
                # 只在「之前判定失败 且 已经过了冷却」时重探
                if rec.get("ok") is not False:
                    continue
                if time.time() - float(rec.get("at") or 0) < MODEL_HEALTH_FAIL_TTL:
                    continue
                sid = ensure_probe_session(api, state)
                good = probe_model(api, MODEL_CHAIN[0], sid, MODEL_PROBE_TIMEOUT)
                state.setdefault("model_health", {})[key] = {
                    "ok": good,
                    "at": time.time(),
                }
                save_state(state)
                log(f"后台复检主模型 {key}：{'已恢复' if good else '仍不可用'}")
            except Exception as exc:  # noqa: BLE001
                log(f"后台健康检查异常：{exc}", "WARN")

    threading.Thread(target=loop, name="health-refresher", daemon=True).start()


def mark_model_bad(state: dict, model: tuple[str, str, str]) -> None:
    """把某个模型标记为当前不可用，下一轮会重新探链。"""
    key = f"{model[0]}/{model[1]}"
    state.setdefault("model_health", {})[key] = {"ok": False, "at": time.time()}
    save_state(state)


def ask_dsh(
    text: str,
    api: "DshApi",
    sid: str,
    model: tuple[str, str, str] | None = None,
) -> str:
    """把消息投递到 DSH 会话，并等待该轮回复产生。

    model 不为空时，投递前先把会话切到该模型（故障切换用）。
    """
    if model:
        provider, name, effort = model
        if not set_session_model(api, sid, provider, name, effort):
            log(f"切换模型 {provider}/{name} 失败", "WARN")

    before = _read_turns(api, sid)
    before_turns = max([t.get("turn", 0) for t in before], default=0)

    req = {
        "sessionId": sid,
        "requestId": str(uuid.uuid4()),
        "mode": "queue",
        "content": [{"type": "text", "text": text}],
    }
    status, data = api.rpc("session/prompt", {"request": req})
    ok = (data.get("result") or {}).get("ok")
    if status != 200 or not ok:
        log(f"投递失败: {status} {(data.get('result') or {}).get('error')}", "ERROR")
        return ""

    deadline = time.time() + DSH_TIMEOUT_S
    while time.time() < deadline and _running:
        time.sleep(2)
        turns = _read_turns(api, sid)
        for t in reversed(turns):
            if t.get("turn", 0) <= before_turns:
                break
            resp = (t.get("response") or "").strip()
            if resp:
                return resp
    log("等待 DSH 回复超时", "WARN")
    return ""


# ----------------------------------------------------------------------------
# 状态
# ----------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {"last_sig": "", "send_times": [], "initialized": False}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    except OSError:
        pass


def in_quiet_hours() -> bool:
    if not QUIET_HOURS:
        return False
    try:
        start_s, end_s = QUIET_HOURS.split("-")
        sh, sm = map(int, start_s.split(":"))
        eh, em = map(int, end_s.split(":"))
        now = datetime.now().time()
        start, end = dtime(sh, sm), dtime(eh, em)
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end
    except Exception:
        return False


def rate_ok(state: dict, verbose: bool = False) -> bool:
    """发送速率是否可以继续。

    两道闸：每分钟（腾讯硬限制，超了会整段限速）和每小时（自己设的总量）。
    """
    now = time.time()
    times = state.get("send_times", [])
    # 只保留最近一小时
    times = [t for t in times if now - t < 3600]
    state["send_times"] = times

    per_min = sum(1 for t in times if now - t < 60)
    if per_min >= SEND_LIMIT_MARGIN:
        if verbose:
            log(
                f"已达每分钟发送上限（{per_min}/{MAX_SENDS_PER_MINUTE}，"
                f"余量刹车 {SEND_LIMIT_MARGIN}），等一分钟后继续",
                "WARN",
            )
        return False

    if len(times) >= MAX_SENDS_PER_HOUR:
        if verbose:
            log(f"已达每小时发送上限（{len(times)}/{MAX_SENDS_PER_HOUR}）", "WARN")
        return False

    return True


def stash_pending(state: dict, parts: list[str], incoming: str) -> None:
    """发送失败时把回复暂存起来，下一轮优先补发。"""
    pending = state.setdefault("pending_sends", [])
    pending.append({"parts": parts, "incoming": incoming, "at": time.time()})
    # 只留最近 5 条，避免无限堆积
    state["pending_sends"] = pending[-5:]
    save_state(state)
    log(f"  已暂存待补发（队列共 {len(state['pending_sends'])} 条）", "WARN")


def take_pending(state: dict) -> dict | None:
    """取出一条待补发的回复。"""
    pending = state.get("pending_sends") or []
    if not pending:
        return None
    item = pending.pop(0)
    # 超过 10 分钟的就不补了，不然会突兀
    if time.time() - item.get("at", 0) > 600:
        state["pending_sends"] = pending
        save_state(state)
        log(f"  丢弃过期待补发（{item.get('incoming', '')[:30]!r}）", "WARN")
        return None
    state["pending_sends"] = pending
    save_state(state)
    return item


def wait_for_rate_slot(state: dict, max_wait: float = 65.0) -> bool:
    """发送前若已到每分钟上限，等到有位置为止（最多等 max_wait 秒）。

    返回 True 表示可以发；False 表示等满了还没位置（调用方应放弃这次发送）。
    """
    if rate_ok(state, verbose=True):
        return True
    deadline = time.time() + max_wait
    while time.time() < deadline and _running:
        time.sleep(1.0)
        if rate_ok(state):
            return True
    return False


class _SkipSend(Exception):
    """内部信号：本次不发送，直接进入下一轮轮询。"""


# ----------------------------------------------------------------------------
# 主循环
# ----------------------------------------------------------------------------
_running = True


def _on_signal(signum, frame):
    global _running
    _running = False


def main() -> int:
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    if not acquire_instance_lock():
        return 3

    if TARGET_REMARK == "你的备注名":
        log("尚未配置目标会话：请复制 .env.example 为 .env，并填写 QQ_TARGET", "FATAL")
        log("  QQ_TARGET = 你在 QQ 里给机器人小号设置的「好友备注」", "FATAL")
        return 2

    if not HIS.AXIsProcessTrusted():
        log("缺少辅助功能权限：系统设置 → 隐私与安全性 → 辅助功能，添加终端", "FATAL")
        return 2

    try:
        api = DshApi(DSH_BASE_URL)
        if not api.ping():
            log(f"无法连接 DSH Web 服务（{DSH_BASE_URL}）", "FATAL")
            return 2
        current_sid = pick_session(api)
        apply_reasoning_effort(api, current_sid)
    except Exception as exc:  # noqa: BLE001
        log(f"初始化 DSH 连接失败: {exc}", "FATAL")
        return 2

    PID_FILE.write_text(str(os.getpid()), "utf-8")
    log("=" * 62)
    log("QQ ↔ DSH 桥接已启动")
    log(f"  目标会话 : {TARGET_REMARK}")
    log(f"  DSH 接入 : {DSH_BASE_URL} / {current_sid}")
    log(f"  模式     : {'只读（不发送）' if READ_ONLY else '自动回复'}")
    log(f"  轮询     : {POLL_MIN_S}~{POLL_MAX_S}s 随机")
    log(
        f"  发送间隔 : {SEND_GAP_MIN_S}~{SEND_GAP_MAX_S}s，"
        f"每分钟≤{SEND_LIMIT_MARGIN}、每小时≤{MAX_SENDS_PER_HOUR}"
    )
    log(f"  静默时段 : {QUIET_HOURS or '未设置'}")
    log(f"  日志     : {LOG_FILE}")
    log("=" * 62)

    qq = QQ()
    state = load_state()
    last_send_at = 0.0
    miss_count = 0

    # ── 进阶组件 ──
    diary = Diary(QQ_SESSION_CWD, DIARY_MAX_PER_DAY) if DIARY_ENABLED else None
    impressions = Impressions(QQ_SESSION_CWD, TARGET_REMARK) if IMPRESSIONS_ENABLED else None
    gallery = Gallery(QQ_SESSION_CWD) if GALLERY_ENABLED else None
    shots = ScreenshotStore(QQ_SESSION_CWD, SCREENSHOT_KEEP_DAYS)
    schedule = Schedule(QQ_SESSION_CWD) if SCHEDULE_ENABLED else None
    if MODEL_CHAIN:
        desc = " → ".join(f"{p}/{m}" for p, m, _ in MODEL_CHAIN)
        log(f"模型链: {desc}")
        start_health_refresher(api, state)
        log(f"  探测超时 {MODEL_PROBE_TIMEOUT:.0f}s，成功缓存 {MODEL_HEALTH_OK_TTL:.0f}s，"
            f"失败冷却 {MODEL_HEALTH_FAIL_TTL:.0f}s")
    else:
        log("模型链为空，将使用会话默认模型", "WARN")

    if schedule is not None:
        if schedule.ok:
            n = len(schedule._classes())  # noqa: SLF001
            log(f"课程表已加载（{n} 节课）")
        else:
            log("课程表未找到（qq-workspace/课程表/schedule.json）", "WARN")
    if VISION_ENABLED:
        cleaned = shots.cleanup()
        log(f"图片理解已开启（截图保留 {SCREENSHOT_KEEP_DAYS} 天"
            + (f"，清理了 {cleaned} 张旧图" if cleaned else "") + "）")
    proactive = Proactive(
        enabled=PROACTIVE_ENABLED,
        check_min_minutes=PROACTIVE_CHECK_MIN,
        check_max_minutes=PROACTIVE_CHECK_MAX,
        idle_minutes=PROACTIVE_IDLE_MINUTES,
        probability=PROACTIVE_PROBABILITY,
        quiet_hours=QUIET_HOURS,
    )
    last_activity_ts = time.time()
    state.setdefault("_last_change_ts", time.time())
    if DIARY_ENABLED:
        log(f"日记已开启（今日 {diary.count_today()}/{DIARY_MAX_PER_DAY}）")
    if PROACTIVE_ENABLED:
        log(
            f"主动找已开启（每 {PROACTIVE_CHECK_MIN:.0f}~{PROACTIVE_CHECK_MAX:.0f} 分钟检查，"
            f"安静 {PROACTIVE_IDLE_MINUTES:.0f} 分钟后才有概率发，概率 {PROACTIVE_PROBABILITY}）"
        )

    while _running:
        try:
            if not qq.refresh():
                miss_count += 1
                if miss_count % 10 == 1:
                    log("未找到 QQ 窗口（未运行或未登录）", "WARN")
                time.sleep(5)
                continue
            miss_count = 0

            item = qq.find_session_item(TARGET_REMARK)
            if item is None:
                log(f"未找到备注为 {TARGET_REMARK} 的会话", "WARN")
                time.sleep(10)
                continue

            # ── 优先补发上次失败的回复 ──
            queued = take_pending(state)
            if queued is not None:
                log(f"发现待补发回复：{queued['parts'][0][:50]!r}")
                if not in_quiet_hours() and wait_for_rate_slot(state, max_wait=30):
                    got_idle, _ = wait_until_user_idle(max_wait=30)
                    if got_idle:
                        resent = send_messages(
                            qq, queued["parts"], rate_guard=lambda: rate_ok(state)
                        )
                        if resent:
                            last_send_at = time.time()
                            state["send_times"].append(last_send_at)
                            own = state.setdefault("own_sent", [])
                            own.extend(p[:200] for p in resent)
                            state["own_sent"] = own[-20:]
                            save_state(state)
                            log(f"✅ 补发成功：{resent[0][:60]!r}")
                        else:
                            # 再失败就放回去，下次继续
                            stash_pending(state, queued["parts"], queued.get("incoming", ""))
                    else:
                        stash_pending(state, queued["parts"], queued.get("incoming", ""))

            info = qq.session_info(item)
            # 主信号：会话列表摘要
            sig = f"{info['time']}|{info['preview']}"

            # 辅助信号：聊天记录里最后一条「对方」消息。
            # 必须每轮都读——QQ 的会话列表摘要经常不刷新，
            # 只靠 sig 会漏消息（实测漏过 13 分钟）。
            deep = ""
            try:
                deep = qq.read_chat_last_incoming()
            except Exception as exc:  # noqa: BLE001
                log(f"读取聊天记录失败：{exc}", "WARN")

            last_incoming = state.get("last_incoming", "")
            summary_changed = sig != state.get("last_sig")
            record_changed = bool(deep) and deep != last_incoming

            # 轮询心跳：DEBUG 模式下每 10 轮打一次，确认它真的在读、读到什么
            poll_count = state.get("_poll_count", 0) + 1
            state["_poll_count"] = poll_count
            if DEBUG_ENABLED and poll_count % 10 == 1:
                log(
                    f"[轮询 {poll_count}] sig={sig[:40]!r} deep={deep[:30]!r} "
                    f"last_sig={str(state.get('last_sig'))[:40]!r} "
                    f"last_incoming={last_incoming[:30]!r}",
                    "DEBUG",
                )

            if not state.get("initialized"):
                state["initialized"] = True
                state["last_sig"] = sig
                state["last_incoming"] = deep
                save_state(state)
                log(f"首次同步完成，当前摘要：{info['preview'][:60]!r}")
            elif summary_changed or record_changed:
                state["_last_change_ts"] = time.time()
                if record_changed and not summary_changed:
                    log(f"会话摘要未更新，但聊天记录有新消息：{deep[:50]!r}")
                # 只记 last_sig，防止同一轮重复检测；
                # last_incoming 要等真正处理完（或确认是自身消息）才更新，
                # 否则中途跳过发送会让这条消息永远丢失。
                state["last_sig"] = sig
                save_state(state)
                log(f"检测到新消息：{info['preview'][:80]!r}")

                # 回环防护
                own = state.get("own_sent") or []

                def _is_own(text: str) -> bool:
                    t = (text or "").strip()
                    if not t:
                        return False
                    return any(
                        t == o.strip() or (o.strip() and o.strip().startswith(t[:20]))
                        for o in own
                    )

                # 两个信号必须分开判断，这是去重的关键：
                #   record_changed  = 聊天记录里出现了新的「对方」消息（可靠）
                #   summary_changed = 会话列表摘要变了（可能只是自己刚发的那条）
                #
                # 曾经的 bug：只要 deep 非空就拿它当 candidate，
                # 于是「只有摘要变成自己的回复」时，又把上一条旧消息处理一遍。
                if record_changed:
                    candidate = deep
                else:
                    # 只有摘要变了：通常是自己刚发出的消息成了预览，不该回复
                    candidate = info["preview"].strip()
                    if not candidate:
                        log("无新消息（摘要为空）")
                        time.sleep(random.uniform(POLL_MIN_S, POLL_MAX_S))
                        continue
                    if _is_own(candidate):
                        log(f"仅摘要变化（自己发的）：{candidate[:50]!r}，跳过")
                        time.sleep(random.uniform(POLL_MIN_S, POLL_MAX_S))
                        continue

                # 兜底：候选内容若是自己发过的，也跳过（防渲染方向异常）
                if _is_own(candidate):
                    log(f"跳过自身消息：{candidate[:60]!r}")
                    if record_changed and deep:
                        state["last_incoming"] = deep
                        save_state(state)
                    time.sleep(random.uniform(POLL_MIN_S, POLL_MAX_S))
                    continue

                # 过滤明显的误发碎片：只有标点，或单个助词（的/了/吧/啊）
                # 注意「嗯」「好」「在」这类是正常回复，不能拦
                stripped = re.sub(r"[\s\W_]+", "", candidate)
                _FILLERS = set("的了吧呢啊哦嘛呀")
                if not stripped or (len(stripped) == 1 and stripped in _FILLERS):
                    log(f"疑似误发的碎片（{candidate!r}），跳过", "INFO")
                    if deep:
                        state["last_incoming"] = deep
                        save_state(state)
                    time.sleep(random.uniform(POLL_MIN_S, POLL_MAX_S))
                    continue

                # ── 去重：同一条消息只回复一次 ──
                # QQ 界面渲染滞后时，read_chat_last_incoming 会反复读到
                # 同一条旧消息，导致同一句话被回复好几遍。
                _norm = lambda s: re.sub(r"\s+", "", (s or ""))
                handled = state.setdefault("handled_incoming", [])
                if _norm(candidate) in handled:
                    log(f"该消息已处理过，跳过：{candidate[:50]!r}")
                    time.sleep(random.uniform(POLL_MIN_S, POLL_MAX_S))
                    continue

                log(f"处理新消息：{candidate[:80]!r}")

                # 切到会话读完整记录（AXPress，不激活窗口）
                qq.open_session(item)
                time.sleep(0.6)
                qq.refresh()
                structured = qq.read_chat_structured()

                user_text = ""
                own = state.get("own_sent") or []
                for msg in reversed(structured):
                    if msg.get("is_self"):
                        continue  # 自己发的，跳过
                    s = (msg.get("text") or "").strip()
                    if not s:
                        continue
                    # 再对一遍 own_sent，防止界面方向判断异常
                    if any(s == o.strip() or (o.strip() and o.strip().startswith(s[:20])) for o in own):
                        continue
                    user_text = s
                    break
                if not user_text:
                    user_text = info["preview"].strip()
                log(f"用户消息：{user_text[:100]!r}")

                # ── 输入层：拼情景头 + 最近对话 ──
                prompt_text = build_context_message(
                    user_text,
                    history=structured,
                    own_sent=own,
                    partner_name=TARGET_REMARK,
                    history_limit=CONTEXT_HISTORY,
                )

                # ── 进阶 5④ 时间感 + ② 印象 + ③ 图库 + ① 日记 ──
                extras: list[str] = []
                extras.append(
                    "【时间感】" + time_feel(datetime.fromtimestamp(last_activity_ts))
                )
                if impressions:
                    snippet = impressions.prompt_snippet()
                    if snippet:
                        extras.append(snippet)
                if gallery:
                    gal = gallery.format()
                    if gal:
                        extras.append("【你的图库】\n" + gal)
                if diary:
                    snippet = diary.diary_prompt_snippet()
                    if snippet:
                        extras.append(snippet)
                if schedule is not None:
                    snippet = schedule.prompt_snippet()
                    if snippet:
                        extras.append(snippet)

                # ── 图片理解：有新图才截图 ──
                if VISION_ENABLED and has_image_placeholder(structured):
                    try:
                        shot = capture_window(qq.pid, shots.new_path())
                        if shot:
                            log(f"  已截图：{shot.name}")
                            extras.append(build_image_hint(shot))
                        else:
                            log("截图失败（检查「屏幕录制」权限）", "WARN")
                    except Exception as exc:  # noqa: BLE001
                        log(f"截图异常：{exc}", "WARN")

                if extras:
                    prompt_text = prompt_text + "\n\n" + "\n\n".join(extras)

                log(f"  投递上下文 {len(prompt_text)} 字符")
                last_activity_ts = time.time()

                # ── 第 8 项：轮数超限则滚动会话 ──
                try:
                    current_sid = maybe_rotate_session(
                        api, current_sid, MAX_SESSION_TURNS
                    )
                except Exception as exc:  # noqa: BLE001
                    log(f"会话轮转失败（继续用旧会话）: {exc}", "WARN")

                # ── 选模型：主模型不可用则降级，恢复后自动切回 ──
                chosen = pick_working_model(api, state)
                if chosen is None:
                    log(
                        "模型链全部不可用，30 秒后重试（消息不会丢，"
                        "恢复后会自动补回）",
                        "ERROR",
                    )
                    time.sleep(30)
                    continue

                raw_reply = ask_dsh(prompt_text, api, current_sid, chosen)
                if not raw_reply:
                    log(f"DSH 无输出（{chosen[0]}/{chosen[1]}），标记为不可用", "WARN")
                    mark_model_bad(state, chosen)

                # ── 从回复里提取日记 / 印象 / 图库动作 ──
                if raw_reply:
                    if diary:
                        raw_reply, diary_entry = parse_diary_from_reply(raw_reply)
                        if diary_entry:
                            saved = diary.append(diary_entry["content"], diary_entry["mood"])
                            if saved:
                                log(f"📔 她写了一条日记：{saved['内容'][:60]!r}")
                            else:
                                log("日记写入跳过（超出当日上限或内容为空）", "WARN")
                    if impressions:
                        raw_reply, traits = parse_impression_from_reply(raw_reply)
                        if traits:
                            impressions.save(traits)
                            log(f"💭 印象已更新：{'、'.join(traits)}")
                    if gallery:
                        raw_reply, gal_entry = parse_gallery_from_reply(raw_reply)
                        if gal_entry:
                            gallery.add(gal_entry["title"], gal_entry["reason"])
                            log(f"🖼 图库新增：{gal_entry['title']!r}")

                # ── 输出层：清洗成聊天腔 ──
                reply = clean_reply(raw_reply) if raw_reply else ""
                if raw_reply and not reply:
                    log(f"回复被清洗丢弃（含 AI 自曝或纯客套）：{raw_reply[:80]!r}", "WARN")
                if not reply:
                    pass  # 没有可发的内容
                elif READ_ONLY:
                    log(f"[只读模式] 拟定回复：{reply[:200]!r}")
                else:
                    if in_quiet_hours():
                        log("处于静默时段，暂不发送", "INFO")
                    elif not wait_for_rate_slot(state):
                        log("已达发送上限且等待超时，放弃本次回复", "WARN")
                    else:
                        # ── 节奏层：拆条 + 打字延迟 + 随机敷衍 ──
                        parts = split_messages(reply, max_chars=SPLIT_MAX_CHARS)
                        # 偶尔只回一个短词，像真人在忙
                        if len(parts) > 1 and random.random() < SHORT_REPLY_PROBABILITY:
                            parts = parts[:1]
                            log("  随机触发：只回第一条")

                        # ── 不抢控制权：等你停下键盘和鼠标再发 ──
                        got_idle, waittime = wait_until_user_idle(TYPING_GUARD_MAX_WAIT_S)
                        if not got_idle:
                            log(
                                f"你一直在操作电脑，本次跳过（拟定回复：{reply[:60]!r}）",
                                "WARN",
                            )
                            raise _SkipSend()

                        gap = random.uniform(SEND_GAP_MIN_S, SEND_GAP_MAX_S)
                        elapsed = time.time() - last_send_at
                        if elapsed < gap:
                            time.sleep(gap - elapsed)

                        # 首条前的延迟：默认几乎为零，追求"秒回"
                        think = random.uniform(
                            TYPING_DELAY_MIN_S, TYPING_DELAY_MAX_S
                        )
                        if think > 0.05:
                            log(f"  发送前延迟 {think:.1f}s")
                            time.sleep(think)

                        sent_parts = send_messages(
                            qq, parts, rate_guard=lambda: rate_ok(state)
                        )
                        if sent_parts:
                            last_send_at = time.time()
                            # 只有本次处理的是「聊天记录里的新消息」时才更新
                            # last_incoming；如果是靠摘要兜底处理的，deep 还是旧值，
                            # 更新它会让真正的新消息在下一轮被跳过。
                            if record_changed and deep:
                                state["last_incoming"] = deep
                            # 记入已处理列表，防止重复回复
                            _h = state.setdefault("handled_incoming", [])
                            _h.append(re.sub(r"\s+", "", candidate))
                            state["handled_incoming"] = _h[-30:]
                            state["send_times"].append(last_send_at)
                            # 回环防护：逐条记录自己发出的每一段
                            own = state.setdefault("own_sent", [])
                            own.extend(p[:200] for p in sent_parts)
                            state["own_sent"] = own[-20:]
                            save_state(state)
                            for p in sent_parts:
                                log(f"已发送：{p[:80]!r}")
                        else:
                            log("发送失败，暂存待补发", "ERROR")
                            stash_pending(state, parts, deep)

            # ── 长时间无变化时轻点一次会话项 ──
            # Chromium 的 AX 树在窗口长期处于后台时会停止更新，
            # 桥接就会一直读到旧摘要（实测漏过 5 分钟）。
            # AXPress 只是切换 QQ 内的会话，不会激活应用、不抢焦点。
            if _running and AX_POKE_AFTER_S > 0:
                idle_for = time.time() - state.get("_last_change_ts", 0)
                if idle_for > AX_POKE_AFTER_S:
                    try:
                        qq.open_session(item)
                        state["_last_change_ts"] = time.time()
                        log(f"已 {idle_for:.0f}s 无变化，轻点会话项刷新界面缓存", "DEBUG")
                    except Exception as exc:  # noqa: BLE001
                        log(f"刷新界面缓存失败：{exc}", "WARN")

            # ── 进阶 5① 主动找人（只在没有新消息可处理时） ──
            if (
                proactive.enabled
                and _running
                and rate_ok(state)
                and not in_quiet_hours()
            ):
                try:
                    if proactive.should_fire(
                        time.time(), last_activity_ts, partner_is_typing=user_is_typing()
                    ):
                        log("触发主动发言检查")
                        tid = time_feel(datetime.fromtimestamp(last_activity_ts))
                        parts_prompt = [f"【时间感】{tid}"]
                        if diary and diary.format_recent(3):
                            parts_prompt.append("【最近日记】\n" + diary.format_recent(3))
                        if impressions:
                            snippet = impressions.prompt_snippet()
                            if snippet:
                                parts_prompt.append(snippet)
                        parts_prompt.append(proactive.prompt_snippet())
                        ask_text = "\n\n".join(parts_prompt)

                        p_model = pick_working_model(api, state)
                        if p_model is None:
                            log("模型链全部不可用，跳过主动发言", "WARN")
                            proactive_reply = ""
                        else:
                            proactive_reply = ask_dsh(
                                ask_text, api, current_sid, p_model
                            )
                        cleaned = clean_reply(proactive_reply) if proactive_reply else ""

                        if not cleaned or "SILENT" in (proactive_reply or "").upper():
                            log("她决定不说话")
                        else:
                            msg_parts = split_messages(cleaned, max_chars=SPLIT_MAX_CHARS)
                            if random.random() < SHORT_REPLY_PROBABILITY and len(msg_parts) > 1:
                                msg_parts = msg_parts[:1]
                            time.sleep(typing_delay(msg_parts[0]) if msg_parts else 0)
                            sent = send_messages(
                                qq, msg_parts, rate_guard=lambda: rate_ok(state)
                            )
                            if sent:
                                last_send_at = time.time()
                                last_activity_ts = last_send_at
                                state["send_times"].append(last_send_at)
                                own_list = state.setdefault("own_sent", [])
                                own_list.extend(p[:200] for p in sent)
                                state["own_sent"] = own_list[-20:]
                                # 主动发言后，预览会变成自己发的内容，同步签名避免被当成新消息
                                time.sleep(1.0)
                                qq.refresh()
                                it2 = qq.find_session_item(TARGET_REMARK)
                                if it2 is not None:
                                    info2 = qq.session_info(it2)
                                    state["last_sig"] = f"{info2['time']}|{info2['preview']}"
                                save_state(state)
                                for p in sent:
                                    log(f"主动发送：{p[:80]!r}")
                            else:
                                log("主动发送失败", "ERROR")
                except Exception as exc:  # noqa: BLE001
                    log(f"主动发言异常：{type(exc).__name__}: {exc}", "ERROR")

        except _SkipSend:
            # 本次不发送，正常进入下一轮轮询
            pass
        except Exception as exc:  # noqa: BLE001
            log(f"循环异常：{type(exc).__name__}: {exc}", "ERROR")
            time.sleep(5)

        if not _running:
            break
        time.sleep(random.uniform(POLL_MIN_S, POLL_MAX_S))

    save_state(state)
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    log("桥接已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
