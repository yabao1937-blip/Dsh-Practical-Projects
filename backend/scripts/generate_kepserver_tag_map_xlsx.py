"""Export the enabled physical point mapping through the bundled Artifact Tool."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies"


def natural_key(text: str):
    return [(0, int(part)) if part.isdigit() else (1, part.casefold())
            for part in re.split(r"(\d+)", text)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", type=Path, default=RUNTIME / "node/bin/node.exe")
    parser.add_argument("--node-modules", type=Path, default=RUNTIME / "node/node_modules")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    config = json.loads((ROOT / "backend/config/kepserver.local.json").read_text(encoding="utf-8-sig"))
    source_id = config["source_id"]
    configured = {point["key"]: point for point in config["points"]}
    if len(configured) != 50 or len(config["points"]) != 50:
        raise ValueError("Expected exactly 50 configured physical points")

    database = ROOT / "backend/data/dense_medium.db"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT key, config FROM telemetry_points WHERE source_id=? AND enabled=1",
            (source_id,),
        ).fetchall()
    if {key for key, _ in rows} != set(configured):
        raise ValueError("Enabled database points do not match the current collector config")

    points = []
    for key, raw_config in rows:
        point = json.loads(raw_config)
        for field in ("node_id", "item_id", "device", "tag_name", "description"):
            if point.get(field, "") != configured[key].get(field, ""):
                raise ValueError(f"Database/config mismatch: {key}, {field}")
        expected = "tag_" + hashlib.sha256(point["item_id"].encode("utf-8")).hexdigest()[:20]
        if key != expected:
            raise ValueError(f"Unexpected point key: {key}")
        points.append({"key": key, **{field: point.get(field, "") for field in
                       ("device", "tag_name", "description", "item_id", "node_id")}})
    for field in ("key", "item_id", "node_id"):
        if len({point[field] for point in points}) != 50:
            raise ValueError(f"Duplicate {field}")
    if any(point["tag_name"].split("/")[0] in {"501A", "502A"} for point in points):
        raise ValueError("Disabled 501A/502A points must not be exported")
    points.sort(key=lambda point: natural_key(point["tag_name"]))

    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    output = args.output or ROOT / "docs" / f"KEPServer50点位tag对照表-{today.replace('-', '')}.xlsx"
    if output.exists():
        raise FileExistsError(f"Use --output with a new filename: {output}")
    evidence = ROOT / "docs/verification" / f"kepserver-tag-map-{today.replace('-', '')}"
    evidence.mkdir(parents=True, exist_ok=True)
    snapshot = evidence / "source.json"
    snapshot.write_text(json.dumps({"source_id": source_id, "date": today, "points": points},
                                  ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    junction = Path(__file__).resolve().parent / "node_modules"
    if not junction.exists():
        # The generator directory resolves only the desktop's bundled dependencies.
        command = "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path '" + str(junction).replace("'", "''") + "' -Target '" + str(args.node_modules).replace("'", "''") + "' | Out-Null"
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], check=True)
    elif junction.resolve() != args.node_modules.resolve():
        raise ValueError("Existing node_modules does not point to the bundled runtime")
    subprocess.run([str(args.node), str(Path(__file__).with_suffix(".mjs")),
                    str(snapshot), str(output), str(evidence)], check=True)


if __name__ == "__main__":
    main()
