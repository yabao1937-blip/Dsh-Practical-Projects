"""Bounded, local EXPERT8.1 shadow CLI. No model activation or production writes."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path
import sys
import time

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.gpt_coarse.expert81_shadow import (  # noqa: E402
    PRIMARY_GROUP, ReadOnlyCacheSource, ShadowRun, digest, model_spec,
)

DEFAULT_FROZEN = BACKEND / "models/gpt_expert8/20261012/model.json"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def remote_configuration(path):
    from remote_mode import settings
    configuration = settings(path)
    if configuration is None:
        raise ValueError("the existing read-only host configuration is required; no local database fallback")
    return configuration


def source_for(args, spec):
    configuration = remote_configuration(args.remote_config)
    if configuration["server_url"] != spec["host_origin"]:
        raise ValueError("read-only host origin differs from the pinned run")
    source = ReadOnlyCacheSource(configuration, args.frozen_model,
        clock_domain=spec["clock_domain"], clock_epoch=spec["clock_epoch"])
    if source.frozen_model_sha256 != spec["frozen_model_sha256"]:
        raise ValueError("frozen EXPERT8 artifact differs from the pinned run")
    return source


def emit(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))


def initialize(args):
    artifact = read_json(args.artifact)
    configuration = remote_configuration(args.remote_config)
    spec = model_spec(artifact, host_origin=configuration["server_url"], clock_domain=args.clock_domain,
                      frozen_model_path=args.frozen_model)
    run = ShadowRun(args.ledger, spec)
    emit(run.summary())


def inspect_input(args):
    configuration = remote_configuration(args.remote_config)
    source = ReadOnlyCacheSource(configuration, args.frozen_model, clock_domain=args.clock_domain,
                                 clock_epoch="input-extraction-only")
    frame = source.frame()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Retain input evidence only; no raw cache ring and no manual laboratory data.
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(frame, stream, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)
        stream.write("\n")
    state_keys = ("sysA", "sysB", "sys401", "sys402", "desliming473", "desliming474")
    values = [frame["inputs"][k]["value"] for k in state_keys]
    emit({"output": str(output.resolve()), "input_asof_at": frame["input_asof_at"],
          "group_values": values, "primary_group": PRIMARY_GROUP, "unusable_reasons": frame["unusable_reasons"],
          "frozen_model_sha256": frame["frozen_model_sha256"], "prediction_created": False})


def tick(args):
    run = ShadowRun(args.ledger)
    source = source_for(args, run.spec)
    for index in range(args.count):
        frame = source.frame()
        prediction = run.predict(frame)
        # This request must begin after predict() has returned from journal fsync.
        seal = run.seal_prediction(prediction["event_hash"], source.anchor())
        emit({"prediction_hash": prediction["event_hash"], "prediction_at_notebook": prediction["recorded_at"],
              "input_asof_at_host": frame["input_asof_at"], "group_key": prediction["payload"]["group_key"],
              "result": prediction["payload"]["result"], "seal_usable": seal["payload"]["usable"],
              "seal_reason": seal["payload"]["reason"], "production_model_activated": False})
        if index + 1 < args.count:
            time.sleep(args.interval)


def ingest(args):
    run = ShadowRun(args.ledger)
    document = read_json(args.labels)
    if args.frozen_seed:
        versions = run.capture_frozen_seed(document)
    else:
        rows = document if isinstance(document, list) else document["items"]
        versions = [run.capture_label(row, source=str(Path(args.labels).resolve())) for row in rows]
    if versions:
        source = source_for(args, run.spec)
        run.seal_labels([v["event_hash"] for v in versions], source.anchor())
        # Bindings were durable before this second host request. They are not a new fit.
        events = run.events()
        pending = [e for e in events if e["kind"] == "association_proposal" and not any(
            a["kind"] == "label_association" and a["payload"]["proposal_hash"] == e["event_hash"] for a in events)]
        if pending:
            run.seal_associations(source.anchor())
        if not args.frozen_seed:
            for version in versions:
                run.score(version["event_hash"])
    emit(run.summary())


def watch_files(args):
    run = ShadowRun(args.ledger)
    for index in range(args.count):
        frame = read_json(args.input)
        # predict() marks all deserialized file frames exploratory, regardless of their transport string.
        prediction = run.predict(frame)
        emit({"prediction_hash": prediction["event_hash"], "evidence_mode": prediction["payload"]["evidence_mode"],
              "result": prediction["payload"]["result"]})
        if index + 1 < args.count:
            time.sleep(args.interval)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    def with_remote(command):
        command.add_argument("--remote-config", help="existing readonly-test JSON config; token is never printed")
        command.add_argument("--frozen-model", type=Path, default=DEFAULT_FROZEN)
    init = commands.add_parser("init", help="pin one research-selected model; no network request")
    init.add_argument("--ledger", required=True, type=Path)
    init.add_argument("--artifact", required=True, type=Path)
    init.add_argument("--clock-domain", required=True, help="identity of the actual production host clock")
    with_remote(init)
    init.set_defaults(handler=initialize)

    inspect = commands.add_parser("inspect-input", help="one bounded cache-only input extraction, no prediction or labs")
    inspect.add_argument("--clock-domain", required=True)
    inspect.add_argument("--output", required=True, type=Path)
    with_remote(inspect)
    inspect.set_defaults(handler=inspect_input)

    live = commands.add_parser("tick", help="bounded live shadow; never calls current/hourly or reads new labels")
    live.add_argument("--ledger", required=True, type=Path)
    live.add_argument("--count", type=int, choices=range(1, 4), default=1)
    live.add_argument("--interval", type=float, default=30)
    with_remote(live)
    live.set_defaults(handler=tick)

    labels = commands.add_parser("ingest-labels", help="capture explicitly supplied versions, then seal, associate and score")
    labels.add_argument("--ledger", required=True, type=Path)
    labels.add_argument("--labels", required=True, type=Path)
    labels.add_argument("--frozen-seed", action="store_true", help="read history_events from an audited research artifact")
    with_remote(labels)
    labels.set_defaults(handler=ingest)

    files = commands.add_parser("watch-files", help="bounded exploratory file input; cannot produce a formal score")
    files.add_argument("--ledger", required=True, type=Path)
    files.add_argument("--input", required=True, type=Path)
    files.add_argument("--count", type=int, choices=range(1, 121), default=1)
    files.add_argument("--interval", type=float, default=30)
    files.set_defaults(handler=watch_files)

    summary = commands.add_parser("summary", help="verify the chain and report immutable latest-version scores")
    summary.add_argument("--ledger", required=True, type=Path)
    summary.set_defaults(handler=lambda args: emit(ShadowRun(args.ledger).summary()))
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    if not math.isfinite(getattr(arguments, "interval", 30)) or getattr(arguments, "interval", 30) < 30:
        parser().error("minimum interval is 30 seconds; automatic high-frequency polling is not supported")
    try:
        arguments.handler(arguments)
        return 0
    except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
        # Transport errors must not dump configurations, credentials or laboratory payloads.
        emit({"status": "failed", "error_type": type(exc).__name__,
              "message": str(exc) if isinstance(exc, ValueError) else "I/O or transport failed; no production write was made"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
