# -*- coding: utf-8 -*-
"""写接口访问控制（token + force 限本机）。

威胁模型（写清楚它挡得住什么、挡不住什么）
挡得住：
  · 同网段任意主机用 curl/脚本盲扫写入或清库（本服务 bind 0.0.0.0，此前零鉴权，
    一条 `curl -X PUT .../api/v1/state?force=true -d @wipe.json` 就能整库覆盖）；
  · 误点链接/被诱导执行的写入请求。
挡不住（诚实说明）：
  · 已经用浏览器打开本页面的人 —— token 随页面下发（见下），能读到它；
  · 能登录服务器本机的人（本机回环默认免 token，见 DMCS_ALLOW_LOCALHOST_WRITES）。
所以它把"任何能连到端口的人都能改写数据"降级为"需要拿到页面下发的 token"，
而**真正具有破坏性的整库回退（force=true）永不接受非本机来源** —— 这一条是硬规则。

token 从哪来
  · 环境变量 DMCS_WRITE_TOKEN 优先；
  · 否则首次启动时自动生成一个随机 token，持久化到 backend/data/write_token.txt
    （该文件在 .gitignore 里）→ 重启不变，浏览器不需重新配置；
  · DMCS_WRITE_TOKEN=off 显式关闭鉴权（回到旧行为，启动时打警告）。
"""
from __future__ import annotations

import ipaddress
import logging
import os
import secrets
from pathlib import Path

from fastapi import HTTPException, Request

from .config import DATA_DIR, REMOTE_READONLY

TOKEN_FILE = DATA_DIR / "write_token.txt"
TOKEN_HEADER = "X-DMCS-Token"
_AUTH_LOG = logging.getLogger("dmcs.auth")


def warn_if_auth_disabled() -> bool:
    """非只读模式下写鉴权被关闭时，启动即大声告警（docstring 承诺过的警告，2026-11-05 落实）。"""
    if REMOTE_READONLY or auth_enabled():
        return False
    _AUTH_LOG.warning(
        "DMCS 写鉴权已关闭（DMCS_WRITE_TOKEN/DMCS_WRITE_AUTH=off）："
        "所有写接口对网络完全开放！仅建议在受信任的隔离环境使用。")
    return True

# 本机回环写入默认放行：现场脚本、pytest、E2E 都从本机发起，
# 而真实的暴露面是同网段的其它主机。设 DMCS_ALLOW_LOCALHOST_WRITES=0 可关掉。
_LOCALHOST_WRITES = os.environ.get("DMCS_ALLOW_LOCALHOST_WRITES", "1") != "0"


def _load_or_create_token() -> str | None:
    if REMOTE_READONLY:
        return None
    raw = os.environ.get("DMCS_WRITE_TOKEN")
    if raw is not None:
        raw = raw.strip()
        if raw.lower() in ("off", "none", "disable", "disabled", "0"):
            return None
        return raw or None
    env_off = os.environ.get("DMCS_WRITE_AUTH", "").strip().lower()
    if env_off in ("off", "0", "false", "disable", "disabled"):
        return None
    try:
        if TOKEN_FILE.exists():
            saved = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if saved:
                return saved
        token = secrets.token_urlsafe(24)
        TOKEN_FILE.write_text(token, encoding="utf-8")
        return token
    except Exception:  # 只读文件系统等：退化为"本次进程内有效"的随机 token
        return secrets.token_urlsafe(24)


WRITE_TOKEN = _load_or_create_token()


def is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else None
    if not host:
        return False
    if host in ("testclient", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def auth_enabled() -> bool:
    return WRITE_TOKEN is not None


def _presented_token(request: Request) -> str | None:
    tok = request.headers.get(TOKEN_HEADER) or request.headers.get(TOKEN_HEADER.lower())
    if tok:
        return tok.strip()
    # 2026-11-05 安全审计后移除 ?token= query 回退：令牌进 URL 会落入 uvicorn 访问日志
    # （%TEMP%/dmcs_server.log，90 天保留）等中间产物。浏览器/前端一向只走 header；
    # curl 调试请用 -H "X-DMCS-Token: ..."。
    return None


async def require_write(request: Request) -> None:
    """写接口依赖：校验 token（本机回环可免）。"""
    if REMOTE_READONLY:
        raise HTTPException(status_code=403,detail={'error_code':'readonly_test_server','message':'测试服只读主机数据，禁止保存到生产数据库'})
    if not auth_enabled():
        return
    if _LOCALHOST_WRITES and is_loopback(request):
        return
    if secrets.compare_digest(_presented_token(request) or "", WRITE_TOKEN or ""):
        return
    raise HTTPException(status_code=401, detail={
        "ok": False, "error": "unauthorized: 写接口需要 X-DMCS-Token（token 见服务器 "
                              "backend/data/write_token.txt，或页面已自动注入）",
    })


async def require_force_allowed(request: Request) -> None:
    """force=true（整库覆盖/清空）**只接受本机来源**。

    这一条与 token 独立：即使有人拿到了 token（例如用浏览器打开了页面），
    也不能从别的机器把整库洗掉。现场所有正常写入都不需要 force
    （前端从不带 force，见 api.js putState 的调用者）。
    """
    if not is_loopback(request):
        raise HTTPException(status_code=403, detail={
            "ok": False,
            "error": "forbidden: force=true（整库覆盖）只允许从服务器本机发起；"
                     "如需从远端整库恢复，请在服务器上执行",
        })
