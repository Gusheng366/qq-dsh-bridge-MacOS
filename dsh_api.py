#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH Web API 客户端：离线铸造浏览器会话 Cookie，直接调用运行中的 DSH 服务。

这样 QQ 桥接复用 web profile 的模型与凭据（workbuddy / deepseek-v4.1-flash），
不再另起 headless 进程消耗 DeepSeek 官方 API 额度。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

COOKIE_PREFIX = "dsh-auth-"
PAYLOAD_VERSION = 1
SECRET_BYTES = 32
# Cookie 有效期 1 小时，提前 5 分钟重铸
COOKIE_LIFETIME_MS = 60 * 60 * 1000
COOKIE_REFRESH_MARGIN_MS = 5 * 60 * 1000


def _rpc_id() -> str:
    import uuid
    return f"rpc-{uuid.uuid4()}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def read_browser_session_secret() -> bytes | None:
    """读取 DSH 持久化的浏览器会话签名密钥（32 字节）。"""
    cred_path = Path.home() / ".dsh" / ".credentials.yaml"
    try:
        text = cred_path.read_text("utf-8")
    except OSError:
        return None
    # 轻量解析：定位 client-connection/browser-session 下的 secret
    m = re.search(
        r"client-connection/browser-session:\s*\n(?:.*\n)*?.*?secret:\s*([A-Za-z0-9_-]+)",
        text,
    )
    if not m:
        return None
    raw = m.group(1)
    padded = raw.replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)
    try:
        secret = base64.b64decode(padded)
    except Exception:
        return None
    return secret if len(secret) == SECRET_BYTES else None


def mint_cookie(base_url: str, secret: bytes, lifetime_ms: int = COOKIE_LIFETIME_MS) -> str:
    """铸造可直接放进 Cookie 头的 name=value。"""
    authority = urllib.parse.urlsplit(base_url).netloc
    if not authority:
        raise ValueError("baseUrl 缺少 authority")
    issued_at = int(time.time() * 1000)
    expires_at = issued_at + lifetime_ms
    payload = json.dumps(
        {
            "version": PAYLOAD_VERSION,
            "authority": authority,
            "issuedAt": issued_at,
            "expiresAt": expires_at,
        },
        separators=(",", ":"),
    ).encode()
    body = _b64url(payload)
    sig = _b64url(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    name = COOKIE_PREFIX + _b64url(hashlib.sha256(authority.encode()).digest())
    return f"{name}=v1.{body}.{sig}"


def _post_json(url: str, payload: dict, cookie: str, timeout: int = 60) -> tuple[int, Any]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Cookie": cookie,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


class DshApi:
    """运行中 DSH Web 服务的极简客户端。"""

    def __init__(self, base_url: str = "http://127.0.0.1:3080"):
        self.base_url = base_url.rstrip("/")
        self._cookie = ""
        self._cookie_at = 0.0

    def _ensure_cookie(self, force: bool = False) -> str:
        now = time.time() * 1000
        if (
            force
            or not self._cookie
            or now - self._cookie_at > COOKIE_LIFETIME_MS - COOKIE_REFRESH_MARGIN_MS
        ):
            secret = read_browser_session_secret()
            if secret is None:
                raise RuntimeError("无法读取 browser-session 签名密钥")
            self._cookie = mint_cookie(self.base_url, secret)
            self._cookie_at = now
        return self._cookie

    def rpc(self, method_path: str, payload: dict | None = None, timeout: int = 120):
        """调用 /api/<namespace>/<method>，包成 client-request 信封。"""
        cid = _rpc_id()
        envelope = {
            "type": "client-request",
            "rpcId": cid,
            "method": method_path,
            "payload": {"args": payload or {}},
        }
        cookie = self._ensure_cookie()
        url = f"{self.base_url}/api/{method_path}"
        status, data = _post_json(url, envelope, cookie, timeout)
        if status == 401:
            cookie = self._ensure_cookie(force=True)
            status, data = _post_json(url, envelope, cookie, timeout)
        return status, data

    # ---- 高层封装 ----
    def ping(self) -> bool:
        status, _ = self.rpc("session/list", {})
        return status == 200
