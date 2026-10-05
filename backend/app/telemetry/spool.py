"""数据库写入前的持久批次队列：原子落盘，提交后确认，重启可重放。"""
import json
import os
import re
from pathlib import Path


class SpoolError(RuntimeError):
    pass


class DurableSpool:
    def __init__(self, root: Path, source_id: str, max_bytes: int = 50 * 1024 * 1024):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", source_id):
            raise SpoolError("invalid spool source")
        self.source_id, self.max_bytes = source_id, max_bytes
        self.folder = (root / source_id).resolve()
        self.folder.mkdir(parents=True, exist_ok=True)

    def pending(self):
        return sorted(self.folder.glob("batch-*.json"))

    def enqueue(self, batch):
        stamp = re.sub(r"[^0-9]", "", batch["samples"][0]["received_at"])
        batch_id = batch["batch_id"]
        if batch["source_id"] != self.source_id or not re.fullmatch(r"[a-f0-9]{32}", batch_id):
            raise SpoolError("invalid spool batch")
        payload = json.dumps(batch, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if sum(p.stat().st_size for p in self.pending()) + len(payload) > self.max_bytes:
            raise SpoolError("spool capacity exceeded")
        target = self.folder / f"batch-{stamp}-{batch_id}.json"
        temporary = target.with_suffix(".tmp")
        with temporary.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        return target

    def read(self, path):
        try:
            batch = json.loads(path.read_text(encoding="utf-8"))
            if batch["source_id"] != self.source_id or not batch["samples"] or not re.fullmatch(r"[a-f0-9]{32}", batch["batch_id"]):
                raise SpoolError("invalid spool content")
            return batch
        except (ValueError, KeyError, TypeError) as exc:
            raise SpoolError("invalid spool content") from exc

    def ack(self, path: Path):
        if path.resolve().parent != self.folder or not re.fullmatch(r"batch-\d+-[a-f0-9]{32}\.json", path.name):
            raise SpoolError("invalid spool acknowledgement")
        path.unlink(missing_ok=True)

    def stats(self):
        files = self.pending()
        return {"batches": len(files), "bytes": sum(p.stat().st_size for p in files)}
