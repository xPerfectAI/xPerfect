# Evidence kit

Optional examples for repeatable evaluation, immutable source intake, terminal evidence export,
and verified delivery. Python 3.11+ on POSIX; no runtime dependencies. xPerfect owns execution,
attempts, uploads, callbacks and recovery. This package is not imported by the runtime.

From this directory, in a Python environment:

```sh
python -m pip install --no-deps -e .
xperfect-evidence --help
python -m pytest tests -q
```

Use a private directory outside the checkout for inputs, receipts and outputs. Roots must exist,
use their real absolute paths, and contain no symlinked ancestors. Commands do not delete source
files. The default limits are 1 MiB per JSON document, 1,000 files, 64 MiB per file, 256 MiB per
package and 120 seconds per command. File operations use a monotonic command deadline; HTTP has
bounded responses and socket timeouts. A network timeout is an uncertain reply, not evidence that
work did not start. Change limits in an application using the Python API when appropriate.

## One source-to-delivery example

Start the existing product with `./xperfect start` from the repository root. Connect an account and
choose the exact native profile/model in the existing UI or [API/MCP guide](../../docs/developer.md).
This kit does not choose a model, connect an account or change your runtime. Use one existing
worker for the following job; keep its `worker_id`.

Choose a **new** private directory and prepare synthetic data:

```sh
export XP_RUN_ROOT='<new real absolute private directory>'
export XP_API='http://127.0.0.1:8766'
export XPERFECT_API_TOKEN='<private API token>'
export XP_WORKER='<worker_id>'
python examples/prepare.py "$XP_RUN_ROOT"

xperfect-evidence check --kind source --manifest "$XP_RUN_ROOT/source-event.json"
xperfect-evidence stage --manifest "$XP_RUN_ROOT/source-event.json" \
  --source "$XP_RUN_ROOT/source" --state "$XP_RUN_ROOT/state"
xperfect-evidence upload --manifest "$XP_RUN_ROOT/source-event.json" \
  --state "$XP_RUN_ROOT/state" --worker "$XP_WORKER" --url "$XP_API"
```

The upload result contains the existing file owner’s IDs and exact revisions. Keep the returned
binding paths. Submit this job through the existing API or MCP: “Read the admitted original.txt
input. Copy its exact bytes to result.txt and return a link to result.txt.” Use the actual binding
path if it differs. For HTTP, use `POST /v1/workers/{worker_id}/assign` with an explicit
`Idempotency-Key`; `Client.assign` wraps that same operation and recovers an unclear reply by key.
The runtime rejects a changed request under the same key. An explicit correction is new work.

Read `GET /v1/runs/{run_id}` until terminal. Inspect the same worker in **Watch**, open the result,
then reload and inspect its history. Through MCP, use `run_get`, `workspace_status`,
`workspace_artifacts` and `workspace_artifact_download` with the same IDs. Starting the job once
through HTTP and again through MCP creates two jobs; choose one submission path.

Download the selected exact artifact from the existing API into the private `output/` directory:

```sh
curl --fail --silent --show-error --get \
  -H "Authorization: Bearer $XPERFECT_API_TOKEN" \
  "$XP_API/v1/workers/$XP_WORKER/artifacts/download" \
  --data-urlencode 'path=result.txt' -o "$XP_RUN_ROOT/output/result.txt"
```

Set `run_id` and `attempt_id` in private `package.json` from that run’s exact `run_id` and
`active_attempt_id`. Preserve the input/output digests. `evaluator/expected.json` was prepared
from the source before execution. Keep evaluator files and installed validator code outside the
worker’s authorized and writable roots. An administrator must enforce that isolation; merely
choosing a different folder does not establish an access boundary.

For an opt-in automated version of this same native copy example, use a **ready disposable worker**
and a new private directory. This command submits one paid/native model job with the explicit effort;
it checks the source, downloads the result, injects a lost publication reply, and recovers delivery
while checking unchanged run/attempt identities. It does not test callback ingress or isolation:

```sh
python examples/live_copy.py --directory '<new private directory>' --url "$XP_API" \
  --worker "$XP_WORKER" --effort '<explicit supported effort>' --case-policy insensitive --seconds 300
```

```sh
xperfect-evidence retain --package "$XP_RUN_ROOT/package.json" \
  --source-manifest "$XP_RUN_ROOT/source-event.json" --output "$XP_RUN_ROOT/output" \
  --retained "$XP_RUN_ROOT/retained" --policy "$XP_RUN_ROOT/evaluator/validator-policy.json" \
  --expected "$XP_RUN_ROOT/evaluator/expected.json" --url "$XP_API"
```

Keep the returned `package_sha256`, then publish one explicit generation:

```sh
export XP_PACKAGE='<returned package_sha256>'
xperfect-evidence publish --package-sha256 "$XP_PACKAGE" \
  --retained "$XP_RUN_ROOT/retained" --destination "$XP_RUN_ROOT/destination" \
  --transaction 'example-source-event-1' --generation 1 --previous 0 --case-policy insensitive --url "$XP_API"
```

The local destination is the reference adapter. It reserves the generation, copies and reads back
every declared byte, checks exact inventory, checks current run/attempt authority, then writes the
commit marker last. `HEAD.json` points to the verified generation. A new generation requires the
explicit predecessor. The destination’s Unicode and case rules are declared with `--normalization`
and required `--case-policy sensitive|insensitive`; the whole path map is checked before writes.
Declare the actual destination rules. Internal staged/retained files use logical-ID-derived names,
so physical instances cannot collide on case-insensitive disks. Source and retained roots
cannot overlap the destination. Links, traversal, hard links, special files and path collisions fail.

If delivery fails or its reply is lost, restore destination access and repeat the same command with
`redeliver` instead of `publish`. It reads retained verified bytes and current runtime authority;
it has no execution/fallback path. It does not need the original worker output directory. An exact
existing marker is adopted after readback; conflicting bytes or stale generations fail. A destination
that cannot be read remains uncertain. A cancellation before the final authority check prevents a
new commit. A commit racing with a later cancellation can already exist and is reported truthfully.
Changed retained bytes or validator code require separate investigation/correction, never a hidden
model rerun. Keep run/attempt counts before and after to verify delivery-only recovery.

The built-in `byte-map` validator checks a synthetic byte-copy contract. It binds every physical
source ID, output identity, digest, trusted validator version/code digest and reviewed denominator.
It does not implement business acceptance. Missing expected evidence remains unknown; partial
review remains partial. Native completion, structural validity, source correctness and delivery are
separate facts. `validator-policy` prints the installed code pin; pin it before work, from the trusted
installation. Never accept a validator path, module, command or replacement policy from a worker.

## Receive the existing callback

An application can configure the existing worker bootstrap `callbacks.events_webhook_url` and
`callbacks.hmac_secret`. Keep that secret outside worker-visible files. Use the product’s current
configuration authority; this example does not write the runtime database. The loopback reference
receiver expects the existing `X-GlassHive-Signature` HMAC over exact callback bytes.

Put the trusted source transaction’s `worker_id` and `run_id` in a private association JSON file.
For an existing delegated job, also include its real `origin_ref` and `work_ref`; the receiver then
uses `/v1/callback-associations/verify`. Plain HTTP assignments have no delegation association;
their binding uses HMAC plus authenticated current-worker/current-run readback. Callback content
does not grant authority or supply accepted output bytes.

```sh
export XPERFECT_CALLBACK_SECRET='<same private callback secret>'
xperfect-evidence receive --association '<private association JSON>' \
  --state "$XP_RUN_ROOT/state" --url "$XP_API" --port 8791 --seconds 120
```

The receiver durably accepts matching control events and keeps listening for a terminal result.
For versioned terminal results it returns the sender's exact result-revision/digest CAS
acknowledgment, then exits. Retries can refresh timestamps and action capabilities: each signed
transport digest is retained separately, while the accepted result identity stays stable.
Duplicate results are idempotent; conflicting or superseded revisions return a typed HTTP 409
receipt. Validation/publication is a separate application step, so ACK does not wait for delivery.
Failed terminal work is accepted as failed work. HTTP 202 does not establish correct output or
external delivery. The receiver binds only to loopback; use authenticated ingress for a remote
installation.

Source intake likewise records `(owner, source, event, revision, physical input)` identities.
Replay of one event recovers its receipt; changing it conflicts. Distinct events or physical inputs
with identical bytes remain distinct. Staging writes its source manifest last and recovers matching
partial copies. `cursor` demonstrates scoped expired-cursor overlap/reconciliation; a real connector
owns source cursors and must reread event revisions. No source file or message is changed.

## Pinned evaluation and actual isolation probes

`examples/cases.json` is a manifest template, not a ready benchmark. Replace its operator placeholders
with exact worker/profile/model/effort, source/image/harness/transport and task/input/skill/bootstrap
pins. The zero digests are placeholders and must never be claimed as measured pins. Keep case order
immutable. Supply a private observed-pin map keyed by case ID from the actual loaded runtime.

Run `probe-read` **inside the actual isolated worker execution context** for the allowed input and
the evaluator/control canaries. Create the evaluator/control canaries outside its grants and record
their existence and digests from the trusted operator side. Record the exact worker/image/mount
generation with the observed pins. A separate delegated case must read authorized parent context.

```sh
python -m xperfect_evidence probe-read --case-id copy-1 --probe-run-id '<probe execution reference>' \
  --path '<exact path as seen by the isolated worker>'
```

The probe performs a real kernel read and returns `read`, `denied`, `not_visible`, or `unavailable`,
with exit code, errno and content digest. `not_visible` proves isolation only when the operator has
independently proved that the canary exists outside the worker namespace. Missing prerequisites,
an unrelated missing path, mount counts, worker claims and literal flags are insufficient.

Retain the raw probe JSON outside worker access. The private receipt map is keyed by case ID; each
receipt binds `schema_version`, `case_id`, `context_mode`, `pins_sha256`, `allowed_roots`,
`probe_run_id`, and `checks`. Each check contains `kind` (`allowed_input`, `evaluator_root`,
`control_root`, plus `delegated_context` for delegated cases), `path`, `operation`, `exit_code`,
`result`, `evidence_path`, `evidence_sha256`, and the operator’s `canary_sha256`. The runner reads
and verifies the raw proof files rather than trusting a success Boolean. The probe execution
reference identifies operator evidence; it does not create another xPerfect run.

```sh
xperfect-evidence evaluate --manifest '<private pinned cases JSON>' \
  --inputs '<real admitted-input root>' --evidence '<private probe root>' --state '<private batch receipts root>' \
  --pins '<private observed-pin map>' --probes '<private probe receipt map>' --url "$XP_API" --maximum 1
```

This is one bounded submission/observation pass, not a scheduler. It checks all cases before
submission and uses current runtime idempotency. Resume recovers the same run; explicit reruns use
`--rerun` within the manifest’s declared bound. Setup failures and uncertain replies remain visible.
Use the same state directory and unchanged manifest when resuming. Do not silently exclude cases.
An explicit `excluded_reason` in a case is retained as an exclusion without a fake run or model
call. Preflight and assignment setup failures retain sanitized receipts. `score --dispositions`
accepts separate `{case_id, kind, reason}` rows for exclusions, setup failures and unjudged cases;
they are never counted as completed native attempts.

Remote run readback labels complete attempt history `unavailable`; it does not invent missing
attempts. An authorized local operator can add `--database`, `--tenant`, and `--owner` to collect
all existing scoped attempts read-only. Each observation is retained by digest, including changed
snapshots; the runtime ledger remains authoritative. Capture again after settlement for terminal
history. Missing local evidence is a real evaluation gap.

`score` consumes source-review observations, retaining all exact run/attempt identities. Report
checked/correct/total/unknown denominators and expected/output digests; baseline agreement is not
truth. First-pass rows are submission ordinal zero, including its runtime attempts. Later explicit
submissions stay separate. Actual batch wall time is separate from summed concurrent durations.
Usage receipts stay known/unknown and all attempts remain included. The core lineage regression
checks original, rendered and native stdin bytes without depending on this optional package.

## Export terminal evidence; hand off state recovery to G8

Adapt `examples/terminal-evidence.json` to exact settled attempt IDs and explicitly selected,
size/hash-pinned file membership. A terminal run does not freeze its workspace. Use immutable file
versions or a stable verified byte snapshot. Mutable/active files, unknown paths and links are not
accepted. This command may export settled evidence while unrelated work is active:

```sh
xperfect-evidence archive --manifest '<private terminal evidence JSON>' \
  --source '<private selected-evidence root>' --destination '<private archive root>' \
  --database '<existing local runtime database>' --tenant '<tenant ID>' --owner '<owner ID>' --case-policy insensitive
```

The explicit operator collector opens only the existing database with SQLite `mode=ro` and
`query_only`; it does not initialize `Store`, migrate or write SQL. It supports runtime-store
schema 13/14, reads only scoped exact attempt identities, and refuses unknown versions. Remote HTTP
does not expose complete attempt history or exact stdin; without trusted local evidence those facts
remain unknown. Archive segments commit only after exact destination inventory/hash readback.
Interrupted copies remain incomplete and can recover matching bytes. Unknown extra files or
abandoned temporary files fail closed; preserve that segment and use a new clean destination after
inspection. No automatic source cleanup occurs.

An archive segment is **not** a crash-consistent runtime backup. Use the existing
[G8 capture/restore procedure](../../docs/deployment.md) for authoritative state. Restore with the
capture’s release, then follow the existing upgrade path. Keep the restored runtime inert. `reconcile`
compares the exact existing G8 pending identities and hold-set digest with supplied restored rows;
it reports missing/conflicting/held identities and never unholds or dispatches work. Resume only
through the existing owner-authorized recovery control after reconciliation. File counts and PIDs
cannot authorize paid work.

## What the tests establish

The local bank checks path/security bounds, changed bytes, runtime idempotency, real kernel read
probe capture, upload revisions, source replay/cursor recovery, independent validator selection,
terminal export, concurrent generations, cancellation, lost replies and marker-last recovery.
Synthetic servers test transport contracts. They do not establish native-provider quality or
production isolation. Complete a disposable real API/MCP/browser journey, actual isolated worker
probes, current loaded-artifact verification and G8 restore/reconciliation before claiming those
paths pass. Keep raw inputs, runtime logs, tokens, receipts and screenshots private.
