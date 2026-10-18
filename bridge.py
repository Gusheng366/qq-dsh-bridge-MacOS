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
import uuid

import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

from dsh_api import DshApi
from reply_style import clean_reply, split_messages

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
POLL_MAX_S = float(os.environ.get("QQ_POLL_MAX", "30"))
SEND_GAP_MIN_S = float(os.environ.get("QQ_SEND_GAP_MIN", "3"))
SEND_GAP_MAX_S = float(os.environ.get("QQ_SEND_GAP_MAX", "8"))
MAX_SENDS_PER_HOUR = int(os.environ.get("QQ_MAX_SENDS_PER_HOUR", "60"))
MAX_REPLY_CHARS = int(os.environ.get("QQ_MAX_REPLY_CHARS", "1500"))
DSH_TIMEOUT_S = int(os.environ.get("QQ_DSH_TIMEOUT", "600"))
DSH_BASE_URL = os.environ.get("QQ_DSH_URL", "http://127.0.0.1:3080")
QQ_SESSION_CWD = Path(
    os.environ.get("QQ_SESSION_CWD", str(BASE_DIR / "qq-workspace"))
)
READ_ONLY = os.environ.get("QQ_READ_ONLY", "0") == "1"
# 打字保护：检测到用户正在输入时，最多等待多少秒
TYPE_GUARD_MAX_WAIT_S = float(os.environ.get("QQ_TYPE_GUARD_MAX_WAIT", "90"))
# 静默时段 "23:00-08:00"，留空则不启用
QUIET_HOURS = os.environ.get("QQ_QUIET_HOURS", "").strip()
# 拆条：单条最大字数
SPLIT_MAX_CHARS = int(os.environ.get("QQ_SPLIT_MAX_CHARS", "60"))
# 拆条后随机只回第一条的概率（模拟真人在忙）
SHORT_REPLY_PROBABILITY = float(os.environ.get("QQ_SHORT_REPLY_PROB", "0.10"))

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
def log(msg: str, level: str = "INFO") -> None:
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


def user_is_typing() -> bool:
    """当前前台应用是否处于文本输入状态。"""
    try:
        syswide = HIS.AXUIElementCreateSystemWide()
        focused = ax_get(syswide, "AXFocusedUIElement")
        if not focused:
            return False
        role = ax_str(focused, "AXRole") or ""
        return role in ("AXTextField", "AXTextArea", "AXComboBox")
    except Exception:
        return False


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
        if self.pid != pid or self.app is None:
            self.pid = pid
            self.app = HIS.AXUIElementCreateApplication(pid)
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
        if self.window is None:
            return []
        area = self._walk(self.window, lambda e: self._has_class(e, "chat-msg-area"), 0, 20)
        if not area:
            return []
        texts: list = []

        def collect(el, d=0):
            if d > 14:
                return
            role = ax_str(el, "AXRole")
            if role in ("AXStaticText", "AXTextField", "AXTextArea"):
                v = ax_str(el, "AXValue")
                if v and v.strip():
                    texts.append(v)
            for c in ax_children(el):
                collect(c, d + 1)

        collect(area)
        return texts

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


def _send_one(qq: "QQ", text: str) -> bool:
    """在 QQ 已激活、会话已打开的前提下，写入一条消息并回车。"""
    qq.refresh()
    box = qq.input_box()
    if box is None:
        log("找不到输入框", "ERROR")
        return False

    HIS.AXUIElementPerformAction(box, cs("AXPress"))
    time.sleep(0.18)
    clear_input(qq.pid)
    time.sleep(0.12)
    type_via_cg(qq.pid, text)
    time.sleep(0.28)
    press_key(qq.pid, 36)  # Return
    time.sleep(0.35)

    # 验证：输入框清空说明已发出
    qq.refresh()
    box2 = qq.input_box()
    residual = (ax_str(box2, "AXValue") or "").strip()
    if residual:
        log(f"输入框仍有残留（{residual[:40]!r}），可能未发送", "WARN")
        return False
    return True


def typing_delay(text: str, split_mode: bool = False) -> float:
    """按字数估算「打字」耗时，带长尾随机。

    基础 4~8 字/秒；多条之间的停顿较短，首次回复前的"读消息"时间较长。
    """
    chars = max(1, len(text))
    # 4~8 字/秒
    cps = random.uniform(4.0, 8.0)
    base = chars / cps

    if split_mode:
        # 条与条之间：短一些，1~3 秒
        delay = max(0.8, min(base, 3.0))
        return random.uniform(delay * 0.6, delay * 1.3)

    # 首次回复：加"看到消息 + 思考"的时间
    delay = base + random.uniform(1.0, 3.0)
    # 约 12% 概率故意慢一拍，像在忙别的事
    if random.random() < 0.12:
        delay += random.uniform(4.0, 10.0)
    return min(delay, 15.0)


def send_messages(qq: "QQ", parts: list[str]) -> list[str]:
    """分多条发送，只在开头激活一次 QQ，末尾还原原前台应用。

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


def ask_dsh(text: str, api: "DshApi", sid: str) -> str:
    """把消息投递到 DSH 会话，并等待该轮回复产生。"""
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


def rate_ok(state: dict) -> bool:
    now = time.time()
    state["send_times"] = [t for t in state.get("send_times", []) if now - t < 3600]
    return len(state["send_times"]) < MAX_SENDS_PER_HOUR


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
    log(f"  发送间隔 : {SEND_GAP_MIN_S}~{SEND_GAP_MAX_S}s，每小时≤{MAX_SENDS_PER_HOUR}")
    log(f"  静默时段 : {QUIET_HOURS or '未设置'}")
    log(f"  日志     : {LOG_FILE}")
    log("=" * 62)

    qq = QQ()
    state = load_state()
    last_send_at = 0.0
    miss_count = 0

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

            info = qq.session_info(item)
            sig = f"{info['time']}|{info['preview']}"

            if not state.get("initialized"):
                state["initialized"] = True
                state["last_sig"] = sig
                save_state(state)
                log(f"首次同步完成，当前摘要：{info['preview'][:60]!r}")
            elif sig != state.get("last_sig"):
                preview_txt = info["preview"].strip()
                # 回环防护：预览与自己刚发出的内容一致 => 是自己发的那条
                own = state.get("own_sent") or []
                if preview_txt and preview_txt in own:
                    state["last_sig"] = sig
                    save_state(state)
                    log(f"跳过自身消息：{preview_txt[:60]!r}")
                    time.sleep(random.uniform(POLL_MIN_S, POLL_MAX_S))
                    continue

                state["last_sig"] = sig
                save_state(state)
                log(f"检测到新消息：{info['preview'][:80]!r}")

                # 切到会话读完整记录（AXPress，不激活窗口）
                qq.open_session(item)
                time.sleep(0.6)
                qq.refresh()
                msgs = qq.read_chat_messages()

                user_text = ""
                own = state.get("own_sent") or []
                for m in reversed(msgs):
                    s = m.strip()
                    if not s or s == info["time"]:
                        continue
                    # 跳过自己发过的消息
                    if any(s == o.strip() or (o.strip() and o.strip().startswith(s[:20])) for o in own):
                        continue
                    user_text = s
                    break
                if not user_text:
                    user_text = info["preview"].strip()
                log(f"用户消息：{user_text[:100]!r}")

                raw_reply = ask_dsh(user_text, api, current_sid)
                if not raw_reply:
                    log("DSH 无输出", "WARN")

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
                    elif not rate_ok(state):
                        log("达到每小时发送上限", "WARN")
                    else:
                        # ── 节奏层：拆条 + 打字延迟 + 随机敷衍 ──
                        parts = split_messages(reply, max_chars=SPLIT_MAX_CHARS)
                        # 偶尔只回一个短词，像真人在忙
                        if len(parts) > 1 and random.random() < SHORT_REPLY_PROBABILITY:
                            parts = parts[:1]
                            log("  随机触发：只回第一条")

                        wait_start = time.time()
                        while user_is_typing() and time.time() - wait_start < TYPE_GUARD_MAX_WAIT_S:
                            time.sleep(2)
                            if not _running:
                                break

                        gap = random.uniform(SEND_GAP_MIN_S, SEND_GAP_MAX_S)
                        elapsed = time.time() - last_send_at
                        if elapsed < gap:
                            time.sleep(gap - elapsed)

                        # 首条前的"读消息 + 思考"延迟
                        think = typing_delay(parts[0]) if parts else 0.0
                        log(f"  模拟打字 {think:.1f}s")
                        time.sleep(think)

                        sent_parts = send_messages(qq, parts)
                        if sent_parts:
                            last_send_at = time.time()
                            state["send_times"].append(last_send_at)
                            # 回环防护：逐条记录自己发出的每一段
                            own = state.setdefault("own_sent", [])
                            own.extend(p[:200] for p in sent_parts)
                            state["own_sent"] = own[-20:]
                            save_state(state)
                            for p in sent_parts:
                                log(f"已发送：{p[:80]!r}")
                        else:
                            log("发送失败", "ERROR")

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
