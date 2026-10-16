# Production-time heavy ash and frozen hourly MD_SET

User-confirmed policy, 2026-10-16:

- New heavy-clean-coal assays use their entered production timestamp directly. The latest sample time in `[hour_start,hour_end)` wins; equal timestamps use the larger record ID. Missing hours carry the latest earlier assay, never a later production hour.
- Existing rows retain `legacy-lag-2h`; original timestamps, IDs, hashes and entry times remain intact. New API records explicitly use `production-time-v1`. Startup and Alembic upgrade add the discriminator and query index without rewriting evidence.
- Publication remains one hour after the production hour ends. At 10:20 the newest published interval is 08:00–09:00, at 11:00 it is 09:00–10:00, and at 12:00 it is 10:00–11:00.
- An 08:47 assay entered at 10:20 settles 08:00–09:00. A 09:20 assay entered at 12:00 revises 09:00–10:00 and later hours carrying it, until a newer production sample exists. Cached settlements are revision checked on reads; original online decisions and saved hourly evidence are immutable.
- Hourly reference targets combine latest published total ash, current target range and the first valid MD_SET sampled in the application hour. The worker persists this basis in `gpt_hourly_density_anchors` inside its serialized transaction. Reads do not write or recapture it. Restart, late assays, target edits and subsequent MD_SET moves do not move that hour's basis.
- Only ash production coverage must strictly exceed 80%; previous-hour actual-density coverage is no longer an hourly-target input. Real source, model, point-definition and adjustment-parameter changes remain protected. Current input/state/setpoint validity and each-channel ±0.03 limits remain execution checks.
- Settlement policy: `gpt-ash-settlement-v4`. Hourly advice: `gpt-hourly-reference-v4`. Outer API versions remain unchanged. The browser recognizes the new basis and isolates last-valid caches across policy changes.

Regression coverage includes the exact late-entry timeline, old/new ordering across manual sources, API retry compatibility, additive/idempotent database upgrades, unchanged decision evidence, target/lab recomputation on a frozen basis, missing/expired/future setpoints, a new-hour basis and two concurrent decision workers. Existing interval, history, execution and DS golden tests are retained. No DS training/prediction/aggregation logic or `backend/data/train_js.json` is changed.

Operational scope: current local checkout. The separate offline Windows deployment needs this update transferred and its service restarted before it uses the new policy.

## Verification completed

- Core timing/manual/advice regressions: 114 passed.
- Ash history, minute intervals, authoritative references and production coverage: 57 passed.
- Startup/Alembic migration tests: 6 passed, including startup-before-Alembic adoption and unchanged old columns/constraints.
- Existing brief/decision/execution/source/context and DS tests: 115 passed on the combined run; its remaining old-policy-version assertion was updated and passed on the focused rerun. DS original golden suite passed all 9 cases. Golden SHA256 remained `aa09b7a4574ec19be5f840b68f3611394b0584f2ec435b0abd062416f8566bbd`.
- Node checks passed: `verify_recent_ash.js`, `verify_ash_history.js`, `verify_decision_hourly.js`, `verify_density_display_memory.js`, `verify_manual_measurement_points.js`.
- Online SQLite backup created before upgrade. The local database's 7 original manual records match the backup on every original column; all retain legacy attribution. The new anchor table and timing column are present.
- Restarted local service and verified HTTP 200 for health, homepage, total-ash-hours and hourly advice. APIs report settlement/advice v4 and the 3600-second publication delay.
- Real browser checked the production-time form explanation and frozen hourly basis explanation, then returned to instant view. No test assays were inserted into the production database. The laptop has no current valid KEP data, so a live hourly anchor has not yet been captured; numeric advice was validated in isolated tests rather than claimed as a live result.
