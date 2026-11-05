# -*- coding: utf-8 -*-
"""2026-11-05 安全审计整改项的回归测试。

覆盖三处：
  ① 离线门户 BoundedThreadingHTTPServer：线程封顶（超出即 503）与 Handler 超时常量；
  ② apply_update.check_expected_sha256：带外整包摘要校验（匹配通过 / 不匹配拒绝）；
  ③ package_windows_offline：certs/ 目录不再被打包（源码级断言，不执行打包）。
"""
import importlib.util
import socket
import threading
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, BACKEND / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------- ① 门户线程封顶 ----------------

def test_portal_thread_cap_constants():
    mod = _load("offline_transfer_under_test", "scripts/offline_transfer.py")
    assert mod.HANDLER_TIMEOUT == 30
    assert mod.BoundedThreadingHTTPServer.max_handler_threads == 8
    assert mod.BoundedThreadingHTTPServer.daemon_threads is True


def test_portal_rejects_beyond_thread_cap():
    """并发超过 max_handler_threads 的连接必须立刻拿到 503，而不是无限挂起。"""
    from http.server import BaseHTTPRequestHandler
    mod = _load("offline_transfer_cap", "scripts/offline_transfer.py")

    class SlowHandler(BaseHTTPRequestHandler):
        timeout = mod.HANDLER_TIMEOUT

        def do_GET(self):
            time.sleep(0.5)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = mod.BoundedThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        results = []
        lock = threading.Lock()

        def hit():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                    sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                    data = sock.recv(1024)
                with lock:
                    results.append(data[:12])
            except OSError as exc:
                with lock:
                    results.append(b"ERR:" + str(exc).encode()[:40])

        # 12 个并发 > 8 个线程位：至少一个必须收到 503，且没有任何连接挂死
        workers = [threading.Thread(target=hit) for _ in range(12)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=10)
            assert not w.is_alive(), "connection hung: cap did not reject nor serve"
        assert any(r.startswith(b"HTTP/1.1 503") for r in results), results
        # 没有占到位子的连接必须拿到正常 200（封顶不影响合法负载）；
        # BaseHTTPRequestHandler 默认 protocol_version=HTTP/1.0，两种版本都算通过。
        assert any(r.startswith((b"HTTP/1.0 200", b"HTTP/1.1 200")) for r in results), results
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------- ② 带外整包摘要 ----------------

def test_check_expected_sha256_roundtrip(tmp_path):
    au = _load("apply_update_under_test", "scripts/windows_offline/apply_update.py")
    pkg = tmp_path / "ProjectUpdate.zip"
    pkg.write_bytes(b"dummy package bytes")
    import hashlib
    good = hashlib.sha256(pkg.read_bytes()).hexdigest()
    assert au.check_expected_sha256(pkg, good) == good          # 大小写不敏感
    assert au.check_expected_sha256(pkg, good.upper()) == good
    with pytest.raises(ValueError, match="sha256 mismatch"):
        au.check_expected_sha256(pkg, "0" * 64)


# ---------------- ③ certs/ 不打包 ----------------

def test_packager_never_copies_certs():
    """源码级守卫：package_windows_offline 不得再出现把 certs 目录复制进 stage 的语句。"""
    src = (BACKEND / "scripts/package_windows_offline.py").read_text(encoding="utf-8")
    assert "shutil.copytree(certs" not in src, "certs/ 目录不得再被打进分发包（含 OPC 私钥）"
    assert "certs_excluded=True" in src, "manifest 需保留 certs_excluded 标记以便追溯"
