#!/usr/bin/env python3
"""
Standalone audit runner for GHA / Netlify batch processing.

Downloads SLDF + master-db from the public web-daw-samples repo,
audits the assigned batch of libraries, and writes results as JSONL.

Usage:
  python3 scripts/audit-runner.py --batch-index 0
  python3 scripts/audit-runner.py --batch-index 0 --results-dir results/
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import tempfile
from pathlib import Path
from collections import defaultdict

import numpy as np
import soundfile as sf
import librosa

try:
    import pyloudnorm
    HAS_PYLLOUDNORM = True
except ImportError:
    HAS_PYLLOUDNORM = False

# CONFIG
OWNER = "zulfikarbarbora-outl"
REPO = "web-daw-samples"
BRANCH = "main"
RAW_BASE = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{BRANCH}/"
BATCH_PLAN_URL = "https://raw.githubusercontent.com/admin-sonicloud/sample-audit-runner/main/scripts/batch-plan.json"

NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
def midi_to_name(midi):
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"

def midi_to_freq(midi):
    return 440.0 * (2.0 ** ((midi - 69) / 12.0))

def download_json(url):
    headers = {"User-Agent": "audit-runner/1.0"}
    token = os.environ.get("GH_TOKEN", "")
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def download_file(url, dest_path, timeout=60):
    headers = {"User-Agent": "audit-runner/1.0"}
    token = os.environ.get("GH_TOKEN", "")
    if token:
        headers["Authorization"] = f"token {token}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            dest_path.write_bytes(r.read())
        return True
    except:
        return False

# AUDIO CHECKS
def check_structural(y, sr, subtype):
    issues = []
    metrics = {"sampleRate": int(sr), "subtype": subtype}
    if "16" in subtype: metrics["bitDepth"] = 16
    elif "24" in subtype: metrics["bitDepth"] = 24
    elif "32" in subtype: metrics["bitDepth"] = 32
    else: metrics["bitDepth"] = None
    duration = len(y) / sr
    metrics["duration"] = round(duration, 4)
    peak = float(np.max(np.abs(y)))
    metrics["peakAmplitude"] = round(peak, 6)
    metrics["peakDb"] = round(20 * np.log10(max(peak, 1e-10)), 2)
    clipped = int(np.sum(np.abs(y) >= 0.99))
    metrics["clippedSamples"] = clipped
    if clipped > 0: issues.append(f"clipping: {clipped} samples")
    dc = float(np.mean(y))
    metrics["dcOffset"] = round(dc, 6)
    if abs(dc) > 1e-3: issues.append(f"dc_offset: {dc:.6f}")
    if len(y) > 0:
        s, e = float(abs(y[0])), float(abs(y[-1]))
        metrics["startAmplitude"] = round(s, 6)
        metrics["endAmplitude"] = round(e, 6)
        if s > 0.02: issues.append(f"boundary_click_start: {s:.4f}")
        if e > 0.02: issues.append(f"boundary_click_end: {e:.4f}")
    if len(y) > sr * 0.1:
        st = 0.001
        fs = next((i for i in range(len(y)) if abs(y[i]) > st), 0)
        metrics["leadingSilence"] = round(fs / sr, 4)
        ls = next((i for i in range(len(y)-1, -1, -1) if abs(y[i]) > st), len(y)-1)
        metrics["trailingSilence"] = round((len(y)-1-ls) / sr, 4)
        if fs/sr > 0.05: issues.append(f"leading_silence: {fs/sr:.3f}s")
        if (len(y)-1-ls)/sr > 0.05: issues.append(f"trailing_silence: {(len(y)-1-ls)/sr:.3f}s")
    return metrics, issues

def check_pitch(y, sr, root_note):
    result = {"labeledRootNote": root_note, "labeledFreq": round(midi_to_freq(root_note), 2)}
    if len(y) < sr * 0.1:
        result.update({"pitchStatus": "too_short", "detectedFreq": None, "centsDeviation": None, "pitchConfidence": 0})
        return result
    n = len(y)
    y_mid = y[int(n*0.2):int(n*0.8)]
    if len(y_mid) < sr * 0.05: y_mid = y
    
    # piptrack
    detected_freq, confidence = None, 0
    try:
        pitches, mags = librosa.piptrack(y=y_mid, sr=sr, threshold=0.5)
        if mags.size > 0:
            max_idx = mags.argmax(axis=0)
            max_mags = mags[max_idx, np.arange(mags.shape[1])]
            sig = max_mags > np.max(max_mags) * 0.3
            valid = pitches[max_idx[sig], np.where(sig)[0]]
            valid = valid[valid > 0]
            if len(valid) > 0:
                detected_freq = float(np.median(valid))
                confidence = max(0, 1 - float(np.std(np.log2(valid / detected_freq))) * 10) if len(valid) > 1 else 0.5
    except: pass
    
    # pyin
    voiced_ratio, pyin_freq = 0, None
    try:
        f0, vf, vp = librosa.pyin(y_mid, sr=sr, fmin=65, fmax=2100, fill_na=np.nan)
        if vf is not None:
            voiced_ratio = float(np.mean(vf))
            vf0 = f0[vf]
            if len(vf0) > 0: pyin_freq = float(np.median(vf0))
    except: pass
    
    result["piptrackFreq"] = round(detected_freq, 2) if detected_freq else None
    result["pyinFreq"] = round(pyin_freq, 2) if pyin_freq else None
    result["voicedRatio"] = round(voiced_ratio, 3)
    
    best = detected_freq or pyin_freq
    if best:
        exp = midi_to_freq(root_note)
        cents = 1200 * np.log2(best / exp)
        for om in [0.5, 2.0]:
            ac = 1200 * np.log2((best * om) / exp)
            if abs(ac) < abs(cents): cents = ac
        result["detectedFreq"] = round(best, 2)
        result["centsDeviation"] = round(cents, 1)
        result["pitchConfidence"] = round(confidence, 3)
        if voiced_ratio < 0.1 and confidence < 0.3: result["pitchStatus"] = "unpitched"
        elif abs(cents) <= 5: result["pitchStatus"] = "ok"
        elif abs(cents) <= 50: result["pitchStatus"] = "slightly_off"
        else: result["pitchStatus"] = "wrong_pitch"
    else:
        result.update({"detectedFreq": None, "centsDeviation": None, "pitchConfidence": 0, "pitchStatus": "no_pitch_detected"})
    return result

def check_loudness(y, sr):
    if not HAS_PYLLOUDNORM: return {"error": "no pyloudnorm"}
    yf = y.astype(np.float32)
    if yf.ndim == 1: yf = yf.reshape(1, -1)
    result = {}
    if len(y) < sr * 0.4:
        result["lufs"] = None
        result["lufsNote"] = "too_short"
    else:
        try:
            meter = pyloudnorm.Meter(sr)
            lufs = meter.integrated_loudness(yf)
            result["lufs"] = round(float(lufs), 2) if not np.isnan(lufs) else None
        except: result["lufs"] = None
    peak = float(np.max(np.abs(y)))
    result["dbtp"] = round(20 * np.log10(max(peak, 1e-10)), 2)
    rms = float(np.sqrt(np.mean(y ** 2)))
    result["rmsDb"] = round(20 * np.log10(max(rms, 1e-10)), 2)
    return result

def audit_zone(zone, lib_id, base_url, opus_base_url, is_sliced=False):
    result = {
        "library": lib_id,
        "zoneRootNote": zone.get("rootNote"),
        "zoneNoteName": midi_to_name(zone.get("rootNote", 60)),
        "zoneVelocity": zone.get("velocity", zone.get("loVelocity", 64)),
        "zoneRoundRobin": zone.get("roundRobin", 1),
    }
    lp = zone.get("losslessPath", "")
    if not lp:
        result["error"] = "no losslessPath"
        result["status"] = "fail"
        return result
    url = (opus_base_url + "flac/" + lp) if is_sliced else (base_url + lp)
    
    with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as tmp:
        tp = Path(tmp.name)
    try:
        if not download_file(url, tp):
            result["error"] = "download_failed"
            result["status"] = "fail"
            return result
        try:
            y, sr = sf.read(str(tp), always_2d=False)
            subtype = sf.info(str(tp)).subtype
        except Exception as e:
            result["error"] = f"load_failed"
            result["status"] = "fail"
            return result
        if y.ndim > 1: y = y.mean(axis=1)
        
        sm, si = check_structural(y, sr, subtype)
        result["structural"] = sm
        if si: result["structuralIssues"] = si
        result["pitch"] = check_pitch(y, sr, zone.get("rootNote", 60))
        result["loudness"] = check_loudness(y, sr)
        
        issues = si[:]
        ps = result["pitch"].get("pitchStatus")
        if ps in ["wrong_pitch", "error"]: issues.append(f"pitch: {ps}")
        dbtp = result["loudness"].get("dbtp")
        if dbtp is not None and dbtp > -0.5: issues.append(f"high_true_peak: {dbtp}")
        result["issues"] = issues
        result["status"] = "fail" if any("wrong_pitch" in i or "clipping" in i for i in issues) else ("warn" if issues else "ok")
    finally:
        tp.unlink(missing_ok=True)
    return result

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-index", type=int, required=True)
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    print(f"Downloading batch plan...", flush=True)
    batches = download_json(BATCH_PLAN_URL)
    if args.batch_index >= len(batches):
        print(f"ERROR: batch-index out of range", file=sys.stderr)
        sys.exit(1)
    batch = batches[args.batch_index]
    print(f"Batch {args.batch_index}: {len(batch)} libraries: {batch[:5]}...", flush=True)

    print(f"Downloading master-db...", flush=True)
    db = download_json(RAW_BASE + "sldf/master-db.json")
    lib_map = {l["id"].replace(".sldf", ""): l for l in db["libraries"]}

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    output_file = results_dir / f"batch-{args.batch_index:03d}.jsonl"

    all_results = []
    start = time.time()

    for i, lib_id in enumerate(batch):
        lib = lib_map.get(lib_id)
        if not lib:
            print(f"  SKIP {lib_id}: not in master-db", flush=True)
            continue
        zc = lib.get("zoneCount", 0)
        print(f"[{i+1}/{len(batch)}] {lib_id} - {zc} zones", flush=True)

        # Download SLDF
        for ext in [".sldf.v3.json", ".sldf.v4.json", ".sldf.json"]:
            try:
                sldf = download_json(RAW_BASE + f"sldf/{lib_id}{ext}")
                break
            except:
                continue
        else:
            print(f"  SKIP {lib_id}: SLDF download failed", flush=True)
            continue

        zones = [z for inst in sldf.get("instruments", []) for z in inst.get("zones", [])]
        if args.limit: zones = zones[:args.limit]

        base_url = lib.get("losslessBaseUrl", "")
        opus_base_url = lib.get("opusBaseUrl", "")
        is_sliced = lib.get("sourceFormat") == "freesound" and "qualityScore" in lib

        for j, zone in enumerate(zones):
            r = audit_zone(zone, lib_id, base_url, opus_base_url, is_sliced)
            all_results.append(r)
            if (j + 1) % 50 == 0:
                el = time.time() - start
                rate = (j + 1) / el
                print(f"  [{j+1}/{len(zones)}] {lib_id} | {rate:.1f}/s | ETA {int((len(zones)-j-1)/rate)}s", flush=True)

        # Incremental save
        with open(output_file, "w") as f:
            for r in all_results:
                f.write(json.dumps(r) + "\n")

    el = time.time() - start
    ok = sum(1 for r in all_results if r.get("status") == "ok")
    warn = sum(1 for r in all_results if r.get("status") == "warn")
    fail = sum(1 for r in all_results if r.get("status") == "fail")

    print(f"\nBATCH {args.batch_index} COMPLETE", flush=True)
    print(f"  Zones: {len(all_results)} | Time: {el:.1f}s | OK: {ok} WARN: {warn} FAIL: {fail}", flush=True)
    print(f"  Output: {output_file}", flush=True)

    # Write summary
    summary = {"batchIndex": args.batch_index, "libraries": batch, "totalZones": len(all_results),
               "ok": ok, "warn": warn, "fail": fail, "elapsed": round(el, 1)}
    (results_dir / f"batch-{args.batch_index:03d}-summary.json").write_text(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
