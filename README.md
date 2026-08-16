# sample-audit-runner (admin-sonicloud)

GHA runner repo for the sample-library audit. The actual audit script,
batch plan, manifests, and results all live in
[`zulfikarbarbora-outl/web-daw-samples`](https://github.com/zulfikarbarbora-outl/web-daw-samples)
(private).

## Why this repo exists

This repo (under `admin-sonicloud`) has working GitHub Actions billing.
The data repo `zulfikarbarbora-outl/web-daw-samples` lives under a
different account (`zmmac1` has write access) whose GHA is billing-locked.
So we host the workflow here and use the `zmmac1` PAT
(stored as the `WEB_DAW_SAMPLES_PAT` secret) to checkout and push back
to `web-daw-samples`.

See **[PAT_SEPARATION.md](PAT_SEPARATION.md)** for the full PAT setup.

## What the workflow does

For each batch index in the matrix (max-parallel: 10):

1. Checkout `zulfikarbarbora-outl/web-daw-samples` (private) via
   `WEB_DAW_SAMPLES_PAT` into `./web-daw-samples`
2. Install ffmpeg + Python deps (librosa, soundfile, pyloudnorm)
3. Run `python3 scripts/sample-audit-runner.py --batch-index N --results-dir audit-results/`
4. Commit + push `audit-results/batch-NNN.{jsonl,summary.json}` back to
   `web-daw-samples` main (with retry on concurrent matrix pushes)
5. Upload the same files as a workflow artifact (90-day retention)

## Dispatching

```bash
# Trigger batches 0-5 (first 5)
curl -X POST \
  -H "Authorization: token $ADMIN_PAT" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/admin-sonicloud/sample-audit-runner/actions/workflows/audit-matrix.yml/dispatches \
  -d '{"ref":"main","inputs":{"start-batch":"0","end-batch":"5"}}'
```

The batch plan (in `web-daw-samples/scripts/sample-batch-plan.json`) has
145 batches covering 65 libraries (~600k zones total). Default dispatch
range is 5 batches at a time. With max-parallel: 10, dispatching 20
batches at once is reasonable.

## Secrets

- `WEB_DAW_SAMPLES_PAT` — the **zmmac1** PAT with `repo` scope on
  `zulfikarbarbora-outl/web-daw-samples`. Used for checkout AND push-back.
- `GH_TOKEN` — the admin-sonicloud PAT (legacy, used by older workflow
  versions; not needed by the current workflow).
- `RESULTS_PAT` — legacy, was used to push to
  `admin-sonicloud/sample-audit-results` (no longer used; results now go
  to `web-daw-samples/audit-results/`).

## Status / monitoring

```bash
# Check run status
curl -s -H "Authorization: token $ADMIN_PAT" \
  https://api.github.com/repos/admin-sonicloud/sample-audit-runner/actions/runs?per_page=10 \
  | python3 -m json.tool

# Results live in:
# https://github.com/zulfikarbarbora-outl/web-daw-samples/tree/main/audit-results
```

## Local fallback

If GHA is unavailable or as a parallel speed-up, the audit can run
locally in a container via `scripts/local-audit-orchestrator.py`
(available in the `zmmac1/sample-audit-runner` repo, not here).
