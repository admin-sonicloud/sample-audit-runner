# PAT Separation — Critical Documentation

**Read this FIRST before any GHA or git operations.**

## The two PATs and what each is for

| PAT prefix | Owner account | Use for | Reason |
|---|---|---|---|
| `<PAT_FROM_ENV_OR_SECRET_MANAGER>` | `admin-sonicloud` | **GHA dispatch only** on `admin-sonicloud/*` repos | This account has working GHA billing — workflows actually start |
| `<PAT_FROM_ENV_OR_SECRET_MANAGER>` | `zmmac1` | **Git access** to `zulfikarbarbora-outl/web-daw-samples` (private) | `zmmac1` has write access to web-daw-samples but its GHA is billing-locked |

## Why this split exists

- `zulfikarbarbora-outl/web-daw-samples` is the **primary sample-data repo** (private, 500 MB, 275 libraries, ~600k zones). The audit script, batch plan, manifests, and results all live here.
- `zmmac1` has `write` permission on `web-daw-samples` but its GitHub Actions are **billing-locked** — every workflow dispatched by `zmmac1` fails with *"account is locked due to a billing issue"*.
- `admin-sonicloud` has working GHA billing and a working `sample-audit-runner` repo from the previous session. So: dispatch GHA on `admin-sonicloud/sample-audit-runner`, but use the `zmmac1` PAT inside the workflow to checkout and push back to `web-daw-samples`.

## Concrete setup

### Repo: `admin-sonicloud/sample-audit-runner` (GHA runner, public)

- Workflow file: `.github/workflows/sample-audit-matrix.yml`
- Secrets:
  - `WEB_DAW_SAMPLES_PAT` = the **zmmac1** PAT (used by `actions/checkout` to clone `web-daw-samples` and by `git push` to push results back)
  - (existing) `GH_TOKEN` = the admin-sonicloud PAT (used for git operations on this runner repo itself — not actually needed for the audit)
- Workflow behavior:
  1. Checkout `zulfikarbarbora-outl/web-daw-samples` via `WEB_DAW_SAMPLES_PAT` into `./web-daw-samples`
  2. Install ffmpeg + Python deps (librosa, soundfile, pyloudnorm)
  3. Run `python3 scripts/sample-audit-runner.py --batch-index N --results-dir audit-results/`
  4. Commit + push `audit-results/batch-NNN.{jsonl,summary.json}` back to `web-daw-samples` main via `WEB_DAW_SAMPLES_PAT` (with retry on concurrent matrix pushes)
  5. Upload same files as artifact (90-day retention)

### Repo: `zulfikarbarbora-outl/web-daw-samples` (data repo, private)

- Audit script: `scripts/sample-audit-runner.py`
- Batch plan: `scripts/sample-batch-plan.json` (145 batches, ~600k zones total)
- Requirements: `scripts/sample-audit-requirements.txt`
- Results: `audit-results/batch-NNN.{jsonl,summary.json}` (committed and pushed by the GHA workflow above)
- Note: this repo also has `.github/workflows/sample-audit-matrix.yml` but it CANNOT run (zmmac1 billing lock). Kept for documentation only.

### Local fallback (this container)

If GHA is unavailable (or as a parallel speed-up), `scripts/local-audit-orchestrator.py` runs 4 workers locally as a double-forked daemon. Reads manifests from a local clone of `web-daw-samples`, downloads opus previews from public `adc-*` repos, runs the same audit script, commits + pushes results back to `web-daw-samples` main.

## Dispatching GHA (the right way)

```bash
# Trigger batches 0-5 on admin-sonicloud/sample-audit-runner
curl -X POST \
  -H "Authorization: token <PAT_FROM_ENV_OR_SECRET_MANAGER>" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/admin-sonicloud/sample-audit-runner/actions/workflows/sample-audit-matrix.yml/dispatches \
  -d '{"ref":"main","inputs":{"start-batch":"0","end-batch":"5"}}'
```

**Do NOT** dispatch on `zulfikarbarbora-outl/web-daw-samples` — its GHA is billing-locked and will fail with `startup_failure`.

## Pushing code changes to `web-daw-samples`

```bash
cd /home/z/my-project/audit-work/web-daw-samples
git push "https://<PAT_FROM_ENV_OR_SECRET_MANAGER>@github.com/zulfikarbarbora-outl/web-daw-samples.git" main
```

**Do NOT** use the admin-sonicloud PAT for web-daw-samples git operations — it doesn't have access.

## Pushing code changes to `admin-sonicloud/sample-audit-runner`

```bash
cd /home/z/my-project/audit-work/sample-audit-runner
git push "https://<PAT_FROM_ENV_OR_SECRET_MANAGER>@github.com/admin-sonicloud/sample-audit-runner.git" main
```

## Monitoring

```bash
# GHA runs on admin-sonicloud/sample-audit-runner
curl -s -H "Authorization: token <PAT_FROM_ENV_OR_SECRET_MANAGER>" \
  https://api.github.com/repos/admin-sonicloud/sample-audit-runner/actions/runs?per_page=5 \
  | python3 -m json.tool

# Local daemon
tail -f /home/z/my-project/audit-work/audit-orchestrator.log

# Results in web-daw-samples
curl -s -H "Authorization: token <PAT_FROM_ENV_OR_SECRET_MANAGER>" \
  https://api.github.com/repos/zulfikarbarbora-outl/web-daw-samples/contents/audit-results
```
