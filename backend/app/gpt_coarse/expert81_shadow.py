"""Local EXPERT8.1 shadow receipts. Nothing here writes a production database.

The journal is append-only through this API, hash chained, locked and fsynced.
Host sample time and notebook receipt time are deliberately different clocks.
Only a host anchor fetched *after* a durable event can seal that event.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import uuid

from .experiments import expert81 as core


UTC = timezone.utc
PRIMARY_GROUP = "0,1,1,1,0,0"
FEATURES = ("coal_amount", "sysA", "sysB", "sys401", "sys402", "desliming473", "desliming474", "level")
STATE_KEYS = FEATURES[1:7]
BLOCKED_ASSAYS = {"197"}
POLICY = {"selection": "latest-eligible-input-at-or-before-sample", "maximum_age_seconds": 30,
          "maximum_snapshot_age_seconds": 10, "maximum_nonlag_input_age_seconds": 10, "level_lag_seconds": 90,
          "level_lookback_seconds": 30, "same_timestamp_label": "excluded",
          "unverified_sample_clock": "unscored", "availability": "capture-then-host-seal"}
FROZEN_MODEL_PATH = Path(__file__).resolve().parents[2] / "models/gpt_expert8/20261012/model.json"
# This is the audited historical observation domain, not a physical off/on claim.
# Keep it independent of the active telemetry members, which now use RUNS.
LEGACY_SOURCE_SEMANTICS = {
    "schema": "expert81-desliming-source-semantics-v1",
    "members": {"desliming" + label: {"item_id": item, "node_id": "ns=2;s=" + item}
                for label, item in (("473", "TIA_3_3.TIA_3_3.473/if_prod"),
                                    ("474", "TIA_3_3.TIA_3_3.474/if_prod"))}}
# Full canonical artifact digest -> its complete history-event digest. The
# frozen C7 artifact predates explicit source metadata; only this audited
# artifact gets the compatibility binding. A new artifact needs a new audit.
AUDITED_LEGACY_ARTIFACTS = {
    "99aba01d22f84d04dc81627cbc85c48e79041cc1dedea18811fc204091565681":
        "1cd008a3bfe6e1b79ae2d4e299391c55d246e14aeb265562bed63283471e3db8"}
DEFINITION_FIELDS = ("item_id", "node_id", "unit", "scale", "offset", "value_type", "minimum", "maximum")


def stamp(value):
    return core.stamp(value)


def now_utc():
    return datetime.now(UTC)


def digest(value):
    return core.digest(value)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def implementation_hash():
    return hashlib.sha256(Path(core.__file__).read_bytes()).hexdigest()


class LedgerError(ValueError):
    pass


class ClockError(LedgerError):
    pass


def _source_evidence(artifact_hash, seed_manifest_hash):
    if (not isinstance(artifact_hash, str) or artifact_hash not in AUDITED_LEGACY_ARTIFACTS
            or AUDITED_LEGACY_ARTIFACTS[artifact_hash] != seed_manifest_hash):
        raise LedgerError("artifact has no audited desliming source semantics; use a separately audited model")
    return {"kind": "audited-frozen-artifact-source-binding-v1", "artifact_hash": artifact_hash,
            "seed_manifest_hash": seed_manifest_hash, "source_semantics_hash": digest(LEGACY_SOURCE_SEMANTICS)}


def _observed_source_semantics(cells):
    return {"schema": LEGACY_SOURCE_SEMANTICS["schema"], "members": {
        key: {"item_id": cells[key].get("item_id"),
              "node_id": cells[key].get("definition", {}).get("node_id")}
        for key in LEGACY_SOURCE_SEMANTICS["members"]}}


def _source_semantics_error(frame, expected):
    """Check actual point definitions, not just bits or a caller's domain name."""
    from ..telemetry.domain import definition_id

    if not isinstance(frame.get("source_semantics"), dict):
        return "desliming_source_semantics_missing_or_malformed"
    if frame["source_semantics"] != expected:
        return "unsupported_desliming_source_semantics"
    inputs = frame.get("inputs")
    if not isinstance(inputs, dict):
        return "desliming_source_metadata_missing_or_malformed"
    for key, identity in expected["members"].items():
        cell = inputs.get(key)
        if not isinstance(cell, dict) or not isinstance(cell.get("definition"), dict):
            return "desliming_source_metadata_missing_or_malformed"
        definition, sample = cell["definition"], cell.get("sample")
        if (cell.get("item_id") != identity["item_id"] or definition.get("item_id") != identity["item_id"]
                or definition.get("node_id") != identity["node_id"]):
            return "unsupported_desliming_source_semantics"
        if (cell.get("status") != "good" or not isinstance(sample, dict)
                or not isinstance(cell.get("source_id"), str) or not cell["source_id"]
                or not isinstance(cell.get("point_key"), str) or not cell["point_key"]
                or sample.get("key") != cell["point_key"]
                or sample.get("node_id") != identity["node_id"]
                or not _number(cell.get("value")) or cell["value"] not in (0, 1)
                or not (isinstance(sample.get("value"), bool) or _number(sample.get("value")))
                or sample["value"] not in (0, 1) or cell["value"] != sample["value"]
                or cell.get("definition_id") != definition_id(definition)
                or sample.get("definition_id") != cell["definition_id"]):
            return "unverified_desliming_source_definition"
    return None


class _HostAnchor(dict):
    """Runtime marker: deserialized user JSON cannot impersonate the transport."""


class _LiveFrame(dict):
    """Runtime marker: a file-supplied transport string is never formal evidence."""


class _FrozenSeedRecord(dict):
    """Created only after checking the complete pinned research artifact."""


def runtime_identity():
    cli = Path(__file__).resolve().parents[2] / "scripts" / "expert81_shadow.py"
    return {"runtime_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "cli_sha256": hashlib.sha256(cli.read_bytes()).hexdigest()}


class _Transaction:
    def __init__(self, stream, events, clock, blobs):
        self.stream, self.events, self.clock, self.blobs = stream, events, clock, blobs

    def _pack(self, value):
        if isinstance(value, list):
            return [self._pack(v) for v in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key == "training_records" and isinstance(item, list) and item:
                identity = digest(item)
                if identity not in self.blobs:
                    self.append("calibration_blob", {"records_sha256": identity, "records": item})
                result[key] = {"$calibration_records": identity}
            else:
                result[key] = self._pack(item)
        return result

    def append(self, kind, payload):
        packed = deepcopy(payload) if kind == "calibration_blob" else self._pack(payload)
        at = stamp(self.clock())
        if self.events and core.parse_time(at) < core.parse_time(self.events[-1]["recorded_at"]):
            raise ClockError("notebook clock moved backwards; open a new audited run")
        event = dict(seq=len(self.events) + 1, kind=kind, recorded_at=at,
                     payload=packed, payload_hash=digest(payload),
                     prev_hash=self.events[-1]["event_hash"] if self.events else None)
        event["event_hash"] = digest(event)
        raw = (json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
        self.stream.seek(0, os.SEEK_END)
        self.stream.write(raw)
        self.stream.flush()
        os.fsync(self.stream.fileno())
        if kind == "calibration_blob":
            self.blobs[payload["records_sha256"]] = deepcopy(payload["records"])
        # Event hashes identify exact stored bytes; payload_hash additionally
        # verifies the expanded core state/receipt returned to callers.
        event["payload"] = deepcopy(payload)
        self.events.append(event)
        return deepcopy(event)


class Journal:
    def __init__(self, path, *, clock=now_utc):
        self.path, self.clock = Path(path), clock

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # msvcrt locks the underlying file offset; an unbuffered stream keeps
        # Python seek(0) and the OS lock region identical even after long reads.
        with self.path.open("a+b", buffering=0) as stream:
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise LedgerError("shadow journal is locked by another process") from exc
            try:
                stream.seek(0)
                events, blobs = [], {}
                def expand(value):
                    if isinstance(value, list):
                        return [expand(v) for v in value]
                    if not isinstance(value, dict):
                        return value
                    if set(value) == {"$calibration_records"}:
                        return deepcopy(blobs[value["$calibration_records"]])
                    return {k: expand(v) for k, v in value.items()}
                # FileIO.readline would issue a Windows read for each byte.
                # Read the journal once while holding the same OS lock.
                for line in stream.read().splitlines(keepends=True):
                    try:
                        if not line.endswith(b"\n"):
                            raise ValueError("partial final line")
                        event = json.loads(line)
                        content = {k: v for k, v in event.items() if k != "event_hash"}
                        if (event["seq"] != len(events) + 1 or event["event_hash"] != digest(content)
                                or event["prev_hash"] != (events[-1]["event_hash"] if events else None)):
                            raise ValueError("sequence or hash mismatch")
                        if events and core.parse_time(event["recorded_at"]) < core.parse_time(events[-1]["recorded_at"]):
                            raise ValueError("notebook clock reversal")
                        if event["kind"] == "calibration_blob":
                            b = event["payload"]
                            if b["records_sha256"] != digest(b["records"]):
                                raise ValueError("calibration blob mismatch")
                            blobs[b["records_sha256"]] = b["records"]
                        else:
                            event["payload"] = expand(event["payload"])
                        if event.get("payload_hash") != digest(event["payload"]):
                            raise ValueError("expanded payload mismatch")
                    except (ValueError, TypeError, KeyError) as exc:
                        raise LedgerError("invalid shadow journal; preserve bytes and audit before recovery") from exc
                    events.append(event)
                # A prior append may have written a complete line before its
                # fsync failed. A retry must establish durability before it can
                # return an existing event and request a later host seal.
                stream.flush()
                os.fsync(stream.fileno())
                yield _Transaction(stream, events, self.clock, blobs)
            finally:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_UN)


def _event(events, event_hash, kind=None):
    found = next((e for e in events if e["event_hash"] == event_hash), None)
    if found is None or kind is not None and found["kind"] != kind:
        raise LedgerError("unknown event or event type")
    return found


def model_spec(artifact, *, host_origin, clock_domain, clock_epoch=None, frozen_model_path=FROZEN_MODEL_PATH):
    """Pin a research-selected artifact; no default model selection is made here."""
    if not artifact.get("candidate_id") or not artifact.get("model_version"):
        raise ValueError("a locked artifact with candidate_id and model_version is required")
    cfg = core.configuration(artifact.get("config"))
    if cfg["receipt_mode"] != "formal" or cfg["allowed_groups"] != [PRIMARY_GROUP]:
        raise ValueError("shadow requires formal receipts and the separately audited primary group")
    artifact_hash, seed_manifest_hash = digest(artifact), digest(artifact.get("history_events", []))
    evidence = _source_evidence(artifact_hash, seed_manifest_hash)
    return {"schema": "expert81-shadow-run-v2", "run_id": str(uuid.uuid4()),
            "model_version": artifact["model_version"], "candidate_id": artifact["candidate_id"],
            "config": cfg, "artifact_hash": artifact_hash, "core_sha256": implementation_hash(),
            **runtime_identity(), "seed_manifest_hash": seed_manifest_hash,
            "source_semantics": deepcopy(LEGACY_SOURCE_SEMANTICS), "source_semantics_evidence": evidence,
            "frozen_model_sha256": hashlib.sha256(Path(frozen_model_path).read_bytes()).hexdigest(),
            "frozen_predictor_sha256": hashlib.sha256((Path(__file__).parent / "expert8.py").read_bytes()).hexdigest(),
            "host_origin": host_origin.rstrip("/"), "clock_domain": clock_domain,
            "clock_epoch": clock_epoch or str(uuid.uuid4()), "pairing_policy": deepcopy(POLICY)}


class ShadowRun:
    def __init__(self, path, spec=None, *, clock=now_utc):
        self.journal = Journal(path, clock=clock)
        with self.journal.transaction() as tx:
            if not tx.events:
                if spec is None:
                    raise LedgerError("initialize a new journal with an explicit locked model spec")
                tx.append("run_started", spec)
            if tx.events[0]["kind"] != "run_started":
                raise LedgerError("missing run header")
            self.spec = deepcopy(tx.events[0]["payload"])
            if spec is not None and spec != self.spec:
                raise LedgerError("a run's model, clock and pairing policy cannot be changed")
            if self.spec.get("core_sha256") != implementation_hash():
                raise LedgerError("model implementation changed; use a new locked run")
            if any(self.spec.get(k) != v for k, v in runtime_identity().items()):
                raise LedgerError("shadow runtime or CLI implementation changed; use a new locked run")
            if self.spec.get("frozen_predictor_sha256") != hashlib.sha256((Path(__file__).parent / "expert8.py").read_bytes()).hexdigest():
                raise LedgerError("frozen EXPERT8 predictor changed")
            if self.spec.get("pairing_policy") != POLICY:
                raise LedgerError("unsupported or changed pairing policy")
            evidence = _source_evidence(self.spec.get("artifact_hash"), self.spec.get("seed_manifest_hash"))
            if (self.spec.get("source_semantics") != LEGACY_SOURCE_SEMANTICS
                    or self.spec.get("source_semantics_evidence") != evidence):
                raise LedgerError("missing or unsupported desliming source semantics; use a new audited run")

    def events(self):
        with self.journal.transaction() as tx:
            return deepcopy(tx.events)

    def capture_label(self, raw, *, source="explicit-label-file"):
        """Observe one complete version now. created_at never establishes availability.

        Deletions are explicit per-ID observations. Absence in a paginated list
        is never interpreted as deletion. Caller metadata cannot set capture_at.
        """
        label = {k: deepcopy(raw[k]) for k in ("assay_id", "sample_at", "sample_clock_domain",
                 "sample_clock_evidence", "sample_precision", "group_key", "value", "deleted",
                 "source_created_at", "source_semantics") if k in raw}
        if isinstance(raw, _FrozenSeedRecord):
            for k in ("audit_features", "audit_frozen_prediction", "audit_evidence"):
                label[k] = deepcopy(raw[k])
            label["trusted_frozen_seed"] = True
        label["assay_id"] = str(label["assay_id"])
        if not isinstance(label.get("deleted", False), bool):
            raise ValueError("deleted must be a boolean, never a string or number")
        label["deleted"] = label.get("deleted", False)
        if not label["deleted"]:
            label["sample_at"] = stamp(label["sample_at"])
            if not _number(label.get("value")) or not 0 <= label["value"] <= 100:
                raise ValueError("assay must be a finite number in 0..100")
        label["source"] = source
        label["content_hash"] = digest(label)
        with self.journal.transaction() as tx:
            previous = next((e for e in reversed(tx.events) if e["kind"] == "label_version"
                             and e["payload"]["assay_id"] == label["assay_id"]), None)
            if previous and previous["payload"]["content_hash"] == label["content_hash"]:
                return deepcopy(previous)
            label["previous_version_hash"] = previous["event_hash"] if previous else None
            label["version_number"] = previous["payload"]["version_number"] + 1 if previous else 1
            if label["assay_id"] in BLOCKED_ASSAYS:
                label["quarantine"] = "user_excluded_bad_sample_time"
            return tx.append("label_version", label)

    def capture_frozen_seed(self, artifact):
        """Only this exact locked artifact may supply historical training features."""
        rows = artifact.get("history_events", [])
        if digest(artifact) != self.spec["artifact_hash"] or digest(rows) != self.spec["seed_manifest_hash"]:
            raise LedgerError("frozen seed artifact or its row manifest differs from the locked run")
        versions = []
        for row in rows:
            if row.get("deleted"):
                continue
            raw = _FrozenSeedRecord(assay_id=row["assay_id"], sample_at=row["sample_at"],
                group_key=row["group_key"], value=row["value"], audit_features=row["features"],
                source_semantics=deepcopy(self.spec["source_semantics"]),
                audit_frozen_prediction=row["frozen_prediction"],
                audit_evidence={"artifact_hash": self.spec["artifact_hash"], "seed_manifest_hash": self.spec["seed_manifest_hash"],
                                "source_semantics_evidence": deepcopy(self.spec["source_semantics_evidence"]),
                                "original_row_hash": digest(row), "original_provenance": row.get("provenance"),
                                "availability": "new_capture_then_host_seal"})
            versions.append(self.capture_label(raw, source="locked_frozen_seed"))
        return versions

    def _validate_anchor(self, tx, anchor, target):
        required = ("requested_at", "received_at", "host_checked_at", "response_sha256",
                    "host_origin", "clock_domain", "clock_epoch", "transport")
        if (not isinstance(anchor, _HostAnchor) or any(not anchor.get(k) for k in required)
                or anchor["transport"] != "readonly-live-cache"):
            raise ClockError("host clock anchor must come from the read-only live-cache transport")
        for key in ("host_origin", "clock_domain", "clock_epoch"):
            if anchor[key] != self.spec[key]:
                raise ClockError("host identity or clock epoch changed")
        started, received = map(core.parse_time, (anchor["requested_at"], anchor["received_at"]))
        if started < core.parse_time(target["recorded_at"]) or received < started:
            raise ClockError("a seal request must start after the referenced event was durable")
        if received > core.parse_time(self.journal.clock()):
            raise ClockError("anchor receipt is in the notebook's future")
        host = core.parse_time(anchor["host_checked_at"])
        self._check_host_clock(tx, host, anchor)
        return host

    def _check_host_clock(self, tx, host, evidence):
        if any(e["kind"] == "clock_fault" for e in tx.events):
            raise ClockError("this clock epoch has a recorded fault; create a new audited run")
        later = [e["payload"]["anchor"] for e in tx.events
                 if e["kind"] in ("prediction_seal", "label_availability_seal", "association_seal")
                 and host < core.parse_time(e["payload"]["anchor"]["host_checked_at"])]
        if later:
            # An older in-flight frame/response may finish after a newer one.
            # Only causally ordered requests can establish clock reversal.
            requested = core.parse_time(evidence["requested_at"]) if evidence.get("requested_at") else None
            if requested is not None and any(requested >= core.parse_time(p["received_at"]) for p in later):
                tx.append("clock_fault", {"reason": "host_clock_moved_backwards", "evidence": evidence})
                raise ClockError("host clock moved backwards; do not continue this clock epoch")
            raise ClockError("stale or overlapping host observation; reacquire input or anchor")

    def seal_labels(self, version_hashes, anchor):
        with self.journal.transaction() as tx:
            targets = [_event(tx.events, h, "label_version") for h in dict.fromkeys(version_hashes)]
            if not targets:
                return None
            remaining = [e["event_hash"] for e in targets if not any(
                s["kind"] == "label_availability_seal" and e["event_hash"] in s["payload"]["version_hashes"]
                for s in tx.events)]
            for target in targets:
                if target["event_hash"] in remaining:
                    self._validate_anchor(tx, anchor, target)
            sealed = tx.append("label_availability_seal", {"version_hashes": remaining, "anchor": anchor}) if remaining else None
            for target in targets:
                h = target["event_hash"]
                if any(e["kind"] == "association_proposal" and e["payload"]["version_hash"] == h for e in tx.events):
                    continue
                version = _event(tx.events, h, "label_version")
                availability_seal = next(e for e in tx.events if e["kind"] == "label_availability_seal"
                                         and h in e["payload"]["version_hashes"])
                label = version["payload"]
                matched, reason = self._match(tx.events[:version["seq"] - 1], label)
                if matched is None:
                    continue
                p = matched["payload"]
                prediction_seal = next(e for e in tx.events if e["kind"] == "prediction_seal"
                                       and e["payload"]["prediction_hash"] == matched["event_hash"])
                tx.append("association_proposal", {"version_hash": h, "prediction_hash": matched["event_hash"],
                    "prediction_seal_hash": prediction_seal["event_hash"], "availability_seal_hash": availability_seal["event_hash"],
                    "original_c5_receipt_hash": (p.get("c5_receipt") or {}).get("receipt_hash"),
                    "training_features_hash": digest({k: float(p["features"][k]) for k in ("level", "coal_amount")}),
                    "features_basis": "issued_prediction_input", "features_asof": p["input"]["input_asof_at"],
                    "pairing_policy": deepcopy(POLICY)})
            return sealed

    def seal_associations(self, anchor):
        """Seal already durable association proposals; no prediction is rebuilt."""
        with self.journal.transaction() as tx:
            proposals = [e for e in tx.events if e["kind"] == "association_proposal" and not any(
                s["kind"] == "label_association" and s["payload"]["proposal_hash"] == e["event_hash"] for s in tx.events)]
            results = []
            for proposal in proposals:
                pp = proposal["payload"]
                prediction = _event(tx.events, pp["prediction_hash"], "prediction")
                label = _event(tx.events, pp["version_hash"], "label_version")
                prediction_seal = _event(tx.events, pp["prediction_seal_hash"], "prediction_seal")
                seal = next((e for e in tx.events if e["kind"] == "association_seal"
                             and e["payload"]["proposal_hash"] == proposal["event_hash"]), None)
                if seal is None:
                    self._validate_anchor(tx, anchor, proposal)
                    seal = tx.append("association_seal", {"proposal_hash": proposal["event_hash"], "anchor": anchor})
                p, receipt = prediction["payload"], prediction["payload"].get("c5_receipt")
                association = None
                if receipt:
                    association = core.make_c5_association(receipt,
                        association_validator=self._association_validator(tx.events, pending_proposal=proposal),
                        sample_at=label["payload"]["sample_at"],
                        input_asof=p["input"]["input_asof_at"],
                        postseal_at=prediction_seal["payload"]["anchor"]["host_checked_at"],
                        max_age_seconds=POLICY["maximum_age_seconds"], matching_policy="latest-eligible",
                        prediction_event_hash=prediction["event_hash"], prediction_seal_hash=prediction_seal["event_hash"],
                        label_event_hash=label["event_hash"], label_version_id=label["event_hash"],
                        group_key=p["group_key"], clock_domain=self.spec["clock_domain"], clock_epoch=self.spec["clock_epoch"],
                        bound_at=seal["payload"]["anchor"]["host_checked_at"], association_seal_hash=seal["event_hash"],
                        training_features_hash=pp["training_features_hash"], issued_features_hash=receipt["features_hash"])
                results.append(tx.append("label_association", {"version_hash": label["event_hash"],
                    "prediction_hash": prediction["event_hash"], "proposal_hash": proposal["event_hash"],
                    "association_seal_hash": seal["event_hash"], "c5_association": association}))
            return results

    def _association_validator(self, events, pending_proposal=None):
        def validate(envelope):
            try:
                association = next((e for e in events if e["kind"] == "label_association"
                                    and e["payload"].get("c5_association") == envelope), None)
                if association is None and pending_proposal is not None:
                    pp = pending_proposal["payload"]
                    association = {"seq": len(events) + 1, "payload": {
                        "proposal_hash": pending_proposal["event_hash"], "version_hash": pp["version_hash"],
                        "prediction_hash": pp["prediction_hash"], "association_seal_hash": envelope["association_seal_hash"]}}
                if association is None:
                    return False
                ap = association["payload"]
                proposal = _event(events, ap["proposal_hash"], "association_proposal")
                seal = _event(events, ap["association_seal_hash"], "association_seal")
                prediction = _event(events, ap["prediction_hash"], "prediction")
                label = _event(events, ap["version_hash"], "label_version")
                postseal = _event(events, proposal["payload"]["prediction_seal_hash"], "prediction_seal")
                selected, _ = self._match(events[:label["seq"] - 1], label["payload"])
                return bool(selected and selected["event_hash"] == prediction["event_hash"]
                    and prediction["seq"] < postseal["seq"] < label["seq"] < proposal["seq"] < seal["seq"] < association["seq"]
                    and seal["payload"]["proposal_hash"] == proposal["event_hash"]
                    and envelope["association_seal_hash"] == seal["event_hash"]
                    and envelope["bound_at"] == seal["payload"]["anchor"]["host_checked_at"]
                    and envelope["original_receipt"] == prediction["payload"]["c5_receipt"]
                    and envelope["label_event_hash"] == label["event_hash"] == envelope["label_version_id"]
                    and envelope["prediction_event_hash"] == prediction["event_hash"]
                    and envelope["prediction_seal_hash"] == postseal["event_hash"]
                    and envelope["training_features_hash"] == proposal["payload"]["training_features_hash"]
                    and envelope["clock_domain"] == self.spec["clock_domain"] and envelope["clock_epoch"] == self.spec["clock_epoch"])
            except (ValueError, KeyError, TypeError):
                return False
        return validate

    def _match(self, events, label):
        if label.get("quarantine"):
            return None, label["quarantine"]
        if label.get("deleted"):
            return None, "deleted_label"
        if label.get("sample_precision") != "second":
            return None, "sample_precision_unverified"
        if not label.get("group_key") or label["group_key"] == "unknown":
            return None, "sample_group_unverified"
        if (label.get("sample_clock_domain") != self.spec["clock_domain"]
                or not label.get("sample_clock_evidence")):
            return None, "sample_clock_unverified"
        if label.get("source_semantics") != self.spec["source_semantics"]:
            return None, "sample_source_semantics_unverified"
        sample = core.parse_time(label["sample_at"])
        candidates = []
        for seal in events:
            if seal["kind"] != "prediction_seal" or not seal["payload"].get("usable"):
                continue
            prediction = _event(events, seal["payload"]["prediction_hash"], "prediction")
            p = prediction["payload"]
            if p["evidence_mode"] != "prospective_host_sealed" or p["result"]["prediction"] is None:
                continue
            if (p.get("source_semantics") != self.spec["source_semantics"]
                    or _source_semantics_error(p["input"], self.spec["source_semantics"])):
                continue
            if label["group_key"] != p["group_key"]:
                continue
            input_at = core.parse_time(p["input"]["input_asof_at"])
            sealed_at = core.parse_time(seal["payload"]["anchor"]["host_checked_at"])
            if (sealed_at < sample and 0 <= (sample - input_at).total_seconds() <= POLICY["maximum_age_seconds"]):
                # Exclude target IDs even after a revision; own-label-trained receipts cannot be scored.
                if label["assay_id"] in {str(i) for i in p["state"]["training_ids"]}:
                    continue
                candidates.append((input_at, prediction["seq"], prediction))
        return (max(candidates, key=lambda x: (x[0], x[1]))[2], None) if candidates else (None, "no_eligible_prior_prediction")

    def _history(self, events, cutoff):
        versions, seals = {}, {}
        for event in events:
            if event["kind"] == "label_version":
                versions[event["event_hash"]] = event
            elif event["kind"] == "label_availability_seal":
                for h in event["payload"]["version_hashes"]:
                    seals.setdefault(h, event)
        # Resolve versions globally before group filtering, including tombstones and bad revisions.
        latest = {}
        for h, event in versions.items():
            seal = seals.get(h)
            if seal is None or core.parse_time(seal["payload"]["anchor"]["host_checked_at"]) >= cutoff:
                continue
            assay_id = event["payload"]["assay_id"]
            if assay_id not in latest or latest[assay_id][0]["seq"] < event["seq"]:
                latest[assay_id] = event, seal
        history = []
        for event, seal in latest.values():
            label = event["payload"]
            at = seal["payload"]["anchor"]["host_checked_at"]
            base = dict(assay_id=label["assay_id"], version_id=event["event_hash"], version_available_at=at,
                        provenance={"kind": "capture_then_host_seal", "capture_at": event["recorded_at"],
                                    "capture_clock_domain": "notebook", "capture_event_hash": event["event_hash"],
                                    "label_event_hash": event["event_hash"],
                                    "seal_event_hash": seal["event_hash"], "clock_domain": self.spec["clock_domain"]})
            if (label.get("deleted") or label.get("quarantine")
                    or label.get("source_semantics") != self.spec["source_semantics"]):
                history.append(dict(base, deleted=True))
                continue
            base["provenance"]["source_semantics"] = deepcopy(self.spec["source_semantics"])
            if core.parse_time(label["sample_at"]) > core.parse_time(at):
                history.append(dict(base, deleted=True))
                continue
            matched, _ = self._match(events[:event["seq"] - 1], label)
            if matched:
                p = matched["payload"]
                features, frozen, group = p["features"], p["frozen_prediction"], p["group_key"]
                base["provenance"].update(features_basis="issued_prediction_input", features_asof=p["input"]["input_asof_at"])
            elif (label.get("trusted_frozen_seed") and label.get("audit_evidence", {}).get("artifact_hash") == self.spec["artifact_hash"]
                    and label["audit_evidence"].get("seed_manifest_hash") == self.spec["seed_manifest_hash"]
                    and label["audit_evidence"].get("source_semantics_evidence") == self.spec["source_semantics_evidence"]
                    and label.get("audit_features") and label.get("group_key")):
                features = label["audit_features"]
                frozen, group = label.get("audit_frozen_prediction"), label["group_key"]
            else:
                # An unusable latest revision must still suppress its older usable version.
                history.append(dict(base, deleted=True))
                continue
            if not all(_number(features.get(k)) for k in ("level", "coal_amount")) or not _number(frozen):
                history.append(dict(base, deleted=True))
                continue
            row = dict(base, group_key=group, sample_at=label["sample_at"], value=label["value"],
                       features={k: features[k] for k in ("level", "coal_amount")}, frozen_prediction=frozen)
            if matched:
                row["provenance"]["prediction_event_hash"] = matched["event_hash"]
                association = next((e for e in events if e["kind"] == "label_association"
                                    and e["payload"]["version_hash"] == event["event_hash"]), None)
                if association and association["payload"].get("c5_association"):
                    envelope = association["payload"]["c5_association"]
                    original = matched["payload"].get("c5_receipt")
                    if (envelope["original_receipt"] != original
                            or association["payload"]["prediction_hash"] != matched["event_hash"]):
                        raise LedgerError("association does not match the original durable C5 receipt")
                    if core.parse_time(envelope["bound_at"]) < cutoff:
                        row["c5_receipt"] = envelope
            history.append(row)
        return history

    def predict(self, frame, *, key=None):
        """Compute now. No caller-supplied prediction_at or capture_at is accepted."""
        forbidden = {"prediction_at", "capture_at", "recorded_at", "version_available_at"} & set(frame)
        if forbidden:
            raise ValueError("prediction/availability timestamps are assigned by the runtime")
        live_transport = isinstance(frame, _LiveFrame)
        frame = deepcopy(frame)
        if not live_transport:
            frame["transport"] = "file-exploratory"
        if frame.get("clock_domain") != self.spec["clock_domain"] or frame.get("clock_epoch") != self.spec["clock_epoch"]:
            raise ClockError("input clock domain or epoch does not match the run")
        source_error = _source_semantics_error(frame, self.spec["source_semantics"])
        if source_error:
            # Do not fit the old seed or manufacture a zero-bias C5 fallback.
            raise LedgerError(source_error)
        request_hash = digest(frame)
        key = str(key or request_hash)
        with self.journal.transaction() as tx:
            previous = next((e for e in tx.events if e["kind"] == "prediction" and e["payload"]["key"] == key), None)
            if previous:
                if previous["payload"]["request_hash"] != request_hash:
                    raise LedgerError("prediction key already refers to different input")
                return deepcopy(previous)
            cutoff = core.parse_time(frame["input_asof_at"])
            if any(e["kind"] == "clock_fault" for e in tx.events):
                raise ClockError("this clock epoch has a recorded fault; create a new audited run")
            if live_transport:
                self._check_host_clock(tx, cutoff, frame["input_anchor"])
                if frame.get("frozen_model_sha256") != self.spec["frozen_model_sha256"]:
                    raise LedgerError("input baseline differs from the pinned frozen EXPERT8 artifact")
            if frame.get("transport") == "readonly-cache-frame" and frame.get("host_origin") != self.spec["host_origin"]:
                raise ClockError("input source origin changed")
            history = self._history(tx.events, cutoff)
            features = {k: frame["inputs"][k].get("value") for k in FEATURES}
            group = ",".join(str(int(features[k])) for k in STATE_KEYS) if all(
                _number(features[k]) and features[k] in (0, 1) for k in STATE_KEYS) else "unknown"
            validator = self._association_validator(tx.events)
            state = core.fit_state(history, cutoff, self.spec["candidate_id"], self.spec["config"], group_key=group,
                                   association_validator=validator)
            result = core.predict_state(state, features, frame.get("frozen_prediction"))
            c5_state = core.fit_state(history, cutoff, "C5", self.spec["config"], group_key=group,
                                      association_validator=validator)
            c5_receipt = core.make_c5_receipt(c5_state, features, sample_at=cutoff,
                                            clock_domain=self.spec["clock_domain"], clock_epoch=self.spec["clock_epoch"])
            support = {}
            for name in ("level", "coal_amount"):
                bounds = state.get("ranges", {}).get(name)
                support[name] = dict(value=features[name], normal_training_range=bounds,
                    within_normal_training_range=bool(bounds and _number(features[name])
                        and bounds["minimum"] <= features[name] <= bounds["maximum"]))
            invalid = frame.get("unusable_reasons", [])
            if invalid:
                result.update(prediction=None, value=None, status="unavailable", reason=";".join(invalid))
                c5_receipt = None
            return tx.append("prediction", {"key": key, "request_hash": request_hash,
                "model_version": self.spec["model_version"], "artifact_hash": self.spec["artifact_hash"],
                "source_semantics": deepcopy(self.spec["source_semantics"]),
                "evidence_mode": "prospective_host_sealed" if live_transport else "file_exploratory",
                "input": frame, "input_clock_domain": self.spec["clock_domain"],
                "fit_cutoff_at": stamp(cutoff), "features": features, "group_key": group,
                "feature_support": support,
                "frozen_prediction": frame.get("frozen_prediction"), "state": state, "result": result,
                "c5_state": c5_state, "c5_receipt": c5_receipt,
                "calibration_version_hashes": [r["version_id"] for r in history],
                "last_local_event_seq": tx.events[-1]["seq"]})

    def seal_prediction(self, prediction_hash, anchor):
        with self.journal.transaction() as tx:
            prediction = _event(tx.events, prediction_hash, "prediction")
            existing = next((e for e in tx.events if e["kind"] == "prediction_seal"
                             and e["payload"]["prediction_hash"] == prediction_hash), None)
            if existing:
                return deepcopy(existing)
            host_at = self._validate_anchor(tx, anchor, prediction)
            input_at = core.parse_time(prediction["payload"]["input"]["input_asof_at"])
            age = (host_at - input_at).total_seconds()
            if age < 0:
                raise ClockError("host seal precedes its input snapshot")
            usable = age <= POLICY["maximum_snapshot_age_seconds"]
            return tx.append("prediction_seal", {"prediction_hash": prediction_hash, "anchor": anchor,
                "input_snapshot_age_seconds": age, "usable": usable,
                "reason": None if usable else "input_snapshot_too_old_at_seal"})

    def score(self, version_hash):
        """Append a score against an existing receipt; never fit or predict here."""
        with self.journal.transaction() as tx:
            label_event = _event(tx.events, version_hash, "label_version")
            existing = next((e for e in tx.events if e["kind"] == "score" and e["payload"]["version_hash"] == version_hash), None)
            if existing:
                return deepcopy(existing)
            label = label_event["payload"]
            # A late seal cannot retrospectively turn a label-known receipt into a prospective one.
            prediction, reason = self._match(tx.events[:label_event["seq"] - 1], label)
            availability = next((e for e in tx.events if e["kind"] == "label_availability_seal"
                                 and version_hash in e["payload"]["version_hashes"]), None)
            if availability is None:
                prediction, reason = None, "label_availability_unsealed"
            elif not label.get("deleted") and core.parse_time(label["sample_at"]) > core.parse_time(availability["payload"]["anchor"]["host_checked_at"]):
                prediction, reason = None, "sample_time_after_observed_label_version"
            payload = {"version_hash": version_hash, "assay_id": label["assay_id"],
                "status": "scored" if prediction else "unmatched", "reason": reason,
                "prediction_hash": prediction["event_hash"] if prediction else None,
                "value": label.get("value"), "sample_at": label.get("sample_at"),
                "sample_precision": label.get("sample_precision"), "sample_clock_evidence": label.get("sample_clock_evidence")}
            if prediction:
                p = prediction["payload"]
                error = p["result"]["prediction"] - label["value"]
                frozen_error = p["frozen_prediction"] - label["value"] if _number(p["frozen_prediction"]) else None
                fence = p["state"]["fence"]
                normal = core.inside(label["value"], fence)
                payload.update(prediction=p["result"]["prediction"], error=error, absolute_error=abs(error),
                    within_1pp=abs(error) <= 1 + 1e-9, frozen_prediction=p["frozen_prediction"],
                    frozen_absolute_error=abs(frozen_error) if frozen_error is not None else None,
                    normal_by_prediction_training_fence=normal, training_fence=fence,
                    model_version=p["model_version"], state_hash=p["state"]["state_hash"], group_key=p["group_key"])
                payload["feature_support"] = p["feature_support"]
            return tx.append("score", payload)

    def summary(self):
        events = self.events()
        latest = {}
        latest_versions = {}
        for e in events:
            if e["kind"] == "label_version":
                latest[e["payload"]["assay_id"]] = e["event_hash"]
                latest_versions[e["payload"]["assay_id"]] = e["payload"]
        scores = [e["payload"] for e in events if e["kind"] == "score"
                  and latest.get(e["payload"]["assay_id"]) == e["payload"]["version_hash"]]
        def metrics(rows):
            scored = [s for s in rows if s["status"] == "scored"]
            return {"n": len(scored), "mae": sum(s["absolute_error"] for s in scored) / len(scored) if scored else None,
                    "within_1pp": sum(s["within_1pp"] for s in scored) / len(scored) if scored else None}
        return {"run_id": self.spec["run_id"], "events": len(events), "last_event_hash": events[-1]["event_hash"],
                "prediction_count": sum(e["kind"] == "prediction" for e in events),
                "unmatched": sum(s["status"] != "scored" for s in scores), "all": metrics(scores),
                "normal": metrics([s for s in scores if s.get("normal_by_prediction_training_fence")]),
                "frozen_seed_count": sum(bool(v.get("trusted_frozen_seed")) for v in latest_versions.values()),
                "outstanding_labels": sum(not v.get("trusted_frozen_seed") for k, v in latest_versions.items()
                    if not any(s["assay_id"] == k for s in scores)), "production_model_activated": False}


class ReadOnlyCacheSource:
    """Eight input cells from the live ring and three bounded metadata reads.

    Does not call decision/current, hourly calculations, manual_measurements,
    historical telemetry, state mirrors, training or a production write API.
    """
    def __init__(self, config, frozen_model_path, *, clock_domain, clock_epoch, clock=now_utc):
        self.config = deepcopy(config)
        self.origin = config["server_url"].rstrip("/")
        self.clock_domain, self.clock_epoch, self.clock = clock_domain, clock_epoch, clock
        raw = Path(frozen_model_path).read_bytes()
        self.frozen_model_sha256 = hashlib.sha256(raw).hexdigest()
        self.frozen_model = json.loads(raw)["model"]
        from .expert8 import valid
        if not valid(self.frozen_model):
            raise ValueError("frozen EXPERT8 artifact is invalid")

    def _live(self):
        from contextlib import closing
        from remote_sqlite import RemoteConnection
        started = stamp(self.clock())
        with closing(RemoteConnection(self.config, timeout=4)) as connection:
            data = connection._request("GET", "/live-cache")
        received = stamp(self.clock())
        if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
            raise ValueError("invalid live-cache response")
        core.parse_time(data["checked_at"])
        if len(data["sources"]) > 128 or len(json.dumps(data).encode()) > 4 * 1024 * 1024:
            raise ValueError("live-cache response exceeds bounded transport size")
        anchor = _HostAnchor({"requested_at": started, "received_at": received,
                  "host_checked_at": stamp(data["checked_at"]), "response_sha256": digest(data),
                  "host_origin": self.origin, "clock_domain": self.clock_domain,
                  "clock_epoch": self.clock_epoch, "transport": "readonly-live-cache"})
        return data, anchor

    def anchor(self):
        return self._live()[1]

    def _metadata(self, host_at):
        from contextlib import closing
        from remote_sqlite import RemoteConnection
        from ..telemetry.derived import FOUR_SYSTEM_MEMBERS, DESLIMING_MEMBERS
        from ..hourly_brief.inputs import COAL_AMOUNT, LEVEL

        items = [COAL_AMOUNT, LEVEL, *(x[1] for x in FOUR_SYSTEM_MEMBERS), *(x[1] for x in DESLIMING_MEMBERS)]
        with closing(RemoteConnection(self.config, timeout=4)) as connection:
            row = connection.execute(
                "SELECT id,effective_at,config FROM decision_source_revisions "
                "WHERE effective_at <= ? ORDER BY id DESC LIMIT 1", (stamp(host_at),)).fetchone()
            revision = {"id": row[0], "effective_at": row[1], "config": json.loads(row[2])} if row else {"id": 0, "config": {"choices": {}}}
            choices = revision["config"].get("choices", {})
            overrides = []
            for node in ("coal-rate", "tail-level", "system-switches", "desliming"):
                choice = choices.get(node)
                if choice:
                    source_id, key = json.loads(choice)
                    if source_id == "manual-measurements":
                        raise ValueError("shadow model inputs must be telemetry points")
                    overrides.append((source_id, key))
            clause = "json_extract(config,'$.item_id') IN (" + ",".join("?" for _ in items) + ")"
            parameters = list(items)
            for source_id, key in overrides:
                clause += " OR (source_id = ? AND key = ?)"
                parameters.extend((source_id, key))
            rows = connection.execute("SELECT source_id,key,config FROM telemetry_points WHERE enabled = 1 AND (" + clause + ") LIMIT 65", parameters).fetchall()
            if len(rows) > 64:
                raise ValueError("ambiguous or oversized telemetry point selection")
            points = [{"source_id": r[0], "key": r[1], "config": json.loads(r[2])} for r in rows]
            source_ids = sorted({p["source_id"] for p in points})
            if len(source_ids) > 8:
                raise ValueError("shadow metadata exceeds eight sources")
            sources = []
            if source_ids:
                sources = connection.execute(
                    "SELECT source_id,owner,lease_until,state FROM telemetry_sources WHERE source_id IN ("
                    + ",".join("?" for _ in source_ids) + ") LIMIT 9", source_ids).fetchall()
        return {"revision": revision, "points": points, "sources": [
            {"source_id": r[0], "owner": r[1], "lease_until": r[2], "state": r[3]} for r in sources]}

    def frame(self):
        data, anchor = self._live()
        metadata = self._metadata(data["checked_at"])
        return self.frame_from_cache(data, metadata, anchor)

    def frame_from_cache(self, data, metadata, anchor):
        """Pure extraction helper, also exercised with synthetic raw cache data."""
        from types import SimpleNamespace
        from ..telemetry.realtime import cached_samples, _fresh
        from ..telemetry.retained import usable_at_receipt
        from ..telemetry.domain import availability, definition_id
        from ..telemetry.derived import FOUR_SYSTEM_MEMBERS, DESLIMING_MEMBERS
        from ..hourly_brief.inputs import COAL_AMOUNT, LEVEL
        from .expert8 import explain

        asof = core.parse_time(data["checked_at"])
        points, sources = metadata["points"], {s["source_id"]: s for s in metadata["sources"]}
        choices = metadata["revision"]["config"].get("choices", {})
        runtime = {}
        for source_id, source in sources.items():
            document = data["sources"].get(source_id)
            try:
                if (document and document.get("source_id") == source_id and document.get("owner") == source["owner"]
                        and source["lease_until"] and core.parse_time(source["lease_until"]) > asof
                        and document.get("state", source["state"]) == "connected" and _fresh(document, asof)):
                    runtime[source_id] = document
            except (TypeError, KeyError, ValueError):
                continue

        def selected(node, item, group=None):
            choice = choices.get(node)
            if node in choices and not choice:
                return None
            candidates = [p for p in points if p["config"].get("item_id") == item]
            if choice:
                sid, key = json.loads(choice)
                candidates = [p for p in candidates if p["source_id"] == sid] if group and key == group else [
                    p for p in points if p["source_id"] == sid and p["key"] == key] if not group else []
            return candidates[0] if len(candidates) == 1 else None

        def cell(point, *, lag=0):
            if point is None:
                return {"value": None, "status": "missing", "reason": "input_point_missing_or_ambiguous"}
            p = SimpleNamespace(**point)
            target = asof - timedelta(seconds=lag)
            samples = cached_samples(runtime, p, target)
            if lag:
                samples = [s for s in samples if core.parse_time(s["received_at"]) >= target - timedelta(seconds=POLICY["level_lookback_seconds"])]
            sample = max(enumerate(samples), key=lambda x: (core.parse_time(x[1]["received_at"]), x[0]), default=(None, None))[1]
            meta = {"source_id": p.source_id, "point_key": p.key, "item_id": p.config.get("item_id"),
                    "definition_id": definition_id(p.config), "unit": p.config.get("unit", ""),
                    "definition": {k: p.config.get(k) for k in DEFINITION_FIELDS},
                    "sample": sample, "observed_at": sample.get("received_at") if sample else None,
                    "clock_domain": self.clock_domain, "target_at": stamp(target), "lag_seconds": lag}
            # Select the newest observation first. A bad newest point never falls back to an older good one.
            good = bool(sample and usable_at_receipt(sample, p.config) and availability(sample, p.config, True, target)[0])
            if good and not lag and not 0 <= (asof - core.parse_time(sample["received_at"])).total_seconds() <= POLICY["maximum_nonlag_input_age_seconds"]:
                good = False
            value = sample.get("value") if good else None
            if isinstance(value, bool):
                value = int(value)
            return dict(meta, value=value, status="good" if good else "missing",
                        reason=None if good else "latest_cache_sample_unusable_or_missing")

        cells = {"coal_amount": cell(selected("coal-rate", COAL_AMOUNT)),
                 "level": cell(selected("tail-level", LEVEL), lag=POLICY["level_lag_seconds"])}
        for label, item in FOUR_SYSTEM_MEMBERS:
            cells[{"A": "sysA", "B": "sysB", "401": "sys401", "402": "sys402"}[label]] = cell(selected("system-switches", item, "four_system_status"))
        for label, item in DESLIMING_MEMBERS:
            cells["desliming" + label] = cell(selected("desliming", item, "desliming_status"))
        reasons = []
        source_semantics = _observed_source_semantics(cells)
        source_error = _source_semantics_error({"inputs": cells, "source_semantics": source_semantics}, LEGACY_SOURCE_SEMANTICS)
        if source_error:
            reasons.append(source_error)
        for key, c in cells.items():
            if not _number(c["value"]) or c["status"] != "good":
                reasons.append(key + ":" + (c.get("reason") or "invalid_value"))
            elif (key in STATE_KEYS and c["value"] not in (0, 1) or key == "coal_amount" and c["value"] <= 10
                    or key == "level" and not 0 <= c["value"] <= 100):
                reasons.append(key + ":outside_valid_range")
        for group in (("sysA", "sysB", "sys401", "sys402"), ("desliming473", "desliming474")):
            identities = {(cells[k].get("source_id"), (cells[k].get("sample") or {}).get("batch_id"), cells[k].get("observed_at")) for k in group}
            if len(identities) != 1 or not next(iter(identities))[1]:
                reasons.append("state_group_not_same_raw_batch")
        features = {k: cells[k]["value"] for k in FEATURES}
        group = ",".join(str(int(features[k])) for k in STATE_KEYS) if all(_number(features[k]) and features[k] in (0, 1) for k in STATE_KEYS) else "unknown"
        if group != PRIMARY_GROUP:
            reasons.append("unsupported_group")
        explanation = explain(features, self.frozen_model) if not reasons else None
        return _LiveFrame({"schema": "expert81-shadow-input-v1", "input_asof_at": stamp(asof), "inputs": cells,
                "source_semantics": source_semantics,
                "clock_domain": self.clock_domain, "clock_epoch": self.clock_epoch, "host_origin": self.origin,
                "transport": "readonly-cache-frame", "input_anchor": anchor,
                "metadata_sha256": digest(metadata), "source_revision": metadata["revision"]["id"],
                "frozen_model_sha256": self.frozen_model_sha256,
                "frozen_prediction": explanation["value"] if explanation else None,
                "unusable_reasons": reasons, "level_policy": {k: POLICY[k] for k in ("level_lag_seconds", "level_lookback_seconds")}})
