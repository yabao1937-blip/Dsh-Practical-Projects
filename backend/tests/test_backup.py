# -*- coding: utf-8 -*-
"""force=true 之前自动备份的用例（2026-09-11 事故的系统性防护）。

事故经过：我用一条带空 store 的 force PUT 把真实库 529 条记录整体清空。
能恢复纯属运气（主库文件恰好停在更早的 checkpoint，旧页还没被覆盖）。
从此 force 必须先留一份可回滚的快照。
"""
import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
SEED = json.loads((Path(__file__).resolve().parent.parent / "data" / "seed_store.json")
                  .read_text(encoding="utf-8"))


def _count(db_path, table):
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute("select count(*) from " + table).fetchone()[0]
    finally:
        con.close()


def _category_counts(db_path):
    con = sqlite3.connect(str(db_path))
    try:
        return {cat: con.execute(
            "select count(*) from coal_records where category=?", (cat,)).fetchone()[0]
            for cat in ("coarse", "float", "ash_density")}
    finally:
        con.close()


def test_force_writes_backup_that_contains_pre_force_data():
    """force 覆盖前必须留下快照，且快照里是**覆盖前**的数据（可回滚）。"""
    client.put("/api/v1/state?force=true", json=SEED)      # 已知状态：113 粗 + 5 浮 + 124 灰分密度
    before = client.get("/api/v1/state").json()
    assert len(before["coarseCoal"]) == 113
    # 注意 coal_records 是**三个类别合计**（242），别拿它去对 113
    want = {"coarse": 113, "float": 5, "ash_density": 124}

    empty = {"coarseCoal": [], "floatCoal": [], "calcLogs": []}
    j = client.put("/api/v1/state?force=true", json=empty).json()
    assert j["ok"] is True
    backup = j.get("backup")
    assert backup, "force 写入必须返回备份路径"
    bpath = Path(backup)
    assert bpath.exists(), f"备份文件不存在: {backup}"
    # 备份里必须是覆盖前的那三类记录 —— 这才叫"可回滚"
    assert _category_counts(bpath) == want, "备份内容不是覆盖前的数据"
    # 现库确实被清空了（force 的语义未变）
    assert len(client.get("/api/v1/state").json()["coarseCoal"]) == 0
    # 收尾：从备份恢复（这本身就是一次真实回滚演练）。
    # 注意最后那条断言：第二次 force 不能把第一次的快照覆盖掉 ——
    # 第一版用秒级时间戳，同一秒内的两次 force 撞名互相覆盖，正是这条断言抓到的。
    assert client.put("/api/v1/state?force=true", json=SEED).json()["ok"] is True
    assert _category_counts(Path(backup)) == want, "第一次的快照被覆盖了（回滚点丢失）"
    assert len(client.get("/api/v1/state").json()["coarseCoal"]) == 113


def test_normal_write_does_not_backup():
    """非 force 的普通镜像不做备份（否则每次镜像都复制一份库，纯浪费）。"""
    client.put("/api/v1/state?force=true", json=SEED)
    j = client.put("/api/v1/state", json=SEED).json()
    assert j["ok"] is True
    assert j.get("backup") is None


def test_backup_keeps_only_recent_copies(monkeypatch):
    """备份数量有上限，避免无限增长。"""
    from app.config import DATABASE_URL
    from app.services import migrate

    src = Path(DATABASE_URL.split("sqlite:///", 1)[-1])
    monkeypatch.setattr(migrate, "BACKUP_KEEP", 2)
    for _ in range(4):
        assert migrate.backup_database("keeptest")
    kept = sorted((src.parent / "backups").glob(f"{src.stem}-keeptest-*.db"))
    assert len(kept) <= 2, f"应只保留最近 2 份，实际 {len(kept)}"
    for f in kept:
        f.unlink(missing_ok=True)
