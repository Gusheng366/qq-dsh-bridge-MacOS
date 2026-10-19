#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测 QQ 窗口截图能力（需要「屏幕录制」权限）。"""

import ctypes
import ctypes.util
import subprocess
import sys
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


def _key(name: str):
    return CF.CFStringCreateWithCString(None, name.encode(), ENC)


def windows_of(pid: int) -> list[dict]:
    """列出某进程的所有窗口（含 id / 层级 / 边界）。"""
    ref = CG.CGWindowListCopyWindowInfo(1, 0)  # kCGWindowListOptionOnScreenOnly
    if not ref:
        return []
    k_pid = _key("kCGWindowOwnerPID")
    k_num = _key("kCGWindowNumber")
    k_layer = _key("kCGWindowLayer")
    result = []
    try:
        count = CF.CFArrayGetCount(ref)
        for i in range(count):
            d = CF.CFArrayGetValueAtIndex(ref, i)
            pid_val = ctypes.c_int32()
            if not CF.CFNumberGetValue(CF.CFDictionaryGetValue(d, k_pid), 3, ctypes.byref(pid_val)):
                continue
            if pid_val.value != pid:
                continue
            num = ctypes.c_int32()
            CF.CFNumberGetValue(CF.CFDictionaryGetValue(d, k_num), 3, ctypes.byref(num))
            layer = ctypes.c_int32()
            CF.CFNumberGetValue(CF.CFDictionaryGetValue(d, k_layer), 3, ctypes.byref(layer))
            result.append({"id": num.value, "layer": layer.value})
    finally:
        CF.CFRelease(ref)
        for k in (k_pid, k_num, k_layer):
            CF.CFRelease(k)
    return result


def main() -> int:
    try:
        pid = int(subprocess.check_output(["pgrep", "-x", "QQ"]).split()[0])
    except Exception:
        print("QQ 未运行")
        return 1

    wins = windows_of(pid)
    print(f"QQ pid={pid}，可见窗口 {len(wins)} 个")
    for w in wins:
        print(f"  id={w['id']} layer={w['layer']}")
    if not wins:
        print("没有可截的窗口")
        return 1

    main_win = min(wins, key=lambda w: w["layer"])
    out = Path("/tmp/qq_capture_test.png")
    if out.exists():
        out.unlink()
    r = subprocess.run(
        ["screencapture", "-x", "-o", f"-l{main_win['id']}", str(out)],
        capture_output=True,
        text=True,
    )
    print("screencapture stderr:", r.stderr.strip() or "(空)")
    if out.exists() and out.stat().st_size > 0:
        print(f"✅ 截图成功：{out} ({out.stat().st_size} bytes)")
        return 0
    print("❌ 截图失败 —— 到 系统设置 → 隐私与安全性 → 屏幕录制 授权终端")
    return 2


if __name__ == "__main__":
    sys.exit(main())
