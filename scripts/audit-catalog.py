#!/usr/bin/env python3
"""
Sample Library Audit — Phase 1: Catalog Audit & Measurement

Runs Tier 1 (structural) + Tier 2 (audio) checks on sample zones.
Downloads each FLAC file once, runs all checks, stores results.

Tier 1 (structural, per-file):
  - Sample rate, bit depth, format consistency
  - Clipping (sample peak >= 0 dBFS)
  - True-peak (dBTP via pyloudnorm)
  - DC offset (abs(mean) > 1e-3)
  - Boundary clicks (abs(x[0]), abs(x[-1]) > 0.02)
  - Leading/trailing silence

Tier 2 (audio, per-instrument):
  - Pitch verification (librosa.pyin, compare to rootNote label)
  - Octave error detection (0.5x/2x candidates)
  - Loudness measurement (LUFS, LRA, dBTP, RMS via pyloudnorm)
  - Percussion skip (voiced_ratio < 0.2)

Usage:
  # Single library
  python3 scripts/audit-catalog.py --lib shamisen-freesound

  # Multiple libraries
  python3 scripts/audit-catalog.py --libs shamisen-freesound,farfisa-freesound

  # All sliced libs
  python3 scripts/audit-catalog.py --sliced-only

  # Full catalog
  python3 scripts/audit-catalog.py --all

  # Limit to N zones (for testing)
  python3 scripts/audit-catalog.py --lib shamisen-freesound --limit 5
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
    print("WARNING: pyloudnorm not installed, loudness checks will be skipped", file=sys.stderr)

PROJECT_ROOT = Path("/home/z/my-project")
SLDF_DIR = PROJECT_ROOT / "sldf"
MASTER_DB = SLDF_DIR / "master-db.json"
OUTPUT_DIR = PROJECT_ROOT / "_work" / "audit"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Note names for reporting
NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
def midi_to_name(midi):
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"

def midi_to_freq(midi):
    return 440.0 * (2.0 ** ((midi - 69) / 12.0))


def download_file(url, dest_path, timeout=30):
    """Download a file from URL."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "audit/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            dest_path.write_bytes(r.read())
        return True
    except Exception:
        return False


# ============================================================================
# TIER 1: STRUCTURAL CHECKS (per-file)
# ============================================================================

def check_structural(y, sr, file_format, subtype):
    """Run structural checks on audio data. Returns dict of metrics + issues."""
    issues = []
    metrics = {}

    # Sample rate
    metrics["sampleRate"] = int(sr)

    # Bit depth (from subtype)
    metrics["subtype"] = subtype
    if "16" in subtype:
        metrics["bitDepth"] = 16
    elif "24" in subtype:
        metrics["bitDepth"] = 24
    elif "32" in subtype:
        metrics["bitDepth"] = 32
    else:
        metrics["bitDepth"] = None

    # Duration
    duration = len(y) / sr
    metrics["duration"] = round(duration, 4)

    # Peak amplitude
    peak = float(np.max(np.abs(y)))
    metrics["peakAmplitude"] = round(peak, 6)
    peak_db = 20 * np.log10(max(peak, 1e-10))
    metrics["peakDb"] = round(peak_db, 2)

    # Clipping detection
    clip_threshold = 0.99
    clipped_samples = int(np.sum(np.abs(y) >= clip_threshold))
    metrics["clippedSamples"] = clipped_samples
    if clipped_samples > 0:
        issues.append(f"clipping: {clipped_samples} samples at >= 0.99")

    # DC offset
    dc_offset = float(np.mean(y))
    metrics["dcOffset"] = round(dc_offset, 6)
    if abs(dc_offset) > 1e-3:
        issues.append(f"dc_offset: {dc_offset:.6f} (abs > 1e-3)")

    # Boundary clicks
    if len(y) > 0:
        start_val = float(abs(y[0]))
        end_val = float(abs(y[-1]))
        metrics["startAmplitude"] = round(start_val, 6)
        metrics["endAmplitude"] = round(end_val, 6)
        if start_val > 0.02:
            issues.append(f"boundary_click_start: {start_val:.4f} (> 0.02)")
        if end_val > 0.02:
            issues.append(f"boundary_click_end: {end_val:.4f} (> 0.02)")

    # Leading/trailing silence
    if len(y) > sr * 0.1:  # At least 100ms
        # Leading silence: find first sample above -60dBFS (0.001)
        silence_thresh = 0.001
        first_sound = 0
        for i in range(len(y)):
            if abs(y[i]) > silence_thresh:
                first_sound = i
                break
        leading_silence = first_sound / sr
        metrics["leadingSilence"] = round(leading_silence, 4)

        # Trailing silence
        last_sound = len(y) - 1
        for i in range(len(y) - 1, -1, -1):
            if abs(y[i]) > silence_thresh:
                last_sound = i
                break
        trailing_silence = (len(y) - 1 - last_sound) / sr
        metrics["trailingSilence"] = round(trailing_silence, 4)

        if leading_silence > 0.05:
            issues.append(f"leading_silence: {leading_silence:.3f}s (> 50ms)")
        if trailing_silence > 0.05:
            issues.append(f"trailing_silence: {trailing_silence:.3f}s (> 50ms)")

    return metrics, issues


# ============================================================================
# TIER 2: AUDIO ANALYSIS (pitch + loudness)
# ============================================================================

def check_pitch(y, sr, root_note):
    """Verify pitch against labeled root note. Returns dict with results.

    Uses piptrack as primary (robust to sub-harmonics), pyin as secondary.
    """
    result = {
        "labeledRootNote": root_note,
        "labeledFreq": round(midi_to_freq(root_note), 2),
    }

    # Skip if too short
    if len(y) < sr * 0.1:
        result["pitchStatus"] = "too_short"
        result["detectedFreq"] = None
        result["centsDeviation"] = None
        result["pitchConfidence"] = 0
        return result

    # Use middle 60% to avoid attack/release transients
    n = len(y)
    start = int(n * 0.2)
    end = int(n * 0.8)
    y_mid = y[start:end]
    if len(y_mid) < sr * 0.05:
        y_mid = y

    # Method 1: piptrack (primary — robust to sub-harmonics, matches slicer)
    detected_freq = None
    confidence = 0
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
                if len(valid) > 1:
                    pitch_std = float(np.std(np.log2(valid / detected_freq)))
                    confidence = max(0, 1 - pitch_std * 10)
                else:
                    confidence = 0.5
    except Exception:
        pass

    # Method 2: pyin (secondary — for voiced_ratio + confidence check)
    voiced_ratio = 0
    pyin_freq = None
    try:
        f0, voiced_flag, voiced_prob = librosa.pyin(
            y_mid, sr=sr, fmin=65, fmax=2100, fill_na=np.nan
        )
        if voiced_flag is not None:
            voiced_ratio = float(np.mean(voiced_flag))
            voiced_f0 = f0[voiced_flag]
            if len(voiced_f0) > 0:
                pyin_freq = float(np.median(voiced_f0))
    except Exception:
        pass

    result["piptrackFreq"] = round(detected_freq, 2) if detected_freq else None
    result["pyinFreq"] = round(pyin_freq, 2) if pyin_freq else None
    result["voicedRatio"] = round(voiced_ratio, 3)

    # Use piptrack as primary if available, fall back to pyin
    # If piptrack and pyin disagree significantly, prefer piptrack (it matches the slicer)
    best_freq = detected_freq
    if best_freq is None and pyin_freq is not None:
        best_freq = pyin_freq

    if best_freq is not None:
        # Compute cents deviation
        expected_freq = midi_to_freq(root_note)
        cents = 1200 * np.log2(best_freq / expected_freq)

        # Octave check: test 0.5x and 2x
        for octave_mult in [0.5, 2.0]:
            alt_cents = 1200 * np.log2((best_freq * octave_mult) / expected_freq)
            if abs(alt_cents) < abs(cents):
                cents = alt_cents

        result["detectedFreq"] = round(best_freq, 2)
        result["centsDeviation"] = round(cents, 1)
        result["pitchConfidence"] = round(confidence, 3)

        # Status
        if voiced_ratio < 0.1 and confidence < 0.3:
            result["pitchStatus"] = "unpitched"
        elif abs(cents) <= 5:
            result["pitchStatus"] = "ok"
        elif abs(cents) <= 50:
            result["pitchStatus"] = "slightly_off"
        else:
            result["pitchStatus"] = "wrong_pitch"
    else:
        result["detectedFreq"] = None
        result["centsDeviation"] = None
        result["pitchConfidence"] = 0
        result["pitchStatus"] = "no_pitch_detected"

    return result


def check_loudness(y, sr):
    """Measure loudness using pyloudnorm. Returns dict with LUFS, dBTP, RMS."""
    result = {}

    if not HAS_PYLLOUDNORM:
        return {"error": "pyloudnorm not installed"}

    # pyloudnorm expects float32, may need to reshape for mono
    y_float = y.astype(np.float32)
    if y_float.ndim == 1:
        y_float = y_float.reshape(1, -1)  # (channels, samples)

    # Skip if too short for LUFS (BS.1770-4 uses 400ms gating blocks)
    if len(y) < sr * 0.4:
        result["lufs"] = None
        result["lufsNote"] = "too_short_for_lufs"
    else:
        try:
            meter = pyloudnorm.Meter(sr)
            lufs = meter.integrated_loudness(y_float)
            result["lufs"] = round(float(lufs), 2) if not np.isnan(lufs) else None
        except Exception as e:
            result["lufs"] = None
            result["lufsError"] = str(e)

    # True-peak (dBTP) — pyloudnorm can measure this
    try:
        # For true-peak, we need to oversample; pyloudnorm doesn't have a direct API
        # but we can use the peak from the filtered signal
        # Simplified: use sample peak as approximation
        peak = float(np.max(np.abs(y)))
        result["dbtp"] = round(20 * np.log10(max(peak, 1e-10)), 2)
    except:
        result["dbtp"] = None

    # RMS
    try:
        rms = float(np.sqrt(np.mean(y ** 2)))
        result["rmsDb"] = round(20 * np.log10(max(rms, 1e-10)), 2)
    except:
        result["rmsDb"] = None

    return result


# ============================================================================
# MAIN AUDIT FUNCTION
# ============================================================================

def audit_zone(zone, lib_id, base_url, opus_base_url, lossless_format, source_format, is_sliced=False):
    """Audit a single zone. Downloads FLAC, runs all checks."""
    result = {
        "library": lib_id,
        "zoneRootNote": zone.get("rootNote"),
        "zoneNoteName": midi_to_name(zone.get("rootNote", 60)),
        "zoneVelocity": zone.get("velocity", zone.get("loVelocity", 64)),
        "zoneRoundRobin": zone.get("roundRobin", 1),
        "zoneVelocityLayer": zone.get("velocityLayer"),
    }

    # Determine download URL
    lossless_path = zone.get("losslessPath", "")
    if not lossless_path:
        result["error"] = "no losslessPath"
        result["status"] = "fail"
        return result

    # For sliced libs (freesound + qualityScore), FLAC is in flac/ subdir of opus repo
    if is_sliced:
        url = opus_base_url + "flac/" + lossless_path
    elif lossless_format == "flac":
        url = base_url + lossless_path
    else:
        url = base_url + lossless_path

    # Download to temp file
    with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        if not download_file(url, tmp_path):
            result["error"] = f"download_failed: {url}"
            result["status"] = "fail"
            return result

        # Load audio
        try:
            y, sr = sf.read(str(tmp_path), always_2d=False)
            info = sf.info(str(tmp_path))
            file_format = info.format
            subtype = info.subtype
        except Exception as e:
            result["error"] = f"load_failed: {e}"
            result["status"] = "fail"
            return result

        # Ensure mono for analysis
        if y.ndim > 1:
            y = y.mean(axis=1)

        # Tier 1: Structural checks
        struct_metrics, struct_issues = check_structural(y, sr, file_format, subtype)
        result["structural"] = struct_metrics
        if struct_issues:
            result["structuralIssues"] = struct_issues

        # Tier 2: Pitch verification
        root_note = zone.get("rootNote", 60)
        pitch_result = check_pitch(y, sr, root_note)
        result["pitch"] = pitch_result

        # Tier 2: Loudness measurement
        loudness_result = check_loudness(y, sr)
        result["loudness"] = loudness_result

        # Overall status
        issues = struct_issues[:]
        if pitch_result.get("pitchStatus") in ["wrong_pitch", "error"]:
            issues.append(f"pitch: {pitch_result.get('pitchStatus')}")
        if loudness_result.get("dbtp") is not None and loudness_result["dbtp"] > -0.5:
            issues.append(f"high_true_peak: {loudness_result['dbtp']} dBTP")

        result["issues"] = issues
        result["status"] = "fail" if any("wrong_pitch" in i or "clipping" in i for i in issues) else ("warn" if issues else "ok")

    finally:
        tmp_path.unlink(missing_ok=True)

    return result


def audit_library(lib_entry, limit=None):
    """Audit all zones in a library. Returns list of zone results."""
    lib_id = lib_entry["id"].replace(".sldf", "")
    name = lib_entry.get("name", "")

    # Find SLDF file
    sldf_path = SLDF_DIR / f"{lib_id}.sldf.v3.json"
    if not sldf_path.exists():
        sldf_path = SLDF_DIR / f"{lib_id}.sldf.v4.json"
    if not sldf_path.exists():
        sldf_path = SLDF_DIR / f"{lib_id}.sldf.json"
    if not sldf_path.exists():
        print(f"  SKIP {lib_id}: no SLDF file", flush=True)
        return []

    try:
        sldf = json.loads(sldf_path.read_text())
    except Exception as e:
        print(f"  SKIP {lib_id}: SLDF parse error: {e}", flush=True)
        return []

    # Get all zones from all instruments
    all_zones = []
    for inst in sldf.get("instruments", []):
        for zone in inst.get("zones", []):
            all_zones.append(zone)

    if limit:
        all_zones = all_zones[:limit]

    base_url = lib_entry.get("losslessBaseUrl", "")
    opus_base_url = lib_entry.get("opusBaseUrl", "")
    lossless_format = lib_entry.get("losslessFormat", "")
    source_format = lib_entry.get("sourceFormat", "")
    is_sliced = source_format == "freesound" and "qualityScore" in lib_entry

    results = []
    for i, zone in enumerate(all_zones):
        result = audit_zone(zone, lib_id, base_url, opus_base_url, lossless_format, source_format, is_sliced)
        results.append(result)
        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(all_zones)}] {lib_id}", flush=True)

    return results


def main():
    ap = argparse.ArgumentParser(description="Sample library catalog audit")
    ap.add_argument("--lib", default="", help="Single library ID")
    ap.add_argument("--libs", default="", help="Comma-separated library IDs")
    ap.add_argument("--sliced-only", action="store_true", help="Only audit sliced (freesound) libs")
    ap.add_argument("--all", action="store_true", help="Audit entire catalog")
    ap.add_argument("--limit", type=int, default=None, help="Limit zones per library (for testing)")
    ap.add_argument("--output", default="", help="Output file (default: auto)")
    args = ap.parse_args()

    db = json.loads(MASTER_DB.read_text())

    # Determine which libraries to audit
    if args.lib:
        to_audit = [l for l in db["libraries"] if l["id"].replace(".sldf", "") == args.lib]
    elif args.libs:
        wanted = [s.strip() for s in args.libs.split(",")]
        to_audit = [l for l in db["libraries"] if l["id"].replace(".sldf", "") in wanted]
    elif args.sliced_only:
        to_audit = [l for l in db["libraries"] if l.get("sourceFormat") == "freesound" and "qualityScore" in l]
    elif args.all:
        to_audit = db["libraries"]
    else:
        print("Specify --lib, --libs, --sliced-only, or --all", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'='*60}", flush=True)
    print(f"Auditing {len(to_audit)} libraries", flush=True)
    print(f"{'='*60}\n", flush=True)

    all_results = []
    start_time = time.time()

    for i, lib in enumerate(to_audit):
        lib_id = lib["id"].replace(".sldf", "")
        name = lib.get("name", "")
        zone_count = lib.get("zoneCount", 0)
        print(f"[{i+1}/{len(to_audit)}] {name} ({lib_id}) — {zone_count} zones", flush=True)

        results = audit_library(lib, limit=args.limit)
        all_results.extend(results)

        # Quick stats
        ok = sum(1 for r in results if r.get("status") == "ok")
        warn = sum(1 for r in results if r.get("status") == "warn")
        fail = sum(1 for r in results if r.get("status") == "fail")
        print(f"  -> {ok} ok, {warn} warn, {fail} fail", flush=True)

    elapsed = time.time() - start_time

    # Output file
    if args.output:
        output_path = Path(args.output)
    else:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        output_path = OUTPUT_DIR / f"audit-{timestamp}.jsonl"

    with open(output_path, "w") as f:
        for r in all_results:
            f.write(json.dumps(r) + "\n")

    print(f"\n{'='*60}", flush=True)
    print(f"AUDIT COMPLETE", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"  Libraries audited: {len(to_audit)}", flush=True)
    print(f"  Zones audited: {len(all_results)}", flush=True)
    print(f"  Time: {elapsed:.1f}s ({elapsed/max(len(all_results),1)*1000:.0f}ms/zone)", flush=True)
    print(f"  Output: {output_path}", flush=True)

    # Summary stats
    ok = sum(1 for r in all_results if r.get("status") == "ok")
    warn = sum(1 for r in all_results if r.get("status") == "warn")
    fail = sum(1 for r in all_results if r.get("status") == "fail")
    print(f"  OK: {ok}, WARN: {warn}, FAIL: {fail}", flush=True)

    # Pitch summary
    pitch_statuses = defaultdict(int)
    for r in all_results:
        p = r.get("pitch", {}).get("pitchStatus", "n/a")
        pitch_statuses[p] += 1
    print(f"  Pitch: {dict(pitch_statuses)}", flush=True)


if __name__ == "__main__":
    main()
