"""Retiring a point edits only the local collector config and keeps a backup."""
import json
import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("retire_point", SCRIPTS / "retire_telemetry_point.py")
retire = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retire)


def config(path, points):
    path.write_text(json.dumps({
        "enabled": False,
        "source_id": "kepserver-main",
        "name": "KEPServer",
        "points": points,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def point(key, item):
    return {"key": key, "name": key, "node_id": "ns=2;s=" + item, "item_id": item}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(retire, "BACKUP_DIR", tmp_path / "backups")
    return tmp_path


def test_retire_removes_point_and_keeps_a_backup(isolated, capsys):
    path = config(isolated / "kepserver.json", [point("a303", "A303/MD"), point("a304", "A304/MD")])
    assert retire.main(["--config", str(path), "--item", "A303/MD", "--apply"]) == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [p["item_id"] for p in data["points"]] == ["A304/MD"]
    backups = list((isolated / "backups").glob("kepserver.json.*.bak"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8"))["points"][0]["item_id"] == "A303/MD"
    assert "下一步" in capsys.readouterr().out


def test_retire_is_idempotent_and_preview_never_writes(isolated, capsys):
    path = config(isolated / "kepserver.json", [point("a304", "A304/MD")])
    before = path.read_bytes()
    assert retire.main(["--config", str(path), "--item", "A303/MD", "--apply"]) == 0
    assert path.read_bytes() == before
    assert "未找到" in capsys.readouterr().out

    path = config(isolated / "kepserver.json", [point("a303", "A303/MD"), point("a304", "A304/MD")])
    before = path.read_bytes()
    assert retire.main(["--config", str(path), "--item", "A303/MD"]) == 0
    assert path.read_bytes() == before
    assert "预览结束" in capsys.readouterr().out


def test_retire_rejects_invalid_remaining_config(isolated):
    path = config(isolated / "kepserver.json", [point("a303", "A303/MD"), point("bad key", "A304/MD")])
    original = path.read_bytes()
    with pytest.raises(ValueError):
        retire.main(["--config", str(path), "--item", "A303/MD", "--apply"])
    assert path.read_bytes() == original
