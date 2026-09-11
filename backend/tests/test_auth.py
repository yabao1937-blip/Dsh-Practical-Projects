# -*- coding: utf-8 -*-
"""写接口访问控制（app/auth.py）的用例。

威胁模型与设计说明见 app/auth.py 顶部注释。这里覆盖四条硬规则：
  ① 写接口在启用鉴权后**必须**带 X-DMCS-Token；
  ② 本机回环可免 token（现场脚本/E2E 从本机发起）；
  ③ force=true（整库覆盖/清空）**只接受本机来源** —— 即便带着正确 token 也不行；
  ④ 首页把 token 以 <meta> 下发（浏览器零配置）。
"""
import importlib

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import app

SEED = {"coarseCoal": [{"timestamp": "2026-07-01 08:00:00", "system": "A",
                        "ash_content": 12.5}], "floatCoal": [], "calcLogs": []}

# TestClient 的 client host 默认是 "testclient"，auth.is_loopback 把它当本机（见 auth.py）——
# 这样既有的写用例（force=true 等）不受鉴权影响，同时也能用 client=("10.0.0.5", ...) 模拟远端。
REMOTE = ("10.0.0.5", 51234)


@pytest.fixture
def token_on(monkeypatch):
    """启用鉴权（用一个确定的 token，避免依赖磁盘上的 write_token.txt）。"""
    monkeypatch.setattr(auth, "WRITE_TOKEN", "test-token-123", raising=False)
    yield "test-token-123"


@pytest.fixture
def token_off(monkeypatch):
    monkeypatch.setattr(auth, "WRITE_TOKEN", None, raising=False)
    yield


@pytest.fixture
def local_client():
    return TestClient(app)


@pytest.fixture
def remote_client():
    return TestClient(app, client=REMOTE)


def test_write_requires_token_from_remote(token_on, remote_client):
    """远端无 token：所有写接口都必须 401（读接口不受影响）。"""
    r = remote_client.put("/api/v1/state", json=SEED)
    assert r.status_code == 401, r.text
    assert "X-DMCS-Token" in r.json()["detail"]["error"]
    # 读接口仍然开放（现场看板不需要 token）
    assert remote_client.get("/api/v1/state").status_code == 200


def test_write_with_token_from_remote(token_on, remote_client):
    """远端带正确 token：普通写入**通过鉴权层**。

    这里只断言"没有被鉴权挡住"：写入本身可能因防回退守卫而 ok=False
    （测试库里已有记录），那是另一条规则的事，不该混在这条用例里。
    """
    r = remote_client.put("/api/v1/state", json=SEED,
                          headers={"X-DMCS-Token": "test-token-123"})
    assert r.status_code == 200, r.text
    assert "unauthorized" not in r.text


def test_write_with_wrong_token_rejected(token_on, remote_client):
    r = remote_client.put("/api/v1/state", json=SEED, headers={"X-DMCS-Token": "wrong"})
    assert r.status_code == 401


def test_loopback_exempt_from_token(token_on, local_client):
    """本机回环免 token：现场脚本、pytest、E2E 都从本机跑，不该被挡。"""
    assert local_client.put("/api/v1/state?force=true", json=SEED).json()["ok"] is True


def test_other_write_endpoints_require_token(token_on, remote_client):
    """不只是 /state：其余写接口同样要 token（逐条点名，避免漏掉某个路由）。"""
    cases = [
        ("post", "/api/v1/records", {"category": "coarse", "ts": "2026-07-02 08:00:00"}),
        ("post", "/api/v1/samples/heavy-ash", {"ts": "2026-07-02 08:00:00", "rho": 1.5,
                                               "ash_content": 8.0}),
        ("put", "/api/v1/settings/ash_target", {"value": 8.5}),
        ("put", "/api/v1/inputs/density", {"value": 1.5}),
        ("post", "/api/v1/manual-entries", {"ts": "2026-07-02 08:00:00", "category": "coarse"}),
        ("post", "/api/v1/migrate/localstorage", SEED),
    ]
    for method, url, body in cases:
        r = getattr(remote_client, method)(url, json=body)
        assert r.status_code == 401, f"{method.upper()} {url} 未受保护: {r.status_code}"


def test_force_only_from_loopback(token_on, remote_client):
    """force=true（整库覆盖）即便带对 token 也不能从远端发起 —— 这条与 token 独立。"""
    r = remote_client.put("/api/v1/state?force=true", json=SEED,
                          headers={"X-DMCS-Token": "test-token-123"})
    assert r.status_code == 403, r.text
    assert "只允许从服务器本机发起" in r.json()["detail"]["error"]
    # 非 force 的整库写（带 token）不受此限制
    assert remote_client.put("/api/v1/state", json=SEED,
                            headers={"X-DMCS-Token": "test-token-123"}).status_code == 200


def test_auth_disabled_by_env(token_off, remote_client):
    """显式关闭鉴权（DMCS_WRITE_TOKEN=off）时回到旧行为：远端也能写。"""
    assert remote_client.put("/api/v1/state", json=SEED).json()["ok"] is True


def test_index_injects_token_meta(token_on, local_client):
    """首页把 token 以 <meta name=\"dmcs-token\"> 下发（浏览器零配置）。"""
    html = local_client.get("/").text
    assert 'name="dmcs-token"' in html and "test-token-123" in html
    assert "<head>" in html and html.index("dmcs-token") < html.index("</head>")


def test_index_without_auth_has_no_token_meta(token_off, local_client):
    html = local_client.get("/").text
    assert "dmcs-token" not in html
    assert "重介密控系统" in html          # 页面本身照常


def test_token_file_is_used_when_env_absent(tmp_path, monkeypatch):
    """没有环境变量时：首次生成并落盘，之后重启沿用同一个 token（浏览器不用重配）。"""
    monkeypatch.delenv("DMCS_WRITE_TOKEN", raising=False)
    monkeypatch.delenv("DMCS_WRITE_AUTH", raising=False)
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "write_token.txt")
    first = importlib.reload(auth)._load_or_create_token()
    assert first and auth.TOKEN_FILE.exists()
    second = auth._load_or_create_token()
    assert second == first
    monkeypatch.setenv("DMCS_WRITE_TOKEN", "off")
    assert auth._load_or_create_token() is None
    monkeypatch.undo()
    importlib.reload(auth)      # 还原模块状态，避免污染其它用例
