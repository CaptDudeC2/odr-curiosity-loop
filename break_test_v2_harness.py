#!/usr/bin/env python3
"""Curiosity-loop break-test harness v2 — re-run against the PATCHED loop
(2026-10-13 patch: Tier 0 freshness gate, z>=4 relevance bypass, delta
observe). Sections A/C numeric replays are verbatim from v1. observe() /
observe_delta() tests run against a temp RUNS_DIR — the real runs
directory is never written.
"""
import importlib.util
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

spec = importlib.util.spec_from_file_location(
    "curiosity_loop", os.path.join(os.path.dirname(os.path.abspath(__file__)), "curiosity_loop.py"))
cl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cl)

MANIFEST = cl.load_manifest()
Z_THRESH = MANIFEST["gates"]["numeric_z_threshold"]
NY = ZoneInfo("America/New_York")

def fresh_stamp():
    return datetime.now(timezone.utc).astimezone(NY).strftime("%b %d %Y %I:%M %p")

results = []

def record(case, verdict, expected, pf, note="", **kw):
    results.append(dict(case=case, verdict=verdict, expected=expected, pf=pf, note=note, **kw))
    print(f"  [{pf}] {case}: verdict={verdict} expected={expected} — {note}")

def run_numeric(case, name, observed, expected, sigma, relevance, expected_verdict, note=""):
    z = cl.numeric_delta(observed, expected, sigma)
    v = cl.gate_numeric(z, relevance, MANIFEST)
    got = "FIRES" if v["fires"] else "QUIET"
    record(case, got, expected_verdict, "PASS" if got == expected_verdict else "FAIL",
           f"{note} | z={z:.2f} rel={relevance} tier={v['tier']} bypass={v.get('relevance_bypass')}")
    return v

print(f"manifest v{MANIFEST.get('version')}: numeric z > {Z_THRESH}, relevance floor = {cl.RELEVANCE_FLOOR}, "
      f"bypass z >= {cl.RELEVANCE_BYPASS_Z}")
print("freshness tolerances:")
for o in MANIFEST["objectives"]:
    t = o.get("tolerances") or {}
    if t.get("requires_freshness"):
        print(f"  {o['id']}: requires_freshness=True max_age_hours={t.get('max_age_hours')}")

# ---------------------------------------------------------------- sanity
HIST = [
    ("1",  "ro_queue_count", 35, 17, 3, 6.0, 0.35, True),
    ("2",  "ro_queue_count", 20, 6, 2, 7.0, 0.35, True),
    ("3",  "cp_ro_count", 13, 8, 2, 2.5, 0.35, True),
    ("4",  "cp_ro_count_20261008", 16, 8, 2, 4.0, 0.315, True),
    ("5",  "mtd_ro_total", 168, 158, 2, 5.0, 0.35, True),
    ("6",  "cp_mech_ro_mtd", 40, 35, 2, 2.5, 0.35, True),
    ("7",  "cp_ro_count", 8, 13, 2, 2.5, 0.315, True),
    ("8",  "g5_ro_count", 133, 98, 15, 2.333, 0.315, True),
    ("8b", "g5_ro_count", 90, 49, 12, 3.417, 0.315, True),
    ("9",  "cp_ro_count_20261008", 0, 8, 2, 4.0, 0.35, True),
    ("10", "cp_ro_count_friday", 0, 12, 4, 3.0, 0.35, True),
]
print("\n== SANITY: recomputed z vs run-log z (patched code) ==")
sanity_ok = True
for case, name, obs, exp, sig, lz, lrel, lfires in HIST:
    z = cl.numeric_delta(obs, exp, sig)
    v = cl.gate_numeric(z, lrel, MANIFEST)
    ok = abs(z - lz) < 0.01 and v["fires"] == lfires
    sanity_ok &= ok
    print(f"  case {case}: z={z:.3f} vs logged {lz}; fires={v['fires']} vs logged {lfires}  {'OK' if ok else 'MISMATCH'}")
record("SANITY", "11/11 match" if sanity_ok else "MISMATCH", "11/11 match",
       "PASS" if sanity_ok else "FAIL", "patched gate_numeric reproduces all logged decisions")

# ---------------------------------------------------------------- A
print("\n== A) GROUP B REPLAYS — priors exactly as logged live (expect FALSE fires) ==")
for case, name, obs, exp, sig, lz, lrel, lfires in HIST[:8]:
    run_numeric(f"A{case}", name, obs, exp, sig, lrel, "FIRES", "live false fire reproduced")
run_numeric("A8b", "g5_ro_count", 90, 49, 12, 0.315, "FIRES", "G5 Oct 7 cumulative level")

# ---------------------------------------------------------------- B (levels / single range model)
print("\n== B) CALIBRATED — single range model for CP (from patched leg_expectations.json) ==")
expf = cl.load_leg_expectations()
cp_exp = next(x for x in expf["legs"]["daily-ro-audit"]["expectations"] if x["key"] == "cp_ro_count")
CP_EXP, CP_SIG = cp_exp["expected"], cp_exp["sigma"]
print(f"  leg_expectations cp_ro_count: model={cp_exp['model']} range={cp_exp['range']} expected={CP_EXP} sigma={CP_SIG}")
run_numeric("B1", "ro_queue_count", 35, 17, 10, 0.35, "QUIET", "anchor 17 (10-02 state); sigma 3 -> 10 (churn)")
run_numeric("B2", "ro_queue_count", 20, 35, 10, 0.35, "QUIET", "expected = last verified state 35; sigma 10")
run_numeric("B3", "cp_ro_count", 13, CP_EXP, CP_SIG, 0.35, "QUIET", "SINGLE range model (empirical median/robust sigma)")
run_numeric("B4", "cp_ro_count_20261008", 16, CP_EXP, CP_SIG, 0.315, "QUIET",
            "Thursday 16 under the SAME single model — no separate weekday prior exists anymore")
run_numeric("B7", "cp_ro_count", 8, CP_EXP, CP_SIG, 0.315, "QUIET",
            "Friday 8 under the single model. B7x is RETIRED: the contradictory weekday model is gone, "
            "so there is no second model left to disagree")
run_numeric("B5", "mtd_ro_total", 168, 158, 12, 0.35, "QUIET", "level replay at staged sigma 12 (record only; operative path is B5d delta)")
run_numeric("B6", "cp_mech_ro_mtd", 40, 35, 12, 0.35, "QUIET", "level replay at staged sigma 12 (metric not converted; out of patch scope)")
run_numeric("B8-L", "g5_ro_count", 133, 98, 12, 0.315, "FIRES",
            "LEVEL mode still fires — expected and honest: sigma cannot fix a level anchor. Superseded by B8d delta mode")
run_numeric("B8b-L", "g5_ro_count", 90, 49, 12, 0.315, "FIRES",
            "LEVEL mode still fires (as it did live). Superseded by B8bd delta mode")

# ---------------------------------------------------------------- B (delta mode)
print("\n== B-delta) CUMULATIVE METRICS via observe_delta (operative calibrated path) ==")
REAL_RUNS = cl.RUNS_DIR

def seed_level(tmpdir, job, run_date, name, level):
    path = os.path.join(tmpdir, f"{job}-{run_date}.jsonl")
    rec = {"ts": f"{run_date}T12:00:00+00:00", "job_id": job, "run_date": run_date,
           "type": "observed",
           "payload": {"name": name, "observed": level, "expected": 0, "sigma": 1,
                       "context": "seeded prior level (harness)", "evidence": "harness seed", "source": "harness"}}
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")

def delta_case(case, metric, prior_level, prior_date, today_level, today_date,
               d_exp, d_sig, expected_verdict, note):
    tmp = tempfile.mkdtemp(prefix="cl-delta-")
    try:
        cl.RUNS_DIR = tmp
        # 2026-10-16: seeds are written under the CALLER's job — delta
        # baselines are scoped per (job, metric) after the
        # DELTA_BASELINE_CROSS_LEG_COLLISION fix, so a prior logged
        # under a different job is (correctly) invisible here, exactly
        # as in production where a leg's prior is its own last run.
        seed_level(tmp, "harness", prior_date, metric, prior_level)
        rl = cl.RunLog("harness", today_date)
        v = cl.observe_delta(rl, MANIFEST, metric, today_level, d_exp, d_sig,
                             context="harness delta replay", evidence="harness", source="harness",
                             data_timestamp=fresh_stamp())
        got = "FIRES" if v["fires"] else "QUIET"
        obs_ev = [r for r in rl.read_all() if r["type"] == "observed"]
        inc = obs_ev[-1]["payload"]["observed"] if obs_ev else None
        record(case, got, expected_verdict, "PASS" if got == expected_verdict else "FAIL",
               f"{note} | increment={inc} vs baseline {d_exp}±{d_sig} -> {v['reason'][:90]}")
    finally:
        cl.RUNS_DIR = REAL_RUNS
        shutil.rmtree(tmp, ignore_errors=True)

# delta baselines — provenance: g5 increments documented {+41, +43} -> median 42;
# mtd daily adds documented {+10, +25} -> median 17.5; sigma 12 = staged MTD correction.
delta_case("B5d", "mtd_ro_total", 158, "2026-10-09", 168, "2026-10-10", 17.5, 12, "QUIET",
           "MTD total as daily increment (the A5 incident, delta mode)")
delta_case("B8d", "g5_ro_count", 90, "2026-10-07", 133, "2026-10-08", 42, 12, "QUIET",
           "G5 as daily increment (the B8 incident, delta mode)")
delta_case("B8bd", "g5_ro_count", 49, "2026-10-06", 90, "2026-10-07", 42, 12, "QUIET",
           "G5 as daily increment (the B8b incident, delta mode)")

# fallback behavior
print("\n== B-fallback) observe_delta with no prior ==")
tmp = tempfile.mkdtemp(prefix="cl-fb-")
try:
    cl.RUNS_DIR = tmp
    rl1 = cl.RunLog("harness", "2026-10-10")
    v1 = cl.observe_delta(rl1, MANIFEST, "g5_ro_count", 100, 42, 12,
                          context="harness fallback", evidence="harness", source="harness")
    fb = [r for r in rl1.read_all() if r["type"] == "delta_fallback"]
    record("F1-seed", "FIRES" if v1["fires"] else "QUIET", "QUIET",
           "PASS" if (not v1["fires"] and fb) else "FAIL",
           f"no prior -> delta_fallback logged={bool(fb)}, verdict reason: {v1['reason'][:80]}")
    rl2 = cl.RunLog("harness", "2026-10-11")
    v2 = cl.observe_delta(rl2, MANIFEST, "g5_ro_count", 143, 42, 12,
                          context="harness fallback day2", evidence="harness", source="harness",
                          data_timestamp=fresh_stamp())
    obs2 = [r for r in rl2.read_all() if r["type"] == "observed"]
    inc2 = obs2[-1]["payload"]["observed"] if obs2 else None
    record("F2-prior", "FIRES" if v2["fires"] else "QUIET", "QUIET",
           "PASS" if inc2 == 43 else "FAIL",
           f"day-2 call found day-1 seed as prior; increment={inc2} (expect 43)")
    rl3 = cl.RunLog("harness", "2026-10-12")
    v3 = cl.observe_delta(rl3, MANIFEST, "g5_ro_count", 153, 42, 12,
                          context="harness fallback day3", evidence="harness", source="harness",
                          data_timestamp=fresh_stamp())
    obs3 = [r for r in rl3.read_all() if r["type"] == "observed"]
    inc3 = obs3[-1]["payload"]["observed"] if obs3 else None
    record("F3-chain", "FIRES" if v3["fires"] else "QUIET", "any (chain is the test)",
           "PASS" if inc3 == 10 else "FAIL",
           f"day-3 prior read from day-2 DELTA event's level_observed; increment={inc3} (expect 10); "
           f"increment math is the test here; the gate verdict itself was QUIET only because the "
           f"harness context scores relevance ~0.05 (< floor) at z=2.67, below the z>=4 bypass")
finally:
    cl.RUNS_DIR = REAL_RUNS
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- C controls (numeric)
print("\n== C) TRUE-POSITIVE CONTROLS under CALIBRATED priors (expect FIRES) ==")
run_numeric("C9a", "cp_ro_count_20261008", 0, 8, 2, 0.35, "FIRES", "case 9 as logged")
run_numeric("C9b", "cp_ro_count_20261008", 0, 14, 2, 0.35, "FIRES", "case 9 under old Thursday prior")
run_numeric("C9c", "cp_ro_count_20261008", 0, CP_EXP, CP_SIG, 0.35, "FIRES",
            "case 9 under the NEW single range model (median 11, robust sigma 4.45)")
run_numeric("C10a", "cp_ro_count_friday", 0, 12, 4, 0.35, "FIRES", "case 10 as logged")
run_numeric("C10b", "cp_ro_count_friday", 0, CP_EXP, CP_SIG, 0.35, "FIRES",
            "case 10 under the NEW single range model")

# ---------------------------------------------------------------- freshness via observe()
print("\n== C/D-fresh) FRESHNESS GATE via observe() end-to-end (temp runs dir) ==")
SR_CONTEXT = "Daily tracking report sheet write verification advisor metrics"
AUDIT_CONTEXT = "Advisor Performance, presort Daily RO Audit, Pay Type Closed Date"

def prose(dt_utc):
    return dt_utc.astimezone(NY).strftime("%b %d %Y %I:%M %p")

now = datetime.now(timezone.utc)
FRESH = prose(now)
STALE25 = prose(now - timedelta(hours=25))

def observe_case(case, name, obs, exp, sig, context, stamp, expect_fires, expect_in_reason, note):
    tmp = tempfile.mkdtemp(prefix="cl-obs-")
    try:
        cl.RUNS_DIR = tmp
        rl = cl.RunLog("harness", now.date().isoformat())
        v = cl.observe(rl, MANIFEST, name, obs, exp, sig, context=context,
                       evidence="harness", source="harness", data_timestamp=stamp)
        got = "FIRES" if v["fires"] else "QUIET"
        ok = v["fires"] == expect_fires and (expect_in_reason in v["reason"])
        record(case, got, "FIRES" if expect_fires else "QUIET", "PASS" if ok else "FAIL",
               f"{note} | reason: {v['reason'][:110]}")
        return v
    finally:
        cl.RUNS_DIR = REAL_RUNS
        shutil.rmtree(tmp, ignore_errors=True)

observe_case("C9f", "cp_ro_count_20261008", 0, 8, 2, SR_CONTEXT, "Oct 8 2026 5:07 AM",
             True, "STALE_DATA_BREACH", "stale-zero incident replayed WITH its real stale stamp (Oct 8 5:07 AM)")
observe_case("D11", "cp_ro_count", 0, 14, 2, AUDIT_CONTEXT, FRESH,
             True, "INVESTIGATE", "true zero, busy day, FRESH stamp -> fires NUMERICALLY (no STALE in reason)")
observe_case("D12", "cp_ro_count", 12, 12, 2, SR_CONTEXT, STALE25,
             True, "STALE_DATA_BREACH", "THE BREAK from v1: stale stamp (25h) behind a normal count 12v12 — now fires at Tier 0")
observe_case("D12b", "cp_ro_count", 12, 12, 2, SR_CONTEXT, None,
             True, "MISSING_MANDATORY_TIMESTAMP", "missing stamp on freshness-critical metric -> fail-closed breach")
observe_case("D12c", "cp_ro_count", 12, 12, 2, SR_CONTEXT, "whenever lol",
             True, "UNPARSEABLE_TIMESTAMP", "garbage stamp -> fail-closed breach")
observe_case("D12d", "cp_ro_count", 12, 12, 2, SR_CONTEXT, FRESH,
             False, "no surprise", "control: fresh stamp + normal count stays QUIET (freshness does not false-fire)")
observe_case("D12e", "cp_ro_count", 12, 12, 2, AUDIT_CONTEXT, STALE25,
             True, "STALE_DATA_BREACH",
             "POST-D12e-FIX (2026-10-13, Loki + Gemini-endorsed): the manifest metric_objectives map "
             "routes cp_ro_count to the 4h service-reporting objective BY NAME, superseding text "
             "relevance — a 25h-old stamp in a plain audit context now BREACHES. The pre-fix caveat "
             "this case originally probed no longer exists for mapped names.")
observe_case("D12f", "unmapped_audit_probe", 12, 12, 2, AUDIT_CONTEXT, STALE25,
             False, "no surprise",
             "CAVEAT probe (retained): an UNMAPPED metric name still routes by text relevance — plain "
             "audit context resolves to obj_accounting_reconciliation (72h window), so a 25h-old stamp "
             "is inside tolerance and stays quiet. The routing caveat is reported, not hidden.")

# ---------------------------------------------------------------- relevance bypass
print("\n== VETO) RELEVANCE BYPASS (z >= 4.0) ==")
v = cl.gate_numeric(4.80, 0.028, MANIFEST)
record("V1", "FIRES" if v["fires"] else "QUIET", "FIRES", "PASS" if v["fires"] else "FAIL",
       f"logged case 2026-10-08 e5_flagged_mtd: z=4.80 relevance=0.028 was DROPPED pre-patch -> {v['reason'][:100]}")
v = cl.gate_numeric(91.64, 0.061, MANIFEST)
record("V2", "FIRES" if v["fires"] else "QUIET", "FIRES", "PASS" if v["fires"] else "FAIL",
       f"logged case 2026-10-07 b34_unapplied: z=91.64 relevance=0.061 was DROPPED pre-patch -> {v['reason'][:100]}")
v = cl.gate_numeric(2.50, 0.028, MANIFEST)
record("V3", "FIRES" if v["fires"] else "QUIET", "QUIET", "PASS" if not v["fires"] else "FAIL",
       "floor still applies below z=4.0 (z=2.50, rel=0.028 stays dropped)")
v = cl.gate_numeric(4.00, 0.0, MANIFEST)
record("V4", "FIRES" if v["fires"] else "QUIET", "FIRES", "PASS" if v["fires"] else "FAIL",
       "boundary: z=4.00 with relevance 0.0 fires on numeric alone")

# ---------------------------------------------------------------- parser
print("\n== PARSE) timestamp parser ==")
p1 = cl._parse_data_timestamp("Oct 13 2026 4:52 AM")
ok1 = p1 is not None and p1.hour == 8 and p1.minute == 52  # 4:52 AM EDT = 08:52 UTC
record("P1", "parsed" if p1 else "None", "parsed", "PASS" if ok1 else "FAIL",
       f"'Oct 13 2026 4:52 AM' -> {p1} (naive read as America/New_York)")
p2 = cl._parse_data_timestamp("October 13, 2026 4:52:30 AM")
record("P2", "parsed" if p2 else "None", "parsed", "PASS" if p2 is not None else "FAIL",
       f"'October 13, 2026 4:52:30 AM' -> {p2}")
p3 = cl._parse_data_timestamp("2026-10-13T08:52:00Z")
record("P3", "parsed" if p3 else "None", "parsed", "PASS" if (p3 and p3.hour == 8) else "FAIL",
       f"ISO 8601 '2026-10-13T08:52:00Z' -> {p3}")
p4 = cl._parse_data_timestamp("whenever lol")
record("P4", "None" if p4 is None else "parsed", "None", "PASS" if p4 is None else "FAIL",
       "garbage string -> None (drives UNPARSEABLE breach)")
p5 = cl._parse_data_timestamp("2026-10-15 04:47:08 EDT")
ok5 = p5 is not None and p5.hour == 8 and p5.minute == 47  # 04:47:08 EDT = 08:47:08 UTC
record("P5", "parsed" if p5 else "None", "parsed", "PASS" if ok5 else "FAIL",
       f"'2026-10-15 04:47:08 EDT' -> {p5} (tz abbreviation stripped, naive read as America/New_York)")
p6 = cl._parse_data_timestamp("2026-01-15 04:47:08 EST")
ok6 = p6 is not None and p6.hour == 9 and p6.minute == 47  # 04:47:08 EST = 09:47:08 UTC
record("P6", "parsed" if p6 else "None", "parsed", "PASS" if ok6 else "FAIL",
       f"'2026-01-15 04:47:08 EST' -> {p6} (EST variant)")

# ---------------------------------------------------------------- D13
print("\n== D13) SLOW DRIFT +1/day x7, sigma 2, baseline 10 ==")
# 2026-10-16: D13 is now exercised through the WIRED path (observe /
# observe_delta against a temp runs dir), because that is where the
# built drift detection lives: the loop holds the anchor from the
# job's own logged history (v1.2 design: 14-day median anchor for
# stationary metrics, trajectory band for cumulative ones). The old
# pure-function rolling case tested a stateless path production never
# uses — a rolling anchor with no held history cannot catch drift by
# construction, which was the finding, not the mechanism.
base = 10
tmp = tempfile.mkdtemp(prefix="cl-d13r-")
try:
    cl.RUNS_DIR = tmp
    # Stationary history: the metric sat at 10 for the 14 days before
    # the drift week (2026-09-20 .. 2026-10-03).
    for d in range(14):
        seed_level(tmp, "harness", f"2026-09-{20 + d:02d}" if d < 11 else f"2026-10-{d - 10:02d}",
                   "drift_level", base)
    prev, rolling_fires = base, []
    for day in range(1, 8):
        rl = cl.RunLog("harness", f"2026-10-{3 + day:02d}")
        v = cl.observe(rl, MANIFEST, "drift_level", base + day, prev, 2,
                       context="harness drift rolling anchor", evidence="harness",
                       source="harness", data_timestamp=fresh_stamp())
        rolling_fires.append(v["fires"])
        prev = base + day
    record("D13-rolling", f"fires on days {[i+1 for i, f in enumerate(rolling_fires) if f]}",
           "fires (drift caught)", "PASS" if any(rolling_fires) else "FAIL",
           "rolling expected (yesterday's value) through observe(): per-day z stays 0.50, but the "
           "held 14-day median anchor (10) catches the accumulated drift — SLOW_DRIFT_MEDIAN_ANCHOR")
finally:
    cl.RUNS_DIR = REAL_RUNS
    shutil.rmtree(tmp, ignore_errors=True)
fx = [cl.gate_numeric(cl.numeric_delta(base + d, base, 2), 0.35, MANIFEST)["fires"] for d in range(1, 8)]
record("D13-fixed", f"fires on days {[i+1 for i, f in enumerate(fx) if f]}", "fires day 5+",
       "PASS" if fx[4] else "FAIL",
       "fixed-anchor control still fires day 5 — and since 2026-10-16 the wired path holds "
       "its own anchor (14-day median / trajectory), so the control is no longer hypothetical")
# Stationary control: same held history, NO drift — the median anchor
# must stay quiet on a stable metric (drift detection must not
# false-fire on the population it protects).
tmp = tempfile.mkdtemp(prefix="cl-d13s-")
try:
    cl.RUNS_DIR = tmp
    for d in range(14):
        seed_level(tmp, "harness", f"2026-09-{20 + d:02d}" if d < 11 else f"2026-10-{d - 10:02d}",
                   "stable_level", base)
    stable_fires = []
    for day in range(1, 8):
        rl = cl.RunLog("harness", f"2026-10-{3 + day:02d}")
        v = cl.observe(rl, MANIFEST, "stable_level", base, base, 2,
                       context="harness drift stable control", evidence="harness",
                       source="harness", data_timestamp=fresh_stamp())
        stable_fires.append(v["fires"])
    record("D13-stable", f"fires on days {[i+1 for i, f in enumerate(stable_fires) if f]}",
           "QUIET all 7 days", "PASS" if not any(stable_fires) else "FAIL",
           "control: a flat metric against its own 14-day median anchor stays quiet all week")
finally:
    cl.RUNS_DIR = REAL_RUNS
    shutil.rmtree(tmp, ignore_errors=True)
# delta-mode drift probe: level +1/day gated as increments vs zero-change baseline
tmp = tempfile.mkdtemp(prefix="cl-d13-")
try:
    cl.RUNS_DIR = tmp
    seed_level(tmp, "harness", "2026-10-01", "drift_metric", 10)
    drift_fires = []
    for day in range(1, 8):
        rl = cl.RunLog("harness", f"2026-10-0{day+1}")
        v = cl.observe_delta(rl, MANIFEST, "drift_metric", 10 + day, 0, 2,
                             context="harness drift", evidence="harness", source="harness",
                             data_timestamp=fresh_stamp())
        drift_fires.append(v["fires"])
    record("D13-delta", f"fires on days {[i+1 for i, f in enumerate(drift_fires) if f]}",
           "fires (drift caught)", "PASS" if any(drift_fires) else "FAIL",
           "delta mode vs +1/day level drift: the increment stays +1 (z=0.50, quiet), but the "
           "level's deviation from its trajectory band (anchor 10 + 0/day) accumulates to "
           "z=2.50 by day 5 — SLOW_DRIFT_TRAJECTORY")
finally:
    cl.RUNS_DIR = REAL_RUNS
    shutil.rmtree(tmp, ignore_errors=True)

# ------------------------------------------------- 2026-10-16 regressions
print("\n== X) 2026-10-16 FIX REGRESSIONS (per-job baselines, cadence routing) ==")
# X1: two jobs log the SAME metric name with different levels; each
# job's observe-delta must anchor to its OWN prior (the live
# DELTA_BASELINE_CROSS_LEG_COLLISION incident, replayed: advisor 271
# vs tech-util 125 on 2026-10-15; advisor level 289 next day must
# compute increment +18, not +164).
tmp = tempfile.mkdtemp(prefix="cl-x1-")
try:
    cl.RUNS_DIR = tmp
    seed_level(tmp, "job-tech", "2026-10-15", "mtd_ro_total", 125)
    seed_level(tmp, "job-adv", "2026-10-15", "mtd_ro_total", 271)
    rl_a = cl.RunLog("job-adv", "2026-10-16")
    va = cl.observe_delta(rl_a, MANIFEST, "mtd_ro_total", 289, 18, 7,
                          context="harness crossjob", evidence="harness", source="harness",
                          data_timestamp=fresh_stamp())
    inc_a = [r for r in rl_a.read_all() if r["type"] == "observed"][-1]["payload"]["observed"]
    rl_t = cl.RunLog("job-tech", "2026-10-16")
    vt = cl.observe_delta(rl_t, MANIFEST, "mtd_ro_total", 138, 13, 7,
                          context="harness crossjob", evidence="harness", source="harness",
                          data_timestamp=fresh_stamp())
    inc_t = [r for r in rl_t.read_all() if r["type"] == "observed"][-1]["payload"]["observed"]
    ok = inc_a == 18 and inc_t == 13 and not va["fires"] and not vt["fires"]
    record("X1-crossjob", f"adv increment={inc_a} (fires={va['fires']}), tech increment={inc_t} (fires={vt['fires']})",
           "increments 18 and 13, both QUIET", "PASS" if ok else "FAIL",
           "per-job delta baselines: each leg anchors to its own prior level; the cross-leg "
           "collision (289-125=164, z=20.86) cannot recur")
finally:
    cl.RUNS_DIR = REAL_RUNS
    shutil.rmtree(tmp, ignore_errors=True)
# X2: a leg that declares cadence_metrics routes ALL its observes
# through the nightly-cadence proof — including auxiliary metrics like
# write_readback carrying the same report stamp (2026-10-16 incident:
# 5.81h-old stamp false-fired STALE under the strict 4h rule). The
# control job (no cadence declaration) must still breach on the same
# stamp, and a stamp beyond the cadence bound must still breach on
# the cadence leg (fail-closed preserved).
STAMP6 = prose(now - timedelta(hours=6))
tmp = tempfile.mkdtemp(prefix="cl-x2-")
try:
    cl.RUNS_DIR = tmp
    rl_c = cl.RunLog("tech-utilization", now.date().isoformat())
    vc = cl.observe(rl_c, MANIFEST, "write_readback", 1.0, 1.0, 0.5,
                    context=SR_CONTEXT, evidence="harness", source="harness",
                    data_timestamp=STAMP6)
    fr_c = [r for r in rl_c.read_all() if r["type"] == "observed"][-1]["payload"]["freshness"]
    ok_c = (not vc["fires"] and fr_c.get("proof_mode") == "nightly_cadence"
            and fr_c.get("status") == "ok")
    record("X2-cadence-aux", f"fires={vc['fires']} proof_mode={fr_c.get('proof_mode')} status={fr_c.get('status')}",
           "QUIET, nightly_cadence, ok", "PASS" if ok_c else "FAIL",
           "auxiliary write_readback on a cadence-declared leg takes the leg's cadence proof: "
           "a 6h-old nightly stamp passes (bound 26h) instead of false-firing at the 4h rule")
    rl_n = cl.RunLog("harness", now.date().isoformat())
    vn = cl.observe(rl_n, MANIFEST, "write_readback", 1.0, 1.0, 0.5,
                    context=SR_CONTEXT, evidence="harness", source="harness",
                    data_timestamp=STAMP6)
    ok_n = vn["fires"] and "STALE_DATA_BREACH" in vn["reason"]
    record("X2-nocadence-control", f"fires={vn['fires']}",
           "FIRES (STALE_DATA_BREACH)", "PASS" if ok_n else "FAIL",
           f"control: same stamp on a leg with NO cadence declaration still breaches at 4h | {vn['reason'][:80]}")
    # A 30h-old stamp exceeds even the 26h cadence bound — must breach.
    rl_s2 = cl.RunLog("tech-utilization", now.date().isoformat())
    vs2 = cl.observe(rl_s2, MANIFEST, "write_readback", 1.0, 1.0, 0.5,
                     context=SR_CONTEXT, evidence="harness", source="harness",
                     data_timestamp=prose(now - timedelta(hours=30)))
    ok_s = vs2["fires"] and "STALE_DATA_BREACH" in vs2["reason"]
    record("X2-cadence-stale", f"fires={vs2['fires']}",
           "FIRES (STALE_DATA_BREACH)", "PASS" if ok_s else "FAIL",
           f"fail-closed preserved: a 30h-old stamp breaches even under cadence (bound 26h) | {vs2['reason'][:80]}")
finally:
    cl.RUNS_DIR = REAL_RUNS
    shutil.rmtree(tmp, ignore_errors=True)

# ------------------------------------------------- S) SPOOL FALLBACK (2026-10-25)
print("\n== S) SPOOL FALLBACK — the record survives a write-blocked context ==")
REAL_RES_P = cl.RESOLUTIONS_PATH
REAL_SPOOL_D = cl.SPOOL_DIR
REAL_RUNS_S = cl.RUNS_DIR
tmp = tempfile.mkdtemp(prefix="cl-spool-")
try:
    cl.RESOLUTIONS_PATH = os.path.join(tmp, "anomaly_resolutions.json")
    cl.SPOOL_DIR = os.path.join(tmp, "spool")
    cl.RUNS_DIR = os.path.join(tmp, "runs")
    os.makedirs(cl.RUNS_DIR)
    cl._write_json(cl.RESOLUTIONS_PATH, {"resolutions": []})
    real_append_s = cl._append_line
    real_write_s = cl._write_json

    def blocked_write(path, data):
        if os.path.abspath(path) == os.path.abspath(cl.RESOLUTIONS_PATH):
            raise PermissionError("simulated broker EPERM (harness)")
        return real_write_s(path, data)

    # S-a: stage with the resolutions write blocked -> the entry spools,
    # stage returns normally, the primary store stays untouched.
    cl._write_json = blocked_write
    try:
        ent = cl.stage_resolution("HARNESS_SPOOL_SIG", "ctx", "cause",
                                  "inv-harness-spool")
    finally:
        cl._write_json = real_write_s
    spool_stage = ([n for n in os.listdir(cl.SPOOL_DIR) if n.startswith("stage-")]
                   if os.path.isdir(cl.SPOOL_DIR) else [])
    store_now = cl._read_json(cl.RESOLUTIONS_PATH)
    ok = (ent.get("spooled") is True and len(spool_stage) == 1
          and store_now.get("resolutions") == [])
    record("S-stage-spool",
           f"spooled={ent.get('spooled')} spool_files={len(spool_stage)} "
           f"store_entries={len(store_now.get('resolutions', []))}",
           "spooled=True, 1 spool file, store empty", "PASS" if ok else "FAIL",
           "primary stage write blocked: entry lands in spool, stage returns "
           "normally with the spooled marker")

    # S-b: run-log append blocked -> the line spools, event returns
    # normally, no run file is created.
    def blocked_append(path, line):
        if os.path.abspath(path).startswith(os.path.abspath(cl.RUNS_DIR)):
            raise PermissionError("simulated broker EPERM (harness)")
        return real_append_s(path, line)
    cl._append_line = blocked_append
    try:
        rl_s = cl.RunLog("harness-spool", "2026-10-25")
        rec_s = rl_s.event("predict", {"name": "spool_probe"})
    finally:
        cl._append_line = real_append_s
    spool_rl = ([n for n in os.listdir(cl.SPOOL_DIR) if n.startswith("runlog-")]
                if os.path.isdir(cl.SPOOL_DIR) else [])
    ok = (rec_s.get("spooled") is True and len(spool_rl) == 1
          and not os.path.exists(rl_s.path))
    record("S-runlog-spool",
           f"spooled={rec_s.get('spooled')} spool_files={len(spool_rl)} "
           f"runfile_exists={os.path.exists(rl_s.path)}",
           "spooled=True, 1 spool file, no run file", "PASS" if ok else "FAIL",
           "primary append blocked: line lands in spool, event returns "
           "normally with the spooled marker")

    # S-c: merge drains both kinds, dedupes on a re-merge, and forces
    # verified_by_principal False — a spool can never create a verified
    # resolution.
    counts1 = cl.merge_spool()
    store1 = cl._read_json(cl.RESOLUTIONS_PATH)
    staged1 = [e for e in store1.get("resolutions", [])
               if e.get("signature") == "HARNESS_SPOOL_SIG"]
    lines1 = rl_s.read_all()
    counts2 = cl.merge_spool()
    leftovers = os.listdir(cl.SPOOL_DIR) if os.path.isdir(cl.SPOOL_DIR) else []
    ok = (counts1["staged"] == 1 and counts1["runlog_lines"] == 1
          and len(staged1) == 1
          and staged1[0].get("verified_by_principal") is False
          and len(lines1) == 1
          and lines1[0]["payload"].get("name") == "spool_probe"
          and counts2 == {"staged": 0, "runlog_lines": 0, "files_drained": 0}
          and leftovers == [])
    record("S-merge",
           f"merge1={counts1} staged={len(staged1)} "
           f"verified={staged1[0].get('verified_by_principal') if staged1 else None} "
           f"runlog_lines={len(lines1)} merge2={counts2} leftovers={leftovers}",
           "drains 1 stage + 1 line, verified forced False, re-merge no-op, "
           "spool empty", "PASS" if ok else "FAIL",
           "merge drains stage+runlog, dedupes, and can never create a "
           "verified resolution")

    # S-d: a merge attempted from a still-blocked context is a silent
    # no-op — the spool stays intact and nothing raises.
    cl._write_json = blocked_write
    try:
        ent2 = cl.stage_resolution("HARNESS_SPOOL_SIG2", "ctx", "cause",
                                   "inv-harness-spool2")
        counts3 = cl.merge_spool()
    finally:
        cl._write_json = real_write_s
    still = ([n for n in os.listdir(cl.SPOOL_DIR) if n.startswith("stage-")]
             if os.path.isdir(cl.SPOOL_DIR) else [])
    store2 = cl._read_json(cl.RESOLUTIONS_PATH)
    ok = (ent2.get("spooled") is True
          and counts3 == {"staged": 0, "runlog_lines": 0, "files_drained": 0}
          and len(still) == 1
          and not any(e.get("signature") == "HARNESS_SPOOL_SIG2"
                      for e in store2.get("resolutions", [])))
    record("S-merge-blocked", f"merge={counts3} spool_left={len(still)}",
           "merge no-op raising nothing, spool intact, store unchanged",
           "PASS" if ok else "FAIL",
           "a write-blocked merge leaves the spool for the next writable "
           "context — merge is never a new failure mode")
finally:
    cl.RESOLUTIONS_PATH = REAL_RES_P
    cl.SPOOL_DIR = REAL_SPOOL_D
    cl.RUNS_DIR = REAL_RUNS_S
    shutil.rmtree(tmp, ignore_errors=True)

# ------------------------------------------------- K) CORE PATCH (2026-10-31)
print("\n== K) CORE PATCH 2026-10-31 — kill-check, verified appends, fail-closed ==")
REAL_RES_K = cl.RESOLUTIONS_PATH
REAL_SPOOL_K = cl.SPOOL_DIR
REAL_RUNS_K = cl.RUNS_DIR
tmp = tempfile.mkdtemp(prefix="cl-corepatch-")
try:
    cl.RESOLUTIONS_PATH = os.path.join(tmp, "anomaly_resolutions.json")
    cl.SPOOL_DIR = os.path.join(tmp, "spool")
    cl.RUNS_DIR = os.path.join(tmp, "runs")
    os.makedirs(cl.RUNS_DIR)
    cl._write_json(cl.RESOLUTIONS_PATH, {"resolutions": []})

    # K1: the CLI refuses to stage without --investigation (absent or
    # blank) — non-zero return, and nothing reaches the store.
    rc_absent = cl.main(["stage", "--signature", "HARNESS_NOINV",
                         "--context", "ctx", "--root-cause", "cause"])
    rc_blank = cl.main(["stage", "--signature", "HARNESS_NOINV",
                        "--context", "ctx", "--root-cause", "cause",
                        "--investigation", "  "])
    store_k1 = cl._read_json(cl.RESOLUTIONS_PATH)
    ok = (rc_absent == 2 and rc_blank == 2
          and store_k1.get("resolutions") == [])
    record("K1-stage-refused", f"rc_absent={rc_absent} rc_blank={rc_blank} "
           f"store_entries={len(store_k1.get('resolutions', []))}",
           "rc=2 both, store empty", "PASS" if ok else "FAIL",
           "CLI stage without --investigation is refused before any "
           "write — the invented-resolution path is closed at the CLI")

    # K2: stage_resolution witnesses a resolution_staged event on the
    # given run log; check_violations flags the one staged with no
    # investigation id and clears the one that has one.
    rl_k = cl.RunLog("harness-corepatch", "2026-10-31")
    cl.stage_resolution("HARNESS_K_SIG_OK", "ctx", "cause",
                        "inv-harness-k2", runlog=rl_k)
    cl.stage_resolution("HARNESS_K_SIG_NAKED", "ctx", "cause",
                        None, runlog=rl_k)
    staged_evs = [r for r in rl_k.read_all() if r["type"] == "resolution_staged"]
    inv_viol = [v for v in cl.check_violations(rl_k)
                if v["kind"] == "invented_resolution"]
    ok = (len(staged_evs) == 2
          and staged_evs[0]["payload"].get("investigation_id") == "inv-harness-k2"
          and staged_evs[1]["payload"].get("investigation_id") is None
          and len(inv_viol) == 1
          and "HARNESS_K_SIG_NAKED" in inv_viol[0]["detail"])
    record("K2-stage-event", f"events={len(staged_evs)} invented_flagged={len(inv_viol)}",
           "2 events, exactly the naked one flagged", "PASS" if ok else "FAIL",
           "stage emits resolution_staged (signature + investigation_id); "
           "check_violations can now fire on a resolution staged with no "
           "investigation — the formerly dead kill check is live")

    # K3: short writes on the run-log append path: os.write accepts
    # only a few bytes per call — the write loop must still land the
    # line whole, exactly once.
    target_k3 = os.path.join(tmp, "shortwrite.jsonl")
    real_os_write = os.write

    def short_write(fd, data):
        return real_os_write(fd, data[:7])
    os.write = short_write
    try:
        cl._append_line(target_k3, '{"probe": "k3"}\n')
    finally:
        os.write = real_os_write
    with open(target_k3, "r", encoding="utf-8") as fh:
        landed_k3 = fh.read()
    ok = landed_k3 == '{"probe": "k3"}\n'
    record("K3-append-shortwrite", f"landed={landed_k3!r}",
           "the exact line, once", "PASS" if ok else "FAIL",
           "os.write capped at 7 bytes/call: the append loop completes "
           "the buffer instead of publishing a truncated line as success")

    # K4: silent-drop writes: os.write reports full acceptance but
    # lands nothing — the size-delta verification must catch it and
    # raise loudly rather than return success.
    target_k4 = os.path.join(tmp, "silentdrop.jsonl")

    def drop_write(fd, data):
        return len(data)
    os.write = drop_write
    raised_k4 = False
    try:
        cl._append_line(target_k4, '{"probe": "k4"}\n')
    except OSError:
        raised_k4 = True
    finally:
        os.write = real_os_write
    size_k4 = os.path.getsize(target_k4) if os.path.exists(target_k4) else -1
    ok = raised_k4 and size_k4 == 0
    record("K4-append-silentdrop", f"raised={raised_k4} size={size_k4}",
           "OSError raised, file empty", "PASS" if ok else "FAIL",
           "write reports success but lands 0 bytes: size-delta "
           "verification retries, then raises — never a false success")

    # K5: check_violations fails closed on a missing or 0-byte run
    # log, and stays clean on a healthy one.
    rl_missing = cl.RunLog("harness-nolog", "2026-10-31")
    v_missing = cl.check_violations(rl_missing)
    open(rl_missing.path, "w").close()  # 0-byte file
    v_empty = cl.check_violations(rl_missing)
    rl_ok = cl.RunLog("harness-oklog", "2026-10-31")
    rl_ok.event("observed", {"name": "probe", "evidence": "harness"})
    v_ok = cl.check_violations(rl_ok)

    def kinds(vs):
        return [v["kind"] for v in vs]
    ok = ("runlog_missing_or_empty" in kinds(v_missing)
          and "runlog_missing_or_empty" in kinds(v_empty)
          and v_ok == [])
    record("K5-check-failclosed",
           f"missing={kinds(v_missing)} empty={kinds(v_empty)} healthy={kinds(v_ok)}",
           "missing+empty flagged, healthy clean", "PASS" if ok else "FAIL",
           "a leg whose receipts were eaten surfaces as a violation, "
           "not as a clean scan of nothing")

    # K6: _write_json rejects a valid-but-wrong JSON replacement:
    # the post-replace file parses, but is not the intended object.
    target_k6 = os.path.join(tmp, "store.json")
    cl._write_json(target_k6, {"resolutions": []})
    real_os_replace = os.replace

    def wrong_replace(src, dst):
        real_os_replace(src, dst)
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"resolutions": [{"signature": "WRONG"}]}) + "\n")
    os.replace = wrong_replace
    raised_k6 = False
    try:
        cl._write_json(target_k6, {"resolutions": []})
    except OSError:
        raised_k6 = True
    finally:
        os.replace = real_os_replace
    ok = raised_k6
    record("K6-writejson-content", f"raised={raised_k6}",
           "OSError raised", "PASS" if ok else "FAIL",
           "post-replace file parses as JSON but is the WRONG object: "
           "byte-equality verification rejects it (parse-only passed "
           "it pre-patch)")
finally:
    cl.RESOLUTIONS_PATH = REAL_RES_K
    cl.SPOOL_DIR = REAL_SPOOL_K
    cl.RUNS_DIR = REAL_RUNS_K
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- summary
print("\n== SUMMARY ==")
fails = [r for r in results if r["pf"] == "FAIL"]
for r in results:
    print(f"  {r['pf']:4s} {r['case']:10s} {r['note'][:95]}")
print(f"\nTOTAL: {len(results)} checks, {len(results)-len(fails)} PASS, {len(fails)} FAIL")
print("FAIL cases:", [r["case"] for r in fails])
