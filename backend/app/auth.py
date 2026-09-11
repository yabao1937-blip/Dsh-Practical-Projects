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
import os
import secrets
from pathlib import Path

from fastapi import HTTPException, Request

from .config import DATA_DIR

TOKEN_FILE = DATA_DIR / "write_token.txt"
TOKEN_HEADER = "X-DMCS-Token"

# 本机回环写入默认放行：现场脚本、pytest、E2E 都从本机发起，
# 而真实的暴露面是同网段的其它主机。设 DMCS_ALLOW_LOCALHOST_WRITES=0 可关掉。
_LOCALHOST_WRITES = os.environ.get("DMCS_ALLOW_LOCALHOST_WRITES", "1") != "0"


def _load_or_create_token() -> str | None:
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
    # 兜底：允许 ?token=（便于 curl 手工调试；浏览器走 header）
    return request.query_params.get("token")


async def require_write(request: Request) -> None:
    """写接口依赖：校验 token（本机回环可免）。"""
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
