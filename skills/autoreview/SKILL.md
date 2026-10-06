---
name: autoreview
description: "Structured code review when explicitly requested, preferring OpenAI/Codex before Claude."
---

# Auto Review

Run an independent review when the user or an owning workflow asks for one.
This is code review, not Guardian approval routing. Let the reviewer choose how
to analyze the change; provide the target, relevant context, and desired severity.
Findings are advice to verify, not instructions to apply blindly.

Before starting a review, read the complete [diagnostic and result guidance](references/diagnostics-and-results.md). It is part of this skill; follow its
output-path, status, failure, usage, and diagnostic rules.

## Run

Use `scripts/autoreview` beside this skill. Keep its custom `codex exec` path:
native `codex review` cannot combine explicit Git target flags with custom instructions.
The helper combines those with evidence, severity filtering, and validated JSON;
it leaves review judgment to Codex. For an OpenClaw checkout:

```bash
AUTOREVIEW=".agents/skills/autoreview/scripts/autoreview"
"$AUTOREVIEW" --mode local
```

In the canonical agent-skills repo, the path is
`skills/autoreview/scripts/autoreview`. On Windows, invoke the helper with Python.
Use `--help` for the complete flags and environment overrides.

Choose the Git target explicitly when the default is ambiguous:

| Target                         | Arguments                      | Scope                                                       |
| ------------------------------ | ------------------------------ | ----------------------------------------------------------- |
| Local work                     | `--mode local`                 | HEAD → index → working tree, plus untracked files           |
| Local candidate against a base | `--mode local --base <ref>`    | Pinned base → index → working tree, plus untracked files    |
| Committed branch/PR            | `--mode branch --base <ref>`   | Merge-base → HEAD; excludes dirty work                      |
| One commit                     | `--mode commit --commit <ref>` | Raw parent → commit; a root compares against the empty tree |

`--mode auto` selects local work when dirty, otherwise a branch review using the
PR base or `origin/main`. Clean main has no implicit review target.
`--mode uncommitted` is an alias for local. The helper does not fetch refs.

Registered nested linked checkouts from the same repository are outside the
current review scope. Their presence or edits do not make the parent dirty;
ordinary adjacent files remain included in the review. Worktree boundaries are
revalidated without changing Git ignore rules.

For a complete PR candidate **including dirty rewrites**, use local mode with
its pinned merge base—not branch mode:

```bash
pr_base=$(gh pr view --json baseRefName --jq .baseRefName)
merge_base=$(git merge-base HEAD "origin/$pr_base")
"$AUTOREVIEW" --mode local --base "$merge_base"
```

The helper refuses any target whose diff includes binary changes it cannot
review (fonts, archives, modified images, or image additions outside the
[image review](#image-review) path), and there is no path filter. Binary
deletions are reviewed as metadata. To review the text half of such a branch,
build a throwaway branch from the base that carries only the text files, and
review that with `--mode branch`; say in `--prompt` that the binaries were
reviewed separately:

```bash
git switch -c review/<branch>-text origin/master
git diff --name-only origin/master <branch> | grep -v -E '\.(png|jpe?g|webp|ttf|otf|zip)$' \
  | xargs git restore --source=<branch> --staged --worktree --
git commit -m "review: text-only view of <branch>"
"$AUTOREVIEW" --mode branch --base origin/master ...
git switch <branch> && git branch -D review/<branch>-text
```

When a file has both staged and unstaged changes, both states are reviewed.
A defect in the index remains actionable even if the working tree fixes it;
the report labels it `INDEX-only`.
Git display settings cannot suppress context markers or add patch colors;
repository configuration is not changed. Source paths and text retain literal
whitespace. An empty present
source uses line 1, column 1, and an empty excerpt; empty physical lines also
use an empty excerpt at column 1. Source identity remains mandatory.

Binary deletions remain in scope as Git deletion metadata; their former contents
are not included or reviewed. Each local transition is checked independently:
deleting a file in the working tree cannot hide a staged binary change.

Finding locations may use native absolute paths that resolve inside the reviewed
repository; these become repository-relative paths before scope and attribution
checks, preserving a changed symlink's path when its target is also inside.
Parent traversal and paths resolving outside the repository remain invalid.
An invalid location still fails the report; findings are never silently dropped.

Local selection honors `core.autocrlf` from external operator Git configuration,
with repository-local values and attributes retaining precedence. Only its
validated scalar value reaches diff/status; other global and system Git
configuration stays disabled. Repository-owned or relative global-config
overrides are not imported, and reviewed source bytes are not rewritten.

Local collection disables effective Git clean/process commands and requires
conversion to succeed. Unused drivers, unchanged filtered neighbors, staged-only
changes, and deletions can still be reviewed without executing converters.
If Git needs executable conversion to assemble the diff, collection fails before
any reviewer starts. This can include an unchanged filtered file whose stat cache
needs refreshing. Use explicit branch or commit mode for committed content in
that case. Built-in line-ending normalization remains enabled; raw bytes never
stand in for a required executable conversion.
PR-base discovery uses trusted external Git and a scoped GitHub CLI environment,
preserving external authentication/configuration and proxy settings while excluding
inherited Git routing, `GH_REPO` redirection, and checkout-owned executables.
A differently named `AUTOREVIEW_GIT` override that cannot also be selected as `git`
by the child requires an explicit `--base`; rejected GitHub configuration paths
also require one.

## Context and severity

Use `--prompt` for task-specific guidance, or `--prompt-file` and `--dataset` for
repository-relative context files. Context does not expand the selected Git
target. The reviewer cannot read unchanged repository files from its empty
sandbox; supply relevant source or dependency evidence when the diff is insufficient.
`--prompt-file` also accepts an absolute path inside the repository; the same
sensitive-path, symlink, and mutation checks apply. `--dataset` stays repo-relative.
Repeated paths in the same evidence role share one validated capture. Equal
content at different paths and prompt-file versus dataset roles stay distinct.

For unchanged committed source, use repeatable `--source-context <repo-relative-path>`
with branch or commit mode. Use `--source-context-file <repo-relative-path>` when
that source must stay intact in every review pass. Both read the exact regular-file
blob from the frozen reviewed commit (branch HEAD or `--commit`), including executable source files.
Local mode, including an auto-selected local target, is unsupported. No separate
context revision or working-copy substitution is accepted. The checkout path must
remain a regular file; its bytes and path topology are revalidated throughout review.
Repeated normalized source-context paths share one capture after every argument
is validated; different paths and evidence roles remain distinct.

Both roles use tracked-source filename classification, so source names such as
`src/token_count.py` are accepted. Credential directories, stores and keyfiles
remain forbidden. Existing prompt-file and dataset restrictions are unchanged.
Every source block carries path, commit, blob and mode provenance. `--source-context`
bytes are partitioned with the change when needed. `--source-context-file` blocks
stay complete in every pass and must fit with the instructions and change framing;
the helper refuses an over-capacity plan without dropping required evidence.
Context never adds finding targets or instruction authority. This is a
source-provenance contract, not secret-content scanning.

```bash
"$AUTOREVIEW" --mode branch --base origin/main --source-context src/token_count.py
"$AUTOREVIEW" --mode branch --base origin/main --source-context-file src/token_count.py
```

The default threshold is **P0 only**: material blockers to normal operation or
safety. Use `--max-priority P1`, `P2`, or `P3` when the caller requests a wider
review. `AUTOREVIEW_MAX_PRIORITY` accepts the same `P0`–`P3` values; an explicit
flag overrides it. Invalid resolved priorities fail during argument parsing,
before preparation or reviewer startup.
Do not add unrelated redesign goals or prescribe file counts, reading
sequences, or ritual extra passes. Historical blame requires a verified
parent-relative patch; otherwise leave the attribution unknown.

```bash
"$AUTOREVIEW" --mode local --prompt-file review-notes.md --dataset evidence.json
```

## Engines

For automatic reviewer selection, try OpenAI models through Codex before Claude.
Start with `--engine codex` even when the invoking agent uses Codex or asks for
an independent second opinion. Use Claude only when the user explicitly selects
it or Codex is unavailable for the review; report the concrete availability failure
before switching. Do not switch because a review is slow, rate-limited, or returns
findings, or to bypass a safety refusal or isolation failure.

Codex defaults to `gpt-6.1-sol`, high reasoning, with a single `gpt-6-sol` retry
only for an account-access failure. Explicit `gpt-6.1-sol` selections use the same
retry. Explicit `gpt-6-sol` selections retain their access-only `gpt-6-luna` retry;
other explicit models, including Luna and Astra, have no model fallback.
Explicit `gpt-5.6-sol` selections retain their access-only `gpt-5.6-terra` retry.
GPT-6.1 Sol rejects `none` and `minimal` effort before review preparation;
GPT-6 Sol and Luna reject `minimal`. An effort-only override keeps the default model.
Honor explicit user engine/model choices.
The helper does not automatically fall back between engines.

Use `--engine`, `--model`, and `--thinking` to override the defaults.
`--codex-speed fast` selects priority service when supported; `--codex-speed ultrafast`
selects Ultrafast when the active model catalog lists it (Codex otherwise silently
sends the standard tier). Only Claude accepts
`--fallback-model`. Per-engine environment overrides use `AUTOREVIEW_<ENGINE>_*`.

If your account cannot access Sol or Luna, pin an available model. To require
GPT-6 Astra without a model fallback, select it explicitly:

```bash
"$AUTOREVIEW" --mode local --model gpt-6-astra --thinking high
```

GPT-6.1 Sol and GPT-6 Astra support `low`, `medium`, `high`, `xhigh`, and `max`;
neither supports `none` or `minimal`. GPT-6 Sol and Luna additionally support `none`,
but not `minimal`. AutoReview defaults to
`high` and does not fall back from an explicit Luna or Astra selection.
Codex's `ultra` mode uses automatic
delegation and is outside this helper's supported effort levels. Use `max`
for its deepest supported review. For EU data residency, use
`--codex-speed default`; GPT-6 fast mode is unavailable there.
See the [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol),
[GPT-6 Sol](https://developers.openai.com/api/docs/models/gpt-6-sol), and
[GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) model docs
and [Codex reasoning modes](https://learn.chatgpt.com/docs/models#know-when-to-use-max-or-ultra).

By default, Codex preserves only authentication settings from user configuration;
provider, profile, context and catalogue settings remain ignored. To project a
named route, select it explicitly through the existing config override:

```bash
"$AUTOREVIEW" --mode local --codex-config 'model_provider="review_api"'
```

The selector must match `model_provider` in the operator's external
`CODEX_HOME/config.toml`. It accepts one bare or simply quoted identifier;
provider definitions and other capabilities cannot be supplied through overrides.
Projection requires Python 3.11 or `tomli`; default auth-only operation retains
its existing fallback parser.

The selected route must use `https://api.openai.com/v1` and command authentication
with an absolute external executable. Fixed arguments belong in that executable's
wrapper; omitted or empty `auth.args` are accepted. Omitted `wire_api` and
`requires_openai_auth` retain Codex's `responses` and `false` defaults. Optional
auth timing and context settings keep native defaults and semantics.

On POSIX, a private launcher restores the validated caller `HOME` only for the
selected authentication executable; the engine and reviewer tools retain their
isolated environment and filesystem access. Caller `HOME` must be an available
absolute directory with no repository-owned path or symlink provenance. Windows
keeps the native executable route. Command-auth runs suppress raw provider
diagnostics and report fixed failure categories, while retaining compact progress,
usage and assistant report streaming. An empty final report fails without exposing
captured stdout.

Catalogue and authentication working-directory paths resolve relative to the
operator config directory and must remain outside the reviewed repository.
A supplied catalogue is copied byte-for-byte into the private client runtime;
retries use the same route and catalogue snapshot. Dry runs check the same
ownership and route shape without executing authentication. Codex owns catalogue
validation, model access and context clamping. Other custom provider forms and
split context overrides are unsupported when projection is selected.

| Optional engine | Prerequisites                                                                                         |
| --------------- | ----------------------------------------------------------------------------------------------------- |
| Claude          | CLI 2.1.169+; safe mode with web-only tools                                                           |
| Amp             | `AMP_API_KEY` for a plugin-free account; local POSIX execution, no custom endpoint or cloud/orb agent |
| Pi              | CLI 0.79.0+; configured model; no tools or project resources                                          |

`--engine kimi` remains recognized but is refused for reviews and `--dry-run`
before any Kimi process, configuration read or authentication setup. The supported
Kimi Code prompt mode accepts review content only as a command-line argument;
the helper has no supported private prompt input channel for it. This intentionally
retires the previous Kimi execution path rather than exposing the bundle in process
arguments. Existing `--kimi-bin` arguments remain accepted for the same clear refusal;
the helper never silently selects another engine. A custom agent file is not an
equivalent replacement because it changes the input into a templated system prompt.

## Image review

Branch mode with Codex supports **added, single-frame PNG, JPEG and WebP** files.
Install Pillow in the Python environment running the helper (`python -m pip install Pillow`).
Use a vision-capable Codex model and a CLI supporting `codex exec --image`.
No new bypass flag is required. Full decoding rejects corrupt and animated files.
Images must have at most 16,777,216 pixels and no dimension above 16,384 pixels;
decoder bomb warnings fail closed before pixel loading. Added image paths are
limited to 20 MiB of encoded bytes each and 100 MiB total, checked against Git
object sizes before capture. Exceeding a limit fails the entire review.

The helper captures exact bytes from the pinned HEAD, stages only those images
in its isolated workspace, and attaches them through Codex's native image input.
Every pass receives the path, media type, byte count and SHA-256 manifest alongside
the image attachments and text diff. Image findings use the original path and line 1.
Text-only review does not require Pillow.

Binary deletions, including images, are reviewed as deletion metadata in every
mode without image attachments or Pillow. Other binaries, modified images,
local/commit image additions or modifications, and image review with other engines
remain unsupported and fail closed. Missing Pillow or provider
image limits fail the review rather than silently dropping assets. Sensitive-path,
source-mutation, authentication and sandbox controls remain enabled.

For partial clones, materialize required Git objects **before** review. The isolated
Git reader intentionally disables lazy network fetching; do not weaken that boundary.

### Explicit authentication and profiles

This fork retains explicit authentication routes and private run history.
`--codex-auth chatgpt` forces stored ChatGPT authentication and rejects a
simultaneous provider profile. `--codex-auth default` preserves the selected
provider environment. Use `--codex-profile NAME` to select an operator-owned
`CODEX_HOME/NAME.config.toml` alternative configuration for a built-in Amazon
Bedrock provider. Only its validated model, effort, service tier, and AWS region
enter the isolated reviewer runtime; hooks, tools, and trust settings do not.

Claude accepts `--claude-auth default`, `subscription`, `bedrock`, or `mantle`
and `--claude-bedrock-region`. Bedrock Runtime and Mantle have different model
availability; select the model ID and region that work on the chosen route.
Mantle defaults to `anthropic.claude-opus-5-5[1m]` at `xhigh`; Bedrock Runtime
to `global.anthropic.claude-fable-5-1[1m]` at `high`.
Explicit auth routes require Claude CLI 2.1.237 or newer.

The retained Fable refusal policy retries a model-refusal result once in a
fresh Claude session. Only after a second refusal, the same engine may retry
Opus 5.5 at max effort (through Mantle for Bedrock or Mantle callers). Other
failures and non-Fable refusals do not trigger this model switch. Each attempt
and route change is recorded in private run history.

Route fallback is opt-in and off when unset. After the primary route is
exhausted (including the retries above) and the reviewer is unavailable —
engine failure, auth or entitlement failure, unavailable model, any model
refusal, invalid or empty report, or timeout — the helper retries the same
frozen prompt once on another route:

- `--claude-fallback-auth` / `AUTOREVIEW_CLAUDE_FALLBACK_AUTH`: `subscription`,
  `mantle`, `bedrock`, or `default`.
- `--claude-fallback-auth-model` / `AUTOREVIEW_CLAUDE_FALLBACK_AUTH_MODEL`:
  defaults to the primary model adapted to the route; subscription strips the
  provider prefix and `[1m]` (`anthropic.claude-opus-5-5[1m]` → `claude-opus-5-5`).
- `--codex-fallback-auth` / `AUTOREVIEW_CODEX_FALLBACK_AUTH`: `chatgpt`.
- `--codex-fallback-profile` / `AUTOREVIEW_CODEX_FALLBACK_PROFILE`: retry through
  another capability-free `CODEX_HOME/NAME.config.toml` Bedrock profile instead,
  for example GPT-6 Sol on Bedrock Runtime (built-in `amazon-bedrock-runtime`
  provider). Exclusive with `--codex-fallback-auth`; a profile equal to the
  primary profile is ignored. The route keeps default auth and the Bedrock
  credential environment, and is staged exactly like `--codex-profile`.
- `--codex-fallback-auth-model` / `AUTOREVIEW_CODEX_FALLBACK_AUTH_MODEL`:
  defaults to the primary model without `openai.` for the ChatGPT route
  (`openai.gpt-6-sol` → `gpt-6-sol`). A profile route uses the fallback
  profile's own `model` (Mantle and Runtime spell GPT-6 ids differently, e.g.
  `openai.gpt-6-sol` vs `global.openai.gpt-6-sol`), so the primary model is not
  carried over unless you set this explicitly.

The fallback keeps the primary thinking level (validated for the fallback
model). The Codex ChatGPT fallback drops `--codex-profile` and any `model_provider`
override and uses the same ChatGPT-auth isolation as `--codex-auth chatgpt`; the
Codex profile fallback swaps the profile and drops any `model_provider` override.
The ChatGPT route needs `--model` or an explicit fallback model when the profile selects the primary model. A
fallback equal to the primary route is ignored. Interrupts, source or evidence
changes, pre-send scan refusals, setup and isolation errors, and completed
reviews never fall back. Later review passes stay on the fallback route. A
failed fallback reports its own failure plus a one-line primary summary. Run
history records a `route_fallback` attempt; the status sidecar is unchanged.
Dry runs also check the fallback route's startup.

For two independent opinions, run two invocations with `--engine codex` and
`--engine claude`, using the same pinned Git scope and unchanged source, in
parallel when the owning workflow asks for both. Give each invocation distinct
output paths and inspect both outcomes. There are no built-in panels, combined
verdicts, or cross-engine retries. Defaults and auth choices remain per engine.

## Runtime boundaries

The helper owns reviewer isolation, sanitized authentication, process cleanup,
Git scope, and structured result validation. Keep those controls enabled.
Before repository detection or target selection, Git must pass `--version`
within 10 seconds. Failure exits `2` with an `incomplete` diagnostic and the
resolved executable (or the unresolved selection); it never means `scoped-clean`.
Set `AUTOREVIEW_GIT` to a trusted external Git executable to override every
helper-owned Git invocation. On macOS with a broken selected Xcode, use
`DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer` for the invocation.
Only an absolute, external `DEVELOPER_DIR` is additionally retained in Git's
sanitized environment;
neither override is forwarded to the isolated reviewer environment.

Every reviewer pass must inspect its bundle for real credentials and report
suspected credentials as P0 findings without reproducing their values. Harmless
placeholders and test fixtures are not credentials. Never work around an
isolation failure.

### Pre-send scanning in this fork

The maintained fork retains its existing TruffleHog pre-send check. Each exact
outgoing prompt is written to an owner-only temporary file and scanned for
`verified,unknown` findings before transmission. Findings, scanner errors, and
a missing scanner refuse the send; credentials are never redacted and forwarded.
The scan includes prompt and dataset context, untracked content, and deleted
diff lines. Image attachments are sent as native image input, not text; their
path and SHA-256 manifest are part of the scanned prompt, but the pixels are
not scanned. This is a deliberate fork customization; upstream's scanner-free
policy is documented in [#240](https://github.com/openclaw/agent-skills/pull/240).

### Reviewer isolation

On macOS, reviewer tools cannot access the shared `/tmp` and `/var/tmp` trees
(including their `/private` aliases). Codex preflight rejects those temporary
roots before workspace, runtime, or authentication setup; unset a shared
`TMPDIR`/`TMP`/`TEMP` override to use macOS's private
temporary directory. Other engines and platforms retain their normal isolation.
Tools installed in shared scratch or requiring writes there will be denied too.

Text review files have no size/count cap and are never truncated; image inputs
use the explicit safety limits above. Large diffs and
datasets are partitioned automatically. Change partitions retain complete
datasets when they fit with sufficient change space. This preference may use more
passes or prompt bytes than evidence batching; the explicit pass budget still applies.
Terminal fallbacks preserve a feasible complete-evidence plan when batch framing cannot fit.
Intact instructions, source-context files and required mixed source context must
fit the per-pass prompt budget. A failed pass does not produce a partial clean verdict.
Otherwise, the planner compares a bounded set of evidence allocations and keeps the existing
plan unless total prompt bytes improve without more passes, or equal bytes need
fewer passes. Every change is still reviewed against every evidence batch.

Each pass is an independent assignment, not a continuing conversation. Its
private completion field must confirm a finished assessment; deferring to
another pass leaves the overall review incomplete.

Do not edit inputs during a review: the helper verifies captured sources before
sending and publishing results. Long reviews are normal; advancing heartbeats
mean progress. Use `--stream-engine-output` for visibility, not extra reviewer
runs. `--dry-run` checks preparation and startup without contacting a reviewer.
Both dry runs and execution print planned pass count and total prompt bytes.
Use `--max-review-passes N` (or `AUTOREVIEW_MAX_REVIEW_PASSES`) to reject the whole
plan before any reviewer starts when it exceeds an explicit campaign budget.
There is no default pass ceiling. `--engine-timeout-seconds` remains an optional
deadline per process attempt. Pass counts, prompt bytes, and deadlines are not
token hard caps; they do not bound model reasoning or tool use.

## Diagnostics and results

Follow the [diagnostic and result guidance](references/diagnostics-and-results.md)
for local stage observation, output paths, exit codes, status, and usage.

## Private run history

Owner-only metadata records model, effort, route, duration, retries, refusals,
service tier, and outcome. `--no-run-log` disables it. Set
`AUTOREVIEW_RUN_LOG_DIR` to an external private history root; set
`AUTOREVIEW_RUN_LOG_BUNDLE=1` to retain scanner-approved bundles, prompts, and
final reports. Do not store history inside the reviewed repository or commit it.
Use the adjacent `scripts/autoreview-history` helper to inspect or summarize
runs; its `--help` lists supported commands.
