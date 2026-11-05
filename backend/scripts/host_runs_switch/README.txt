Confirmed desliming RUNS switch - host-local component

Extract the whole ZIP. Run SwitchDeslimingRuns.cmd on the production host.
The combined update wrapper runs this component automatically after both
components pass their zero-write preflight checks.

Python CLI:
  switch_runs.py --install-dir <host-root> --dry-run
  switch_runs.py --install-dir <host-root>
Success returns 0; a failed preflight or apply returns 1.

Requires the already installed two RUNS points and an active healthy collector.
Removes only the two exact 473/474 if_prod active definitions; all other point
definitions, connection settings and 5/60/30-second policies are preserved.
RUNS definitions keep their existing bytes and metadata definition identities.

Only the current desliming choice is migrated when it explicitly uses one of
the two exact retired keys from kepserver-main. Prior revisions remain intact.
The host clock sets one immutable cutover marker; reruns keep it unchanged.
Before that cutoff, non-live history can still read the original retired points.
Fresh readings, status groups and EXPERT8 use RUNS with the existing freshness,
quality, batch and configuration-change protections. No PLC writes are made.

The component restarts only its precisely owned Web and collector processes,
waits for the previous lease to expire naturally and checks new ownership,
advancing committed samples and fresh Good Boolean RUNS values before success.
It never clears database leases or ordinary spool files.

Backups include the exact old configuration, four runtime source files and a
small journal of the affected metadata only. No full database backup or scan.
Rollback restores only this component's changes and refuses to overwrite an
external edit. A power loss can leave a journal marked prepared or
switched-verifying; retain that journal and inspect it before manual recovery.

Unknown installed source hashes are refused. The package manifest lists the
finite reviewed source baselines; the actual host hashes are verified locally
by preflight, not claimed to have been remotely measured.

v4 includes two exact variants made from the user's four-file read-only host
capture. They preserve the host's original absence of filter-press aliases.
The measured derived.py and Inputs.py source families must be selected as a
pair; mixing them with the other family or a partial source update is refused.
All bytes outside the specific RUNS/cutover edits are retained. A changed or
unknown source is still refused, with only relative path, SHA-256, byte count,
UTF-8 BOM and line-ending counts included in the error and failure callback.

This component does not install the density-trend frontend. The top-level
combined wrapper reports the two components separately and reports overall
success only after both complete.
