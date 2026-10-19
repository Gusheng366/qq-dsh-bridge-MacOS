#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看懂图片和表情包。

QQ 的 Accessibility 树里拿不到图片的像素数据——只能拿到「这里有一张图」。
所以走截图路线：把 QQ 窗口截下来存成文件，让模型自己用 read 工具看。

为什么不让模型直接"看图"：
  DSH 的图片附件走 base64 段或附件服务，QQ 界面里既没有可下载的 URL，
  也没有附件 API。截成文件 + read 工具是唯一稳定可行的路径。

成本控制：
  只在「确实有新图片消息」时才截图，不能每条消息都截。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import subprocess
from datetime import datetime
from pathlib import Path

CG = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
)
CF = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
ENC = 0x08000100

CF.CFStringCreateWithCString.restype = ctypes.c_void_p
CF.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
CF.CFArrayGetCount.restype = ctypes.c_long
CF.CFArrayGetCount.argtypes = [ctypes.c_void_p]
CF.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
CF.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
CF.CFDictionaryGetValue.restype = ctypes.c_void_p
CF.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
CF.CFNumberGetValue.restype = ctypes.c_bool
CF.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
CF.CFRelease.restype = None
CF.CFRelease.argtypes = [ctypes.c_void_p]

CG.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
CG.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]


def _cfkey(name: str):
    return CF.CFStringCreateWithCString(None, name.encode(), ENC)


def _windows_of(pid: int) -> list[dict]:
    """列出指定进程的可见窗口。"""
    ref = CG.CGWindowListCopyWindowInfo(1, 0)  # kCGWindowListOptionOnScreenOnly
    if not ref:
        return []
    keys = {
        "pid": _cfkey("kCGWindowOwnerPID"),
        "num": _cfkey("kCGWindowNumber"),
        "layer": _cfkey("kCGWindowLayer"),
    }
    out: list[dict] = []
    try:
        for i in range(CF.CFArrayGetCount(ref)):
            d = CF.CFArrayGetValueAtIndex(ref, i)
            pid_v = ctypes.c_int32()
            if not CF.CFNumberGetValue(
                CF.CFDictionaryGetValue(d, keys["pid"]), 3, ctypes.byref(pid_v)
            ):
                continue
            if pid_v.value != pid:
                continue
            num = ctypes.c_int32()
            CF.CFNumberGetValue(
                CF.CFDictionaryGetValue(d, keys["num"]), 3, ctypes.byref(num)
            )
            layer = ctypes.c_int32()
            CF.CFNumberGetValue(
                CF.CFDictionaryGetValue(d, keys["layer"]), 3, ctypes.byref(layer)
            )
            out.append({"id": num.value, "layer": layer.value})
    finally:
        CF.CFRelease(ref)
        for k in keys.values():
            CF.CFRelease(k)
    return out


def capture_window(pid: int, out_path: Path) -> Path | None:
    """截取该进程的主窗口，返回文件路径；失败返回 None。

    需要「屏幕录制」权限（系统设置 → 隐私与安全性 → 屏幕录制）。
    """
    wins = _windows_of(pid)
    if not wins:
        return None
    # 主窗口通常 layer=0，面板类窗口 layer 更大
    main = min(wins, key=lambda w: w["layer"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    try:
        subprocess.run(
            ["screencapture", "-x", "-o", f"-l{main['id']}", str(out_path)],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return None
    if out_path.exists() and out_path.stat().st_size > 1024:
        return out_path
    return None


def has_image_placeholder(messages: list[dict]) -> bool:
    """**最新一条**消息是不是图片/表情包。

    只看最后一条——历史里躺着的旧图片不该触发截图，
    否则每条纯文字消息都会白白多花 8~10 秒去读截图。
    """
    if not messages:
        return False
    for msg in reversed(messages):
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        # 跳过自己发的，找到对面最新那条
        if msg.get("is_self"):
            continue
        return "[图片" in text or "[表情包]" in text
    return False


def build_image_hint(screenshot: Path) -> str:
    """给模型的提示：让它自己去看截图。"""
    return (
        "【对方发了图片】\n"
        f"用 read 工具看一下这张截图：{screenshot}\n"
        "截图里能看到整个聊天窗口，找到最新那张图/表情包，"
        "用一句话说说那是什么，然后接着聊。\n"
        "不要提「截图」「工具」这些词，就当自己看见了。"
    )


class ScreenshotStore:
    """管理截图文件：按天归档、自动清理旧文件。"""

    def __init__(self, base_dir: Path, keep_days: int = 2):
        self.dir = Path(base_dir) / "截图"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.keep_days = keep_days

    def new_path(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return self.dir / f"qq-{stamp}.png"

    def cleanup(self) -> int:
        """删除超过保留期的截图，返回删除数量。"""
        if self.keep_days <= 0:
            return 0
        cutoff = datetime.now().timestamp() - self.keep_days * 86400
        removed = 0
        try:
            for f in self.dir.glob("qq-*.png"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
        except OSError:
            pass
        return removed
