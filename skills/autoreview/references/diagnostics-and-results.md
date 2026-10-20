# Diagnostic and result guidance

Exit codes, result interpretation, finding decisions:
[SKILL.md](../SKILL.md#diagnostics-and-results).
Read for output-path edge cases, machine status, usage accounting, local stage observation.

## Local stage diagnostic

- Observe first streamed Codex process: `--engine-stage-dir /outside/repo/fresh-private-directory`
  + `--stream-engine-output`.
- Create/exclusively hold fresh mode-`0700` directory on responsive local POSIX
  storage. Helper neither creates it nor overwrites `engine-stage.json`.
- No observation for other engines, buffered output, dry runs; no environment
  variable activates it.

- Sidecar: only closed booleans/enums, ≤4096 bytes; local launch, read, parser,
  display, first-process terminal observations. Retries/passes never update it;
  terminal state can differ from review outcome.
- `spoofable` always true; `provider_receipt`/`acceptance` always false.
- Missing/false = unknown, not provider activity, native initialization, acceptance.
  Redaction wrapper leaves flushed-display observations unknown.

- Ordinary observation/persistence failures preserve primary result. Persistence
  rejects repository paths, symlinks, unsafe ownership/permissions, existing names.
- Failed cleanup may leave private `.engine-stage.partial` or unresolved closure;
  retain custody when clearing it. Never retry ambiguous closes.
- Cancellation propagates; cleanup cancellation can replace pending exception
  after remaining attempts.
- Assumes ordinary descriptor acquisition/assignment, trusted caller custody,
  responsive storage; not hostile same-user interference, concurrent descriptor
  reuse, arbitrary interruption, network/stalled filesystems, crash durability.
- No change to report, usage, deadline, exit, isolation rules.

## Results

- All output paths must be outside repo as both final entries (resolve parent
  symlinks) and resolved referents.
- Without `--status-output`: human/JSON outputs must name distinct final entries;
  distinct symlinks/hardlinks may share existing referent (publication replaces entries).
- Status enabled: all resolved referents must differ; refuse hardlink aliases.
- Conservatively refuse case-only/Unicode-normalization aliases on every platform.
- Tilde/relative paths expand once, anchored to invocation directory.
- Atomic writes replace final symlink; never modify its target.
- After path and alias checks, probe report parents for file creation, writing,
  and atomic replacement before target selection or reviewer startup. Existing
  reports remain untouched; missing parents are not created. This checks current
  storage readiness, not capacity or permissions for a later report write.
  Existing read-only Windows report entries are refused before the probe.

- Event streams: non-null `structured_output` must contain report object;
  only absent/null permits `result`. Last terminal event authoritative.

- `--status-output /outside/repo/status.json`: versioned outcome; validated
  `--json-output` report format unchanged. Completed status values:
  `scoped-clean`, `findings`, `filtered`, `incorrect`, `incomplete`.
- Launched reviewer fails or returns invalid output: `reviewer_unavailable` (exit 1).
- Failed later pass never publishes partial report.

```json
{
  "schema_version": 1,
  "status": "reviewer_unavailable",
  "exit_code": 1,
  "engine": "codex",
  "report_produced": false,
  "reason": "engine_failed",
  "reviewer_exit_code": 124,
  "timed_out": true
}
```

- Unavailable reasons: `engine_failed`, `invalid_report`, `runtime_validation_failed`;
  completed reviews use null.
- Runtime validation: Amp post-launch isolation attestation/private-result check
  refused output, not transient-provider failure.
- `reviewer_exit_code`: retained last process code (possibly zero for rejected
  output), otherwise null. `timed_out`: helper's deadline, not merely process exit 124.
- Inspect status independently of `--expect-findings` exit changes.

- Unfinished assessment: keep validated observations with `incomplete`, exit 2,
  `report_produced: true`, even with findings. Never publish private completion field.
- Missing/invalid completion: invalid report, not unfinished.

- Status contains no logs, prompts or findings. OpenClaw status may include
  `effective_models`, the unique provider/model identities observed in validated
  terminal responses; rejected or missing identities are not recorded. Other
  engines' status fields are unchanged.
- Bounded display-safe stderr diagnostics + command-auth suppression stay enabled.
- Use a fresh status path for real reviews: after argument/path validation, remove
  old status before target selection.
- Dry runs validate output paths but do not create, replace, or remove output files.
  Any existing status belongs to a prior invocation, not the dry run.
- Preflight/pre-launch isolation refusals, source mutations, interruptions,
  output failures: no new status. Absence never means clean verdict.

## Preflight summary

`--preflight-output PATH --preflight-repo owner/repository` writes one raw
`openclaw-preflight:v1` comment, at most 4096 UTF-8 bytes. The explicit repository
is a caller claim about the target, never inferred from a fork's remote.
Only a clean, nonempty branch range is supported, including `auto` when it
selects a branch. Base and head identify the actual captured merge-base and
commit; local/index/working-tree mixtures are not relabelled as commits.

The deterministic summary includes the selected priority, native status, and
known accepted/filtered/incomplete counts. Priorities below the selected threshold are not assessed.
Validated `incomplete` maps to `partial`; other validated outcomes map to
`complete`, including findings and incorrect verdicts. Neither means clean.
The descriptive `source-<12 hex>` version fingerprints the helper file; it is
not a released package version or trusted execution evidence.

The output follows the same external-path/atomic-write rules as native reports,
with distinct destinations outside the engine-stage directory. A prior block is removed before review. No safe
validated outcome, source drift, or unavailable reviewer means no new block.
An export failure after native publication warns on stderr and leaves the native
report, status, and exit unchanged. Check that the new file exists before sharing.
The helper does not attach, upload, fetch a report, or bypass hosted review.
Summary JSON carries no model prose, source text, report hash, roles, or usage.

## Usage

- Codex collects usage with live display on/off. Reports, status, terminal summary:
  process attempts; reported, unknown, partial counts; `complete`; observed token totals.
- Each attempt contributes last valid cumulative snapshot once, including access
  retries + failed passes. Retry policy: [engine options](engines.md).

- Cached input/reasoning output: subsets, not extra totals.
- Counts: observed tokens, not billing estimates or cache-hit promises.
- Missing telemetry (including all-zero defaults without sample): unknown, never
  measured zero. `tokens: null`: no usable totals; incomplete totals: lower bounds.
- Interrupted runs: print retained usage; never publish status/reports.
- Other engines do not aggregate report usage; private run history records
  per-seat Claude and Codex usage (see
  [Seat metrics in run history](#seat-metrics-in-run-history)).

## Seat metrics in run history

Run metadata with `seat_metrics_version: 1` adds these fields to every
`reviewer_runs` entry (a seat: one reviewer on one review pass) and every
`attempts` entry (one engine process, linked by `reviewer_run_id`). `null`
means not reported or not applicable, never zero. `schema_version` stays 1 and
existing fields keep their meaning, so older readers are unaffected.

- `model_requested`: the model the seat asked for; null when a Codex profile
  chose it. `model` keeps its existing meaning and follows fallbacks.
- `model_actual`, `model_actual_source`: Claude reports it (`engine_reported`,
  the `modelUsage` model with the most output). Codex reports no model, so this
  is the `--model` it received (`cli`) or the staged profile's model
  (`codex_profile`).
- `effort`, `effort_source`, `service_tier`, `service_tier_source`: Codex values
  follow its command precedence (`cli`, then `codex_config`, then
  `codex_profile`; null is the engine default). The helper passes its own medium
  effort and Ultrafast tier defaults as flags when no profile is selected, so
  they are recorded as `cli`. The Codex tier is the requested
  tier because Codex does not echo the applied one. Claude reports
  `usage.service_tier` (`engine_reported`); its effort is the `--effort` passed.
- `tokens`: `input_tokens`, `cache_read_tokens`, `cache_write_tokens` (Claude
  adds `cache_write_5m_tokens` and `cache_write_1h_tokens`), `output_tokens`,
  `reasoning_tokens` (a subset of output), `input_total_tokens`, and Claude's
  `web_search_requests` and `web_fetch_requests`. `input_includes_cache` is true
  for Codex, whose `input_tokens` already contains cached and cache-written
  prompt tokens, and false for Claude, so compare `input_total_tokens` across
  engines. `complete` is false when a process lacked terminal usage; totals are
  then a lower bound.
- `cost_usd`, `cost_source`: Claude Code's `total_cost_usd` (`engine_reported`;
  `model_usage` keeps each model's `cost_basis`, for example `list`). Codex
  reports no cost, so it stays null. Cost is never estimated.
- `model_usage`: Claude's per-model tokens and cost.
- Seats only: `verdict` (the provider's `overall_correctness`),
  `review_completion`, `overall_confidence`, `findings_by_priority` (accepted
  findings, matching `findings`), `provider_findings_by_priority` (every finding
  the model returned), and `findings_rejected` (`out_of_scope`, `attribution`,
  `below_max_priority`). These are the inputs of the published `review_status`,
  which is still computed once per run. Failed seats keep observed usage and
  leave them null.

A seat sums its processes, including refusal retries and access or route
fallbacks; its model, effort, and tier come from its last process. Only counts,
costs, model ids, and short enums are stored, never prompts, report text,
session ids, or environment values. Amp, Pi, and OpenClaw seats carry the fields
as null.
