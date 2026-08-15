#!/usr/bin/env python3
"""Collect audit results from GHA artifacts into a single report."""
import json
import subprocess
import sys
from pathlib import Path
from collections import defaultdict

def main():
    results_dir = Path("collected-results")
    results_dir.mkdir(exist_ok=True)
    
    # Download all artifacts using gh CLI
    print("Downloading artifacts...", flush=True)
    r = subprocess.run(
        ["gh", "run", "download", "--repo", "admin-sonicloud/sample-audit-runner",
         "--dir", str(results_dir)],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        print(f"gh download failed: {r.stderr}", file=sys.stderr)
        sys.exit(1)
    
    # Merge all JSONL files
    all_results = []
    summaries = []
    
    for jsonl_file in sorted(results_dir.rglob("batch-*.jsonl")):
        print(f"  Merging {jsonl_file.name}...", flush=True)
        with open(jsonl_file) as f:
            for line in f:
                all_results.append(json.loads(line))
    
    for summary_file in sorted(results_dir.rglob("batch-*-summary.json")):
        summaries.append(json.loads(summary_file.read_text()))
    
    # Write merged output
    output_file = Path("audit-results-all.jsonl")
    with open(output_file, "w") as f:
        for r in all_results:
            f.write(json.dumps(r) + "\n")
    
    print(f"\nTotal zones audited: {len(all_results)}")
    print(f"Merged results: {output_file}")
    
    # Summary
    ok = sum(1 for r in all_results if r.get("status") == "ok")
    warn = sum(1 for r in all_results if r.get("status") == "warn")
    fail = sum(1 for r in all_results if r.get("status") == "fail")
    print(f"OK: {ok}, WARN: {warn}, FAIL: {fail}")
    
    # Write summary report
    report = {
        "totalZones": len(all_results),
        "ok": ok,
        "warn": warn,
        "fail": fail,
        "batches": summaries,
    }
    Path("audit-summary.json").write_text(json.dumps(report, indent=2))
    print(f"Summary: audit-summary.json")

if __name__ == "__main__":
    main()
