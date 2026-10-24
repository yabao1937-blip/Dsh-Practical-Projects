# -*- coding: utf-8 -*-
"""无人值守启动后端：自己把 stdout/stderr 落到日志文件，再交给 uvicorn。

为什么要这个文件（2026-09-30 实测）：
  · 用 `cmd /c "... > log 2>&1"` 重定向，在 DETACHED_PROCESS（无控制台）下两个日志文件都是
    **0 字节** —— uvicorn 的 STARTUP/访问日志一行都没落盘，出事时等于没有现场；
  · python 自己 open() 日志文件最可靠：行缓冲、追加写、启动就打时间戳与 pid。

用法（由 backend/scripts/start_server.ps1 以无控制台方式调起）：
    python -u serve.py                    # 端口 8000，日志 %TEMP%\\dmcs_server.log
环境变量：DMCS_PORT（默认 8000）、DMCS_LOG（默认 %TEMP%\\dmcs_server.log）、DMCS_HOST（默认 0.0.0.0）
"""
import datetime
import os
import pathlib
import sys

from app.storage_retention.files import RotatingWebLog

from client_mode import client_url
from remote_mode import settings as remote_settings
_remote_readonly = remote_settings()
if not _remote_readonly and (_client_target := client_url()):
    print('Client mode: open '+_client_target+'; local database service is disabled.')
    raise SystemExit(0)

log_path = pathlib.Path(os.environ.get("DMCS_LOG")
                        or (pathlib.Path(os.environ.get("TEMP") or ".") / "dmcs_server.log"))
log_path.parent.mkdir(parents=True, exist_ok=True)
# 保持当前日志地址，已关闭分卷另存并认证为90天错误日志。
_log = RotatingWebLog(log_path, pathlib.Path(__file__).resolve().parent.parent)
sys.stdout = sys.stderr = _log


def _say(msg):
    print("[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


_say("serve.py 启动：pid=%d，cwd=%s，日志=%s" % (os.getpid(), os.getcwd(), log_path))
if _log.last_rotation_error:
    _say("日志分卷暂缓，当前日志已保留：" + _log.last_rotation_error)
try:
    import uvicorn
except Exception as exc:                                        # 依赖缺失也要留痕
    _say("导入 uvicorn 失败：%r" % exc)
    raise

host = os.environ.get("DMCS_HOST", "127.0.0.1" if _remote_readonly else "0.0.0.0")
port = int(os.environ.get("DMCS_PORT", "8000"))
_say("uvicorn.run('app.main:app', host=%s, port=%d)" % (host, port))
try:
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")
except BaseException as exc:                                     # 含 KeyboardInterrupt：留下退出原因
    _say("uvicorn 退出：%r" % exc)
    raise
finally:
    _say("serve.py 结束：pid=%d" % os.getpid())
    _log.close()
