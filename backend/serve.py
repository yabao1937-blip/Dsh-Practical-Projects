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

log_path = pathlib.Path(os.environ.get("DMCS_LOG")
                        or (pathlib.Path(os.environ.get("TEMP") or ".") / "dmcs_server.log"))
log_path.parent.mkdir(parents=True, exist_ok=True)
# 新文件先写 UTF-8 BOM：否则 Windows 记事本/PowerShell 5.1 会按 GBK 打开，中文日志全是乱码
if not log_path.exists() or log_path.stat().st_size == 0:
    with open(log_path, "wb") as _seed:
        _seed.write(b"\xef\xbb\xbf")
_log = open(log_path, "a", encoding="utf-8", buffering=1)      # 行缓冲，崩溃前的行也能落盘
sys.stdout = sys.stderr = _log


def _say(msg):
    print("[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


_say("serve.py 启动：pid=%d，cwd=%s，日志=%s" % (os.getpid(), os.getcwd(), log_path))
try:
    import uvicorn
except Exception as exc:                                        # 依赖缺失也要留痕
    _say("导入 uvicorn 失败：%r" % exc)
    raise

host = os.environ.get("DMCS_HOST", "0.0.0.0")
port = int(os.environ.get("DMCS_PORT", "8000"))
_say("uvicorn.run('app.main:app', host=%s, port=%d)" % (host, port))
try:
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")
except BaseException as exc:                                     # 含 KeyboardInterrupt：留下退出原因
    _say("uvicorn 退出：%r" % exc)
    raise
finally:
    _say("serve.py 结束：pid=%d" % os.getpid())
