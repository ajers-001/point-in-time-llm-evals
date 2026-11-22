# Two-GPU cloud continuation controller

Implements `specs/cloud-two-gpu-20261006/SPEC.md`: resume only the frozen 74 unattempted requests on at most two concurrent single-H100 Runpod workers, with your PC off. The prior 58 outputs are preserved and are never redispatched. This is development measurement; completing the queue is not a scientific success claim.

## Architecture

| Piece | Where it runs | Role |
|---|---|---|
| `controller.py` via `entrypoint.py` | ECS Fargate service, desired count 1 | Single fenced instance. Allocates, provisions, runs readiness checks, claims, dispatches, reconciles and releases pods. |
| `state.py`, `store.py` | DynamoDB `pit-2gpu-state` | Session, fenced lease, jobs, attempts, append-only events, pods, operator requests. |
| `watchdog.py` | Lambda, invoked every minute by EventBridge Scheduler | Independent of the controller and of any PC. Deletes only exact owned pods at GPU end, at the budget backstop or on request, then scales the controller to zero. Never creates compute. |
| `host_agent.py`, `host_admit.py` | Each GPU pod (root supervisor) | Fenced commands, exclusive per-attempt reservation, one fresh engine per request in the audited UID 10001 sandbox, 2-minute backups and immediate result uploads to append-only presigned slots, hard stop at inference end. |
| `artifacts.py`, `merge.py` | Controller / any shell | Bounded archive verification; immutable per-attempt copies with read-back; final 132-request merge with the prior audit. |

Times are derived from the absolute deadline D:
- GPU end is D − 3 min. From then the watchdog deletes pods; the margin allows for scheduler jitter.
- Inference end is GPU end − 2 min, leaving the retrieval reserve.
- New starts stop at inference end − max(7 min, worst measured duration + 2 min).

Admission reserves both workers at the quoted rate from now through GPU end plus 15 minutes, within the minimum of three limits:
- the session ceiling,
- the aggregate ceiling minus prior spend,
- observed Runpod credit.

All limits are net of reserves.

## Recovery rules (tested in `tests/test_cloud_two_gpu_*.py`)

- **Lost heartbeat, SSH timeout or controller restart.** The request goes to RECONCILING and is never dispatched again on that basis alone. It is resolved by its exact pid and start-time identity, host receipts and S3 uploads.
- **Retry.** A request is retried only after confirmed process death or verified pod absence, plus a recorded repair id, at most once per request.
- **Pod loss mid-request.** The controller replaces the pod automatically after verifying absence, up to 2 replacements per session, and the request is requeued with the replacement recorded as its repair.
- **Runtime failure.** The request stays FAILED_INFRA until an operator queues `--retry RUN --repair ID`. A host with two consecutive failures is paused for diagnosis.
- **Uncertain pod create.** Resolved against inventory by the exact unique pod name. A second create request is never sent.
- **Uploads.** Late or conflicting uploads are kept under their attempt id and recorded as conflicts. They never replace a canonical result.
- **Mismatches.** A config-hash mismatch blocks that request. A bootstrap or model-hash failure releases that host for operator review.

## Commands (any authenticated cloud shell, AWS credentials as explicit environment variables)

```sh
python -m infra.cloud_two_gpu.cli dry-run   --session S.json           # no cloud writes
python -m infra.cloud_two_gpu.cli deploy    --session S.json           # CPU controls only; no GPU
python -m infra.cloud_two_gpu.cli start     --session S.json --approve-paid-gpu-launch <session_id>
python -m infra.cloud_two_gpu.cli status    --session S.json
python -m infra.cloud_two_gpu.cli reconcile --session S.json --retry RUN --repair ID | --resume-slot N --repair ID | --stop
python -m infra.cloud_two_gpu.cli terminate --session S.json [--cleanup]
```

`S.json` is `specs/cloud-two-gpu-20261006/session.template.json` with a new `session_id`, the approved budget and an absolute `deadline_utc`. The CLI refuses it while any of those is still null.

## Benchmark-reproduction sessions (`queue_kind: benchmark-repro-v1`)

A session file may name a different frozen queue. `prepared/benchmark-repro-v1/` (built once by
`python -m pit_bench.prepare`, checked with `--check`) holds 14 jobs in pre-registered priority order:
the authors' own `upstream/evals/lmeval` harness, settings and scoring on Qwen3.5-27B for M&A and pharma
look-ahead plus MMLU-Pro, at base, the shipped DD+steering config, a DD alpha sweep and the paper's
Figure 4 steering clamps. Differences from the pilot queue:

- one fresh engine per *job*; the job's prompts are batched inside it as upstream does;
- one H200 per pod (`preferred_gpu`), so P and the DD aux pair share a GPU; `allowed_cuda_versions`
  narrows hosts to CUDA 13 drivers for the pinned torch build;
- the GPU sandbox receives prompt-only M&A/pharma parquets (`data/`); answers never mount there;
  `pit_bench.score` rescores every logged generation offline with the authors' functions;
- bootstrap additionally installs `pit_bench/requirements-evals.txt` (hash-locked, constrained to
  `upstream/uv.lock`) and fetches MMLU-Pro at a resolved, recorded revision.

`tests/test_pit_bench.py` covers frozen inputs, bundles, staging and a controller simulation;
`tests/test_pit_bench_harness.py` runs the real harness end to end on CPU with a fake model (needs the
eval dependencies, torch and vllm importable; skipped otherwise).

## Prerequisites (administrator, outside this repo)

- IAM deployer user and `pit-2gpu-runtime-boundary`: see `iam/README.md`.
- Secrets Manager (us-east-2):
  - `pit-2gpu/runpod-api-key`
  - `pit-2gpu/session/host-uploader` (append-only signer, see `iam/host-uploader-policy.json`)
- Runpod credit covering the session ceiling, with auto-pay off.

## Verified so far vs. not yet

- **Verified locally:**
  - queue invariants;
  - concurrency and fencing on the in-memory store and the DynamoDB emulator;
  - all recovery rules on simulated workers;
  - the real host agent CLI in fake-execution mode, over a pinned-key SSH server;
  - bundle allowlists;
  - stack rules: every role carries the permissions boundary, there are no users or keys, and the provider key is readable only by the controller and watchdog;
  - CloudFormation `ValidateTemplate`;
  - the controller container's boot path (bundle extraction, hash-locked install, imports).
- **Not yet verified (needs deployment):**
  - live AWS stack creation;
  - a live watchdog invocation;
  - the Runpod GraphQL account query shape;
  - real pod SSH, `uv` fetch, install, admission probes and weight download on a new host;
  - the laptop-off readiness run;
  - budget and deadline expiry injection against real pods.
