"""CLI 配置校验与启动错误，不连接任何设备。"""
import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).parents[1]


def invoke(tmp_path, cfg, database_url, *extra):
    path = tmp_path / "collector.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    env = dict(os.environ, DMCS_DATABASE_URL=database_url, DMCS_WRITE_AUTH="off", PYTHONUTF8="1")
    return subprocess.run([sys.executable, "collector.py", "--config", str(path), *extra], cwd=BACKEND, env=env, capture_output=True, text=True, encoding="utf-8", timeout=10)


def test_disabled_collector_never_initializes_database(tmp_path):
    result = invoke(tmp_path, {"enabled": False}, "unknown-dialect://synthetic-private-value")
    assert result.returncode == 0 and "enabled=False" in result.stdout
    assert "synthetic-private" not in result.stdout + result.stderr


def test_validate_enabled_configuration_does_not_connect_or_initialize_database(tmp_path):
    cfg = {"enabled": True, "endpoint": "opc.tcp://127.0.0.1:1", "security": "None", "allow_insecure": True,
        "points": [{"key": "density", "name": "test", "node_id": "ns=2;s=Test"}]}
    result = invoke(tmp_path, cfg, "unknown-dialect://synthetic-private-value", "--validate")
    assert result.returncode == 0 and "enabled=True" in result.stdout
    assert "synthetic-private" not in result.stdout + result.stderr


def test_startup_database_failure_is_sanitized_before_network_connection(tmp_path):
    cfg = {"enabled": True, "endpoint": "opc.tcp://127.0.0.1:1", "security": "None", "allow_insecure": True,
        "points": [{"key": "density", "name": "test", "node_id": "ns=2;s=Test"}]}
    result = invoke(tmp_path, cfg, "unknown-dialect://synthetic-private-value", "--once")
    assert result.returncode == 1 and "collector_startup_failed" in result.stderr
    assert "Traceback" not in result.stderr and "synthetic-private" not in result.stderr + result.stdout
