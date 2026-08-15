# Sample Audit Runner

GHA-based parallel audit system for the web-daw-samples catalog.

## How it works

1. `scripts/batch-plan.json` defines 65 batches (each ~5000 zones, or 1 big library)
2. `.github/workflows/audit-matrix.yml` runs batches in parallel (up to 10 concurrent)
3. Each job downloads SLDF files from public `zulfikarbarbora-outl/web-daw-samples`, audits zones, uploads results as artifacts
4. Results collected locally via `scripts/collect-results.py`

## Running

### Pilot (5 batches, ~25k zones)
Triggered automatically on push to main, or manually:
```bash
gh workflow run audit-matrix.yml -f start-batch=0 -f end-batch=5
```

### Full catalog (65 batches, ~600k zones)
```bash
gh workflow run audit-matrix.yml -f start-batch=0 -f end-batch=65
```

### Collect results
```bash
python3 scripts/collect-results.py
```

## Audit checks

Each zone gets:
- **Structural**: sample rate, bit depth, clipping, DC offset, boundary clicks, silence
- **Pitch**: piptrack (primary) + pyin (secondary), octave correction, ±5 cents tolerance
- **Loudness**: LUFS (pyloudnorm), true-peak (dBTP), RMS

Status: `ok` / `warn` / `fail` based on issues found.
