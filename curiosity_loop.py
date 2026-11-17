#!/usr/bin/env python3
"""
Curiosity Loop core — Predict → Compare → Gate → Investigate → Learn.

Shared mechanics for a fleet of scheduled workflow jobs.
Implements the gate math, TTL enforcement, audit trail, and kill conditions
from CURIOSITY_LOOP_v0.1.md and curiosity_manifest.json.

AUTHORITY: READ + TRACE + STAGE only. This module never writes business
cells, never closes ROs, never touches manual inputs. Any investigation step
that attempts another kind of operation raises KillConditionError.

Kill condition (the principal, 2026-10-01): if the loop hallucinates data,
invents resolutions, or alters close behavior in any way, it gets reported
and removed. His call, no appeals.

Usage (CLI, from a cron worker):
    python3 curiosity_loop.py predict   --job service-daily-tracking-master \\
        --name audited_date_cell --expected 2026-10-02 --source "Master!D5"
    python3 curiosity_loop.py observe   --job service-daily-tracking-master \\
        --name advisor_ro_total --observed 17 --expected 17 --sigma 1 \\
        --context "E9 read-back after write"
    python3 curiosity_loop.py stage     --signature WRITE_REPORTED_CLEAN_BUT_STALE \\
        --context "..." --root-cause "..." --investigation inv-20261003-001
    python3 curiosity_loop.py check     --job service-daily-tracking-master
    python3 curiosity_loop.py spool     [--job <job>]   # drain spooled records

All state lives beside this module, in the directory it runs from.
Stdlib only.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import datetime as _dt
import fcntl
import json
import math
import os
import re
import sys
import time
import uuid
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(BASE, "curiosity_manifest.json")
RESOLUTIONS_PATH = os.path.join(BASE, "anomaly_resolutions.json")
EXPECTATIONS_PATH = os.path.join(BASE, "leg_expectations.json")
RUNS_DIR = os.path.join(BASE, "runs")
SPOOL_DIR = os.path.join(BASE, "spool")
WATCHDOG_PATH = os.path.join(BASE, "watchdog_alerts.jsonl")

# The only operations an investigation may perform. Anything else is a
# kill-condition violation (behavior alteration).
ALLOWED_OPS = {"read", "trace", "stage"}

# Relevance floor: below this, surprise is Noisy TV and the gate stays shut.
RELEVANCE_FLOOR = 0.30


class KillConditionError(RuntimeError):
    """Raised when the loop (or its caller) breaches the principal's kill condition."""


class BudgetExhausted(RuntimeError):
    """Raised when an investigation exceeds its step or time budget."""


# --------------------------------------------------------------------------
# Stores
# --------------------------------------------------------------------------

# Concurrency hardening (the principal's "fix it" ruling, 2026-10-21).
# Morning-batch legs invoke this module in parallel against the SAME
# state files. Two failure modes seen live on 2026-10-21: (a) a stage
# read caught anomaly_resolutions.json momentarily empty — writers
# shared one fixed ".tmp" name, so interleaved truncate/write/replace
# sequences could publish a truncated file, and the store's
# read-modify-write was unserialized, so concurrent stages could lose
# entries; (b) run-log appends died with PermissionError under
# concurrent appends, failing legs whose business work had succeeded.
# Fixes: per-writer unique tmp names + fsync before the atomic
# replace, the resolutions read-modify-write serialized under an
# flock, JSON reads retried briefly against a torn read, and JSONL
# appends done as one locked O_APPEND write with bounded retry.
# Behavior is unchanged; only the I/O is safer.
#
# 2026-10-27 write-path patch (patch-2026-10-27-write-path.md): the
# remaining hole was VERIFICATION. This VM's filesystem
# intermittently truncates writes to 0 bytes, and nothing re-read
# what it had written: a truncated tmp could be published as the
# store, spool writes landed empty and were returned as successes
# (five 0-byte stage files, 2026-10-27), and stage_resolution's
# OSError-only guard let a JSONDecodeError from an unreadable store
# escape and lose the entry. Now: _write_json verifies the tmp
# before publishing and the target after replacing (3 attempts,
# then a loud OSError); _spool_write and the run-log spool append
# verify their payloads landed whole; stage spools on an unreadable
# store too; merge_spool explicitly never drains or deletes a
# 0-byte/corrupt spool file. I/O only, again.
#
# 2026-10-31 core patch (multi-AI panel code review; the principal's
# go): the 10-27 verification covered the store and the spool but
# NOT the primary run-log append, and one kill-condition check was
# dead. Now: _append_line is verified like the spool append (write
# loop, fsync, size-delta proof under the lock; torn landings raise);
# _write_json additionally requires the replaced file to equal the
# intended serialization byte-for-byte, not merely parse; the stage
# path emits the "resolution_staged" run-log event check_violations
# scans for, and the CLI refuses to stage without --investigation;
# check_violations fails closed on a missing/empty run log instead
# of reporting a clean scan of nothing. I/O and enforcement only —
# gate math, TTL/budget, and manifest semantics are untouched.

@contextlib.contextmanager
def _locked(path: str):
    """Exclusive advisory lock on <path>.lock, held across a
    read-modify-write so concurrent invocations serialize."""
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o660)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_json(path: str) -> dict:
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            last_exc = exc
            time.sleep(0.05 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def _verify_json_file(path: str) -> None:
    """Raise unless `path` exists, is non-empty, and parses as JSON.
    The read-back half of every verified write (2026-10-27 patch)."""
    if os.path.getsize(path) <= 0:
        raise OSError(f"verify failed: {path} is empty")
    with open(path, "r", encoding="utf-8") as fh:
        json.load(fh)


def _fsync_dir(dirpath: str) -> None:
    """Best-effort directory fsync, so a replace is durable and not
    just atomic. Some filesystems refuse it; there, durability is the
    post-write verification's problem, so failures are ignored."""
    try:
        fd = os.open(dirpath, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _write_json(path: str, data: dict) -> None:
    """Atomic, VERIFIED JSON write (2026-10-27 write-path patch).

    Serialize once; per attempt: write a per-writer unique tmp file in
    the same directory, flush + fsync, verify the tmp ON DISK (size
    > 0 and parses) BEFORE publishing it, os.replace over the target,
    fsync the directory, then re-read the target and require a clean
    parse AND byte-equality with the intended serialization
    (2026-10-31: a clean parse alone proves only that the file is
    SOME valid JSON — a stale or wrong-but-valid object would pass;
    equality with what was serialized proves it is THE intended
    object). This VM's filesystem intermittently truncates writes to
    0 bytes (seen live 2026-10-24→27: readers hitting an empty store,
    0-byte spool files); the pre-patch version of this function could
    publish such a truncation as the store and report success.
    Bounded: 3 attempts with short backoff. If no attempt lands a
    verified file, raise OSError — loudly — so callers fall back to
    the spool (stage) or leave records in place (merge) instead of
    treating a silently-empty file as a successful write."""
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    last_exc: Exception | None = None
    for attempt in range(3):
        tmp = f"{path}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            _verify_json_file(tmp)
            os.replace(tmp, path)
            _fsync_dir(os.path.dirname(path) or ".")
            _verify_json_file(path)
            with open(path, "r", encoding="utf-8") as fh:
                landed_text = fh.read()
            if landed_text != text:
                raise OSError(
                    f"verify failed: {path} content differs from the "
                    f"intended serialization (valid JSON, wrong object)")
            return
        except (OSError, json.JSONDecodeError) as exc:
            last_exc = exc
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
            time.sleep(0.05 * (attempt + 1))
    raise OSError(
        f"verified write to {path} failed after 3 attempts: {last_exc}")


def _append_line(path: str, line: str) -> None:
    """One VERIFIED locked O_APPEND write of a full line (2026-10-31
    core patch). Concurrent legs appending to the same JSONL must
    never interleave or die on a transient PermissionError — and the
    write must be proven to have landed: the pre-patch version
    ignored os.write's return count, never fsynced, and never
    verified, so on this filesystem a run-log append could vanish
    while the caller believed it landed (0-byte run logs,
    2026-10-29/30; the hall review found the hole). Now, under the
    flock: a write loop runs until the full buffer is accepted
    (os.write may accept only part of one), the descriptor is
    fsynced, and the append is verified by exact size delta — the
    same proof the per-writer run-log spool append already uses.
    A write that lands nothing is retried (bounded, 5 attempts); a
    write that lands TORN raises OSError immediately — a partial
    record is never retried into a duplicated or garbled line, and
    never reported as success. Callers keep their existing OSError
    contracts (RunLog.event spools, merge leaves the spool file,
    the watchdog swallows)."""
    data = line.encode("utf-8")
    last_exc: OSError | None = None
    for attempt in range(5):
        before: int | None = None
        after: int | None = None
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o660)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                before = os.fstat(fd).st_size
                written = 0
                while written < len(data):
                    n = os.write(fd, data[written:])
                    if n <= 0:
                        raise OSError(
                            f"os.write accepted 0 bytes appending to {path}")
                    written += n
                os.fsync(fd)
                after = os.fstat(fd).st_size
            finally:
                os.close(fd)
        except OSError as exc:
            last_exc = exc
            time.sleep(0.05 * (attempt + 1))
            continue
        if after == before + len(data):
            return
        if after != before:
            raise OSError(
                f"append to {path} landed torn ({before} -> {after}, "
                f"expected +{len(data)} bytes)")
        last_exc = OSError(
            f"append to {path} did not land (size unchanged at {before})")
        time.sleep(0.05 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


# --------------------------------------------------------------------------
# Spool fallback (spec spool-fallback-spec-2026-10-22; built 2026-10-25
# on the principal's "Fix whatever you need to today")
# --------------------------------------------------------------------------
# The 2026-10-21 patch made concurrent writes safe; it did not help a
# context that cannot write the shared store at all — broker-mediated
# worker contexts intermittently fail with PermissionError AFTER
# _append_line's retries, and the record was lost (4 legs on
# 2026-10-22; watchdog JSONDecodeError alerts on 2026-10-23/24/25 from
# readers hitting files a dying writer left torn). The spool makes the
# record un-losable: when a primary write fails after its retries, the
# payload goes to a per-writer UNIQUE file under spool/ (same
# pid+uuid pattern as _write_json tmp names — no shared file, no lock,
# no append contention), and the caller returns normally with a
# "spooled": true marker. A later invocation that CAN write drains the
# spool opportunistically (merge_spool, also a standalone CLI op):
# stage entries are appended under the resolutions lock with dedupe
# and verified_by_principal forced False — a spool can never smuggle
# in a verified resolution; the principal's sign-off remains the only
# path to verified. Merge never raises: a context that still cannot
# write leaves the spool in place for the next one. I/O only — gate
# logic, TTL/budget, and manifest semantics are untouched.

def _spool_write(kind: str, name: str, text: str, suffix: str) -> str:
    """Write one spooled payload to a per-writer unique file, VERIFY
    it landed (size > 0; for .json payloads, a clean parse), and
    return its path. A spool file that lands at 0 bytes is a FAILED
    write, not a spooled record — the pre-patch version returned such
    files as successes (five of them sat in spool/ on 2026-10-27,
    unreadable and undrainable). Bounded: 3 attempts, each under a
    fresh unique name, failed attempts unlinked. Raises OSError only
    if no attempt lands a verified file — callers treat that as
    today's pre-spool behavior (record survives only in the leg's own
    run notes)."""
    os.makedirs(SPOOL_DIR, exist_ok=True)
    last_exc: Exception | None = None
    for attempt in range(3):
        path = os.path.join(
            SPOOL_DIR, f"{kind}-{name}-{os.getpid()}-{uuid.uuid4().hex[:12]}{suffix}")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            if os.path.getsize(path) <= 0:
                raise OSError(f"spool write landed empty: {path}")
            if suffix == ".json":
                with open(path, "r", encoding="utf-8") as fh:
                    json.load(fh)
            return path
        except (OSError, json.JSONDecodeError) as exc:
            last_exc = exc
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass
            time.sleep(0.05 * (attempt + 1))
    raise OSError(
        f"spool write failed verification after 3 attempts: {last_exc}")


def merge_spool(runlog: "RunLog | None" = None) -> dict:
    """Best-effort drain of spool/ into the primary stores. Returns
    counts {"staged": n, "runlog_lines": n, "files_drained": n}.
    Never raises — merge is an opportunistic repair, not a new failure
    mode for a leg. Spool files are removed only after their payload is
    durably in the primary store AND verified there (stage: dedupe-key
    presence on a locked re-read; runlog: the appended lines counted in
    the target file). Written by the merging invocation as a
    "spool_merge" run-log event when it has a run log and anything
    drained, so the drain is itself witnessed."""
    counts = {"staged": 0, "runlog_lines": 0, "files_drained": 0}
    try:
        if not os.path.isdir(SPOOL_DIR):
            return counts
        names = sorted(os.listdir(SPOOL_DIR))
    except OSError:
        return counts
    # Stage entries first: append under the resolutions lock, deduped,
    # verified flag forced False on merge.
    stage_names = [n for n in names if n.startswith("stage-") and n.endswith(".json")]
    if stage_names:
        drained: list[str] = []
        try:
            with _locked(RESOLUTIONS_PATH):
                store = load_resolutions()
                entries = store.setdefault("resolutions", [])
                keys = {(e.get("signature"), e.get("discovered_at"),
                         e.get("investigation_id")) for e in entries}
                newly = 0
                for name in stage_names:
                    path = os.path.join(SPOOL_DIR, name)
                    try:
                        if os.path.getsize(path) <= 0:
                            # A 0-byte spool file is a write that
                            # failed (pre-2026-10-27 spool writes were
                            # unverified). It holds no entry: never
                            # drain it, never delete it here — cleanup
                            # of empty spool files is a recorded human
                            # act, not a merge side effect.
                            continue
                        with open(path, "r", encoding="utf-8") as fh:
                            item = json.load(fh)
                    except (OSError, json.JSONDecodeError):
                        continue  # unreadable: leave in place
                    entry = dict(item.get("entry") or {})
                    if not entry.get("signature"):
                        continue
                    key = (entry.get("signature"), entry.get("discovered_at"),
                           entry.get("investigation_id"))
                    if key not in keys:
                        entry["verified_by_principal"] = False
                        entry["verified_at"] = None
                        entries.append(entry)
                        keys.add(key)
                        newly += 1
                    drained.append(path)
                if newly:
                    _write_json(RESOLUTIONS_PATH, store)
                    # Count only after the write lands — if it raises,
                    # nothing drained and the spool stays whole.
                    counts["staged"] += newly
                # Verify on a locked re-read before deleting anything:
                # every drained entry's key must be present in the store.
                if drained:
                    check = load_resolutions()
                    have = {(e.get("signature"), e.get("discovered_at"),
                             e.get("investigation_id"))
                            for e in check.get("resolutions", [])}
                    for path in drained:
                        try:
                            with open(path, "r", encoding="utf-8") as fh:
                                item = json.load(fh)
                            e = item.get("entry") or {}
                            if (e.get("signature"), e.get("discovered_at"),
                                    e.get("investigation_id")) in have:
                                os.unlink(path)
                                counts["files_drained"] += 1
                        except OSError:
                            pass
        except (OSError, json.JSONDecodeError):
            # Cannot write the store — or cannot re-read it to verify
            # (a torn/empty read): spool stays whole for the next
            # writer. Merge honors its never-raises contract for
            # corrupt-store reads too, not just OS errors.
            pass
    # Run-log lines: append verbatim to each spool file's target run
    # file, then verify by line count before deleting the spool file.
    for name in [n for n in names if n.startswith("runlog-") and n.endswith(".jsonl")]:
        path = os.path.join(SPOOL_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                items = [json.loads(line) for line in fh if line.strip()]
            if not items:
                os.unlink(path)
                counts["files_drained"] += 1
                continue
            target = items[0]["target"]
            lines = [item["line"] for item in items]
            before = 0
            if os.path.exists(target):
                with open(target, "r", encoding="utf-8") as fh:
                    before = sum(1 for line in fh if line.strip())
            for line in lines:
                _append_line(target, line)
            with open(target, "r", encoding="utf-8") as fh:
                after = sum(1 for line in fh if line.strip())
            if after - before >= len(lines):
                os.unlink(path)
                counts["runlog_lines"] += len(lines)
                counts["files_drained"] += 1
        except (OSError, json.JSONDecodeError, KeyError):
            continue  # leave this spool file for a later writer
    if runlog is not None and (counts["staged"] or counts["runlog_lines"]):
        try:
            runlog.event("spool_merge", counts)
        except Exception:
            pass
    return counts


def load_manifest() -> dict:
    return _read_json(MANIFEST_PATH)


def _live_read_prefixes(job_id: str) -> list:
    """Metric-name prefixes declared live_read for a job, from
    leg_expectations.json legs[<job>].live_read_metrics. Live-read
    sources (timecard exports, survey-platform views, source-system
    Performance) print no Last Updated stamp — the read is live against
    the system of record — so Tier 0 takes a different proof for them
    (retrieval timestamp + population completeness; see _freshness_gate).
    Classification lives with the leg definitions so it is auditable in
    one place; the worker cannot choose the mode per call."""
    try:
        legs = _read_json(EXPECTATIONS_PATH).get("legs", {}) or {}
    except Exception:
        return []
    return (legs.get(job_id, {}) or {}).get("live_read_metrics", []) or []


def _cadence_prefixes(job_id: str) -> list:
    """Metric-name prefixes declared nightly_cadence for a job, from
    leg_expectations.json legs[<job>].cadence_metrics. Nightly-cadence
    sources (a source-system utilization report) DO print a Last Updated
    stamp, but it is produced by a nightly batch for the audited date,
    so the intraday objective max_age can never be met at the morning
    run. Tier 0 keeps the stamp requirement (present, parseable,
    fail-closed) and swaps only the age bound for the manifest's
    cadence bound (see _freshness_gate). Declared per leg like
    live_read; the worker cannot choose the mode per call. Approved by
    the principal 2026-10-15 (TECH_UTIL_NIGHTLY_STAMP_CADENCE sign-off).
    Since the 2026-10-16 routing fix the declaration marks the LEG as
    a nightly-cadence source: in observe() any non-empty declaration
    routes ALL of the leg's observes through the cadence proof, not
    just the listed metric names (see observe())."""
    try:
        legs = _read_json(EXPECTATIONS_PATH).get("legs", {}) or {}
    except Exception:
        return []
    return (legs.get(job_id, {}) or {}).get("cadence_metrics", []) or []


def load_resolutions() -> dict:
    return _read_json(RESOLUTIONS_PATH)


def verified_signatures() -> list[dict]:
    """Resolutions the principal has signed off — pre-loaded into baselines."""
    store = load_resolutions()
    return [r for r in store.get("resolutions", []) if r.get("verified_by_principal")]


def load_leg_expectations() -> dict:
    return _read_json(EXPECTATIONS_PATH)


def stage_resolution(signature: str, context: str, root_cause: str,
                     investigation_id: str | None,
                     baseline_correction: dict | None = None,
                     runlog: "RunLog | None" = None) -> dict:
    """Stage a learned resolution. Always unverified — only the principal flips
    verified_by_principal to true, on explicit sign-off.

    When a runlog is given, the staging is witnessed by a
    "resolution_staged" run-log event carrying the signature and
    investigation_id (2026-10-31 core patch): check_violations has
    scanned for that event type since the kill conditions were
    written, but nothing ever emitted it, so the invented-resolution
    check could not fire. The CLI additionally refuses to stage
    without --investigation at all."""
    entry = {
        "signature": signature,
        "context": context,
        "root_cause": root_cause,
        "discovered_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "investigation_id": investigation_id,
        "baseline_correction": baseline_correction or {},
        "verified_by_principal": False,
        "verified_at": None,
        "ttl": None,
    }
    def _witness_staged() -> None:
        if runlog is not None:
            runlog.event("resolution_staged", {
                "signature": signature,
                "investigation_id": investigation_id,
            })

    # Serialized read-modify-write: two legs staging in the same
    # minute must both land, never overwrite each other (2026-10-21).
    try:
        with _locked(RESOLUTIONS_PATH):
            store = load_resolutions()
            store.setdefault("resolutions", []).append(entry)
            _write_json(RESOLUTIONS_PATH, store)
    except (OSError, json.JSONDecodeError):
        # Spool fallback: this context cannot write the shared store —
        # or cannot READ it: a store left momentarily empty/corrupt
        # makes load_resolutions() raise JSONDecodeError after its
        # retries, which the pre-2026-10-27 OSError-only guard let
        # escape, losing the entry outright (the failure shape of the
        # 2026-10-24/25/27 stage watchdog alerts). An unreadable store
        # is a write-path failure: spool the entry, never lose it.
        # Never lose the staged resolution — spool the exact entry; a
        # later writable invocation merges it (verified flag forced
        # False on merge, so spooling can never create a verified
        # resolution). If the spool itself fails, the original OSError
        # propagates — today's pre-spool behavior, never worse.
        _spool_write("stage", "", json.dumps({"entry": entry}, indent=2) + "\n",
                     ".json")
        entry = {**entry, "spooled": True}
        _witness_staged()
        return entry
    # Opportunistic drain of anything earlier contexts had to spool.
    merge_spool()
    _witness_staged()
    return entry


# --------------------------------------------------------------------------
# Run log (JSONL audit trail, one file per job per run date)
# --------------------------------------------------------------------------

class RunLog:
    def __init__(self, job_id: str, run_date: str | None = None):
        self.job_id = job_id
        self.run_date = run_date or _dt.date.today().isoformat()
        os.makedirs(RUNS_DIR, exist_ok=True)
        self.path = os.path.join(RUNS_DIR, f"{job_id}-{self.run_date}.jsonl")

    def event(self, event_type: str, payload: dict) -> dict:
        record = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "job_id": self.job_id,
            "run_date": self.run_date,
            "type": event_type,
            "payload": payload,
        }
        line = json.dumps(record, sort_keys=True) + "\n"
        try:
            _append_line(self.path, line)
        except OSError:
            # Spool fallback: never lose the record (and never fail a
            # leg whose business work succeeded) because this context
            # cannot append to the shared run log. One spool file per
            # (job, run date, writer pid) — only this process ever
            # appends to it, so no lock and no contention; merge_spool
            # appends the lines verbatim to the target later. If the
            # spool write itself fails, that OSError propagates —
            # pre-spool behavior, never worse.
            os.makedirs(SPOOL_DIR, exist_ok=True)
            spool_path = os.path.join(
                SPOOL_DIR,
                f"runlog-{self.job_id}-{self.run_date}-{os.getpid()}.jsonl")
            payload = json.dumps({"target": self.path, "line": line}) + "\n"
            data = payload.encode("utf-8")
            # Verified append (2026-10-27): this file is per-writer,
            # so an exact size delta proves the line landed whole.
            # Nothing landed -> bounded retry; a torn partial landing
            # -> raise OSError loudly rather than return a "spooled"
            # record whose payload is not intact on disk.
            landed = False
            for attempt in range(3):
                before = (os.path.getsize(spool_path)
                          if os.path.exists(spool_path) else 0)
                with open(spool_path, "a", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                after = os.path.getsize(spool_path)
                if after == before + len(data):
                    landed = True
                    break
                if after != before:
                    raise OSError(
                        f"runlog spool append to {spool_path} landed "
                        f"torn ({before} -> {after}, expected "
                        f"+{len(data)} bytes)")
                time.sleep(0.05 * (attempt + 1))
            if not landed:
                raise OSError(
                    f"runlog spool append to {spool_path} failed "
                    f"verification after 3 attempts")
            record = {**record, "spooled": True, "spool_path": spool_path}
            return record
        if event_type != "spool_merge":
            # Opportunistic drain (guarded against recursion: the merge
            # logs its own spool_merge event through this same method).
            merge_spool(self)
        return record

    def read_all(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


# --------------------------------------------------------------------------
# Step 1 — Predict
# --------------------------------------------------------------------------

def predict(runlog: RunLog, leg: str, name: str, expected,
            sigma: float | None = None, tolerance: float | None = None,
            kind: str = "numeric", source: str = "") -> dict:
    """Log a falsifiable expectation before acting. An unlogged expectation
    cannot generate surprise."""
    return runlog.event("predict", {
        "leg": leg,
        "name": name,
        "expected": expected,
        "sigma": sigma,
        "tolerance": tolerance,
        "kind": kind,
        "source": source,
    })


# --------------------------------------------------------------------------
# Step 2 — Compare (dual delta)
# --------------------------------------------------------------------------

def numeric_delta(observed: float, expected: float, sigma: float) -> float:
    """z-score surprise. sigma must be > 0 — a zero tolerance means any
    deviation is infinite surprise, which is never what you want; pass a
    real baseline deviation instead."""
    if sigma is None or sigma <= 0:
        raise ValueError(f"sigma must be > 0 (got {sigma})")
    return abs(observed - expected) / sigma


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tf_vector(text: str) -> dict[str, int]:
    vec: dict[str, int] = {}
    for tok in _TOKEN_RE.findall(text.lower()):
        vec[tok] = vec.get(tok, 0) + 1
    return vec


def semantic_divergence(a: str, b: str) -> float:
    """1 − cosine over term-frequency vectors. Lightweight proxy for the
    embedding cosine in the spec — honest about what it is: good for
    entity/keyword drift, not deep paraphrase. Returns 0.0 when either
    side is empty (no divergence measurable)."""
    va, vb = _tf_vector(a or ""), _tf_vector(b or "")
    if not va or not vb:
        return 0.0
    dot = sum(va[t] * vb.get(t, 0) for t in va)
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - dot / (na * nb)))


# --------------------------------------------------------------------------
# Step 3 — Gate (curiosity grant = Δ × relevance)
# --------------------------------------------------------------------------

def _cosine(va: dict[str, int], vb: dict[str, int]) -> float:
    if not va or not vb:
        return 0.0
    dot = sum(va[t] * vb.get(t, 0) for t in va)
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def relevance_score(text: str, manifest: dict) -> tuple[float, str | None]:
    """Max over manifest objectives of (cosine similarity × priority), with an
    entity-hit floor: matching 2+ of an objective's declared entity tokens
    sets a floor of 0.35 × priority. The entities list is the manifest's
    controlled vocabulary — two independent entity hits is deliberate
    signal, not noise. Never prompt vibes: the manifest is the only
    relevance source."""
    text_toks = set(_TOKEN_RE.findall((text or "").lower()))
    text_vec = _tf_vector(text or "")
    best, best_id = 0.0, None
    for obj in manifest.get("objectives", []):
        obj_text = " ".join(obj.get("entities", [])) + " " + obj.get("description", "")
        sim = _cosine(text_vec, _tf_vector(obj_text))
        score = sim * float(obj.get("priority", 0.5))
        entity_toks = set(_TOKEN_RE.findall(" ".join(obj.get("entities", [])).lower()))
        hits = len(text_toks & entity_toks)
        if hits >= 2:
            score = max(score, 0.35 * float(obj.get("priority", 0.5)))
        if score > best:
            best, best_id = score, obj.get("id")
    return (max(0.0, min(1.0, best)), best_id)


# At or above this z, the numeric signal stands alone: the relevance floor
# is bypassed. Below it, the floor still applies. (2026-10-13 patch: the
# floor vetoed real anomalies at z=4.80 and z=91.64 purely on worker
# phrasing — break-test 2026-10-13, log evidence.)
RELEVANCE_BYPASS_Z = 4.0


def gate_numeric(z: float, relevance: float, manifest: dict) -> dict:
    gates = manifest.get("gates", {})
    z_thresh = float(gates.get("numeric_z_threshold", 2.0))
    bypass = z >= RELEVANCE_BYPASS_Z and relevance < RELEVANCE_FLOOR
    fires = z > z_thresh and (bypass or relevance >= RELEVANCE_FLOOR)
    tier = "anomaly" if z > 3.0 else ("notable" if z > z_thresh else "none")
    reason = (
        f"z={z:.2f} (>{z_thresh}) × relevance={relevance:.2f} → "
        + ("INVESTIGATE" if fires else "drop (noisy TV)" if z > z_thresh else "no surprise")
    )
    if bypass and fires:
        reason += f" [relevance floor bypassed: z>={RELEVANCE_BYPASS_Z}]"
    return {"fires": fires, "grant": z * relevance, "tier": tier,
            "reason": reason, "relevance_bypass": bypass}


def gate_semantic(divergence: float, relevance: float, manifest: dict) -> dict:
    gates = manifest.get("gates", {})
    d_thresh = float(gates.get("semantic_divergence_threshold", 0.65))
    fires = divergence > d_thresh and relevance >= RELEVANCE_FLOOR
    reason = (
        f"div={divergence:.2f} (>{d_thresh}) × relevance={relevance:.2f} → "
        + ("INVESTIGATE" if fires else "drop (noisy TV)" if divergence > d_thresh else "no surprise")
    )
    return {"fires": fires, "grant": divergence * relevance,
            "tier": "anomaly" if fires else "none", "reason": reason}


def investigations_used(runlog: RunLog) -> int:
    return sum(1 for r in runlog.read_all() if r.get("type") == "investigation_opened")


def allowance_remaining(runlog: RunLog, manifest: dict) -> int:
    allowance = int(manifest.get("gates", {}).get("allowance_per_run", 3))
    return max(0, allowance - investigations_used(runlog))


# --------------------------------------------------------------------------
# Step 4 — Investigate (bounded; Resolved or Escalated; never silent)
# --------------------------------------------------------------------------

class Investigation:
    """Bounded investigation. Use as a context manager:

        with Investigation(runlog, manifest, signature="...") as inv:
            inv.step("checked X", evidence="...", op="read")
            ...
            inv.resolve(root_cause="...", evidence="...")

    Leaving the block without resolve()/escalate() auto-escalates —
    'gave up silently' is not a terminal state. Exceeding 5 steps or
    10 minutes raises BudgetExhausted and auto-escalates on exit.
    """

    def __init__(self, runlog: RunLog, manifest: dict, signature: str,
                 ttl_steps: int | None = None, ttl_minutes: int | None = None):
        gates = manifest.get("gates", {})
        self.ttl_steps = ttl_steps or int(gates.get("investigation_ttl_steps", 5))
        self.ttl_minutes = ttl_minutes or int(gates.get("investigation_ttl_minutes", 10))
        self.runlog = runlog
        self.manifest = manifest
        self.signature = signature
        self.steps: list[dict] = []
        self.terminal: str | None = None
        self.outcome: dict = {}
        self._t0 = time.monotonic()
        self.investigation_id = (
            f"inv-{runlog.run_date.replace('-', '')}-"
            f"{len(runlog.read_all()):04d}"
        )

    def _check_budget(self) -> None:
        elapsed_min = (time.monotonic() - self._t0) / 60.0
        if len(self.steps) >= self.ttl_steps:
            raise BudgetExhausted(
                f"step budget exhausted ({self.ttl_steps} steps)")
        if elapsed_min >= self.ttl_minutes:
            raise BudgetExhausted(
                f"time budget exhausted ({self.ttl_minutes} min)")

    def step(self, note: str, evidence: str | None = None, op: str = "read") -> dict:
        """One bounded trace step. op must be read/trace/stage — anything
        else is behavior alteration → KillConditionError."""
        if op not in ALLOWED_OPS:
            raise KillConditionError(
                f"investigation attempted op={op!r}; only {sorted(ALLOWED_OPS)} "
                f"are allowed (signature={self.signature})")
        self._check_budget()
        record = {"n": len(self.steps) + 1, "op": op, "note": note,
                  "evidence": evidence}
        self.steps.append(record)
        self.runlog.event("investigation_step",
                          {"investigation_id": self.investigation_id,
                           "signature": self.signature, **record})
        return record

    def resolve(self, root_cause: str, evidence: str,
                baseline_correction: dict | None = None) -> dict:
        if not evidence:
            raise KillConditionError(
                "resolve() requires evidence — a cause without evidence is "
                "an invented resolution")
        self.terminal = "resolved"
        self.outcome = {"root_cause": root_cause, "evidence": evidence,
                        "baseline_correction": baseline_correction or {}}
        staged = stage_resolution(
            signature=self.signature,
            context=f"investigation {self.investigation_id}",
            root_cause=root_cause,
            investigation_id=self.investigation_id,
            baseline_correction=baseline_correction,
            runlog=self.runlog)
        self.runlog.event("investigation_resolved",
                          {"investigation_id": self.investigation_id,
                           "signature": self.signature, **self.outcome})
        return staged

    def escalate(self, findings: str, checked: list[str],
                 unknown: list[str]) -> dict:
        self.terminal = "escalated"
        self.outcome = {"findings": findings, "checked": checked,
                        "unknown": unknown}
        self.runlog.event("investigation_escalated",
                          {"investigation_id": self.investigation_id,
                           "signature": self.signature, **self.outcome})
        return self.outcome

    def __enter__(self) -> "Investigation":
        if allowance_remaining(self.runlog, self.manifest) <= 0:
            raise BudgetExhausted("per-run investigation allowance exhausted")
        self.runlog.event("investigation_opened",
                          {"investigation_id": self.investigation_id,
                           "signature": self.signature,
                           "ttl_steps": self.ttl_steps,
                           "ttl_minutes": self.ttl_minutes})
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.terminal is None:
            # Never leave silently.
            why = (f"{exc_type.__name__}: {exc}" if exc_type
                   else "exited without resolve() or escalate()")
            self.escalate(
                findings=f"Investigation {self.investigation_id} did not "
                         f"reach a terminal state: {why}.",
                checked=[s["note"] for s in self.steps],
                unknown=["root cause — investigation ended before terminal state"])
        return False  # do not swallow exceptions


# --------------------------------------------------------------------------
# Kill-condition checks over a run log
# --------------------------------------------------------------------------

def check_violations(runlog: RunLog) -> list[dict]:
    """Scan a run's audit trail for kill-condition breaches:

    0. Missing receipts — the run log itself is missing or empty.
    1. Hallucinated data — an observation logged with no evidence/source.
    2. Invented resolutions — a staged resolution with no investigation id.
    3. Behavior alteration — any investigation step with op outside
       {read, trace, stage}.

    FAIL CLOSED (2026-10-31): a leg that ran leaves a non-empty run
    log. A missing or 0-byte log means the receipts were eaten (the
    2026-10-29/30 failure class) — scanning it would return an empty
    violations list that reads as "clean", so the absence itself is
    reported as a violation instead of passing silently.

    SCOPE HONESTY (2026-10-31): these are STRUCTURAL checks. They
    verify the presence and shape of the fields a real investigation
    leaves behind — an observation carries evidence/source text, a
    staged resolution names an investigation id, a step's op is in
    the allowed set. They do NOT prove the evidence is true, that an
    observation wasn't fabricated with plausible text, or that a
    root cause is correct. Semantic truth is established by
    the principal's sign-off and independent cross-checks, not by
    this scan.
    """
    violations: list[dict] = []
    if not os.path.exists(runlog.path) or os.path.getsize(runlog.path) <= 0:
        violations.append({
            "kind": "runlog_missing_or_empty",
            "detail": f"run log {runlog.path} is missing or empty — "
                      f"the leg's receipts did not survive, so a clean "
                      f"scan cannot be trusted (fail closed)",
            "ts": None,
        })
    for record in runlog.read_all():
        p = record.get("payload", {})
        rtype = record.get("type")
        if rtype == "observed":
            if not p.get("evidence") and not p.get("source"):
                violations.append({
                    "kind": "hallucinated_data",
                    "detail": f"observation {p.get('name')!r} logged with no "
                              f"evidence or source",
                    "ts": record.get("ts"),
                })
        elif rtype == "investigation_step":
            if p.get("op") not in ALLOWED_OPS:
                violations.append({
                    "kind": "behavior_alteration",
                    "detail": f"step op={p.get('op')!r} outside {sorted(ALLOWED_OPS)}",
                    "ts": record.get("ts"),
                })
        elif rtype == "resolution_staged" and not p.get("investigation_id"):
            violations.append({
                "kind": "invented_resolution",
                "detail": f"resolution {p.get('signature')!r} staged with no "
                          f"investigation id",
                "ts": record.get("ts"),
            })
    return violations


def enforce_no_violations(runlog: RunLog) -> None:
    violations = check_violations(runlog)
    if violations:
        raise KillConditionError(
            "kill-condition breach:\n"
            + "\n".join(f"- [{v['kind']}] {v['detail']}" for v in violations))


# --------------------------------------------------------------------------
# Tier 0 — Freshness gate (2026-10-13 patch)
#
# The manifest's #1 declared failure mode is a clean report on stale data
# (obj_service_reporting_accuracy: stale_report_max = 0), but until this
# patch no code path measured freshness — observe() had no timestamp input,
# so stale data wearing plausible numbers passed at z=0.00 (break-test
# D12). Freshness is now a hard invariant checked BEFORE numeric
# evaluation, and it is FAIL-CLOSED: when the resolved objective's
# tolerances set requires_freshness=true, a missing or unparseable
# "Last Updated" stamp is a breach, never a silent pass.
# --------------------------------------------------------------------------

_NY_TZ = ZoneInfo("America/New_York")

# vendor prose stamps, e.g. "Oct 13 2026 4:52 AM" / "October 13, 2026
# 4:52:30 AM". Tried after ISO 8601.
_PROSE_TS_FORMATS = (
    "%b %d %Y %I:%M %p", "%b %d, %Y %I:%M %p",
    "%B %d %Y %I:%M %p", "%B %d, %Y %I:%M %p",
    "%b %d %Y %I:%M:%S %p", "%b %d, %Y %I:%M:%S %p",
    "%B %d %Y %I:%M:%S %p", "%B %d, %Y %I:%M:%S %p",
    "%b %d %Y %H:%M", "%b %d, %Y %H:%M",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M",
)


# A trailing timezone abbreviation (e.g. "2026-10-15 04:47:08 EDT", seen on
# the CSI leg 2026-10-15) parses under neither fromisoformat nor the prose
# formats. The abbreviation adds nothing the naive-as-New-York rule in
# _parse_data_timestamp doesn't already supply, so a known one is stripped
# before parsing. Closed list on purpose: an unrecognized tail still fails
# closed as UNPARSEABLE rather than being silently mangled.
_TZ_ABBREV_TAIL = re.compile(
    r"\s+(?:UTC|GMT|EST|EDT|CST|CDT|MST|MDT|PST|PDT|AKST|AKDT|HST|HDT)$")


def _parse_data_timestamp(raw) -> _dt.datetime | None:
    """Parse a report 'Last Updated' stamp into an aware UTC datetime.

    Accepts ISO 8601 and vendor prose formats. Naive stamps are read as
    America/New_York (the operator's timezone — the stamps are rendered
    by the source system in local time). Returns None when unparseable.

    WARNING (2026-10-31): the naive-as-New-York rule is an assumption
    about the SOURCE, not a property of the stamp. It holds for
    the source system, which renders in local time. A future source
    that renders naive stamps in UTC would be silently misread by
    4–5 hours and its freshness age misjudged — and no breach would
    fire, because the stamp parses cleanly and merely lands at the
    wrong instant. Any new stamp source must be checked against
    this assumption before its metrics are trusted to the freshness
    gate."""
    if raw is None:
        return None
    if isinstance(raw, _dt.datetime):
        dt = raw
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        s = _TZ_ABBREV_TAIL.sub("", s)
        dt = None
        try:
            dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            for fmt in _PROSE_TS_FORMATS:
                try:
                    dt = _dt.datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_NY_TZ)
    return dt.astimezone(_dt.timezone.utc)


def _objective_by_id(manifest: dict, objective_id: str | None) -> dict | None:
    for obj in manifest.get("objectives", []):
        if obj.get("id") == objective_id:
            return obj
    return None


def _freshness_gate(manifest: dict, objective_id: str | None,
                    data_timestamp, now: _dt.datetime | None = None,
                    proof_mode: str = "report_stamp",
                    population_complete: bool = False):
    """Tier 0 freshness check. Returns (info, breach_verdict_or_None).

    info is always a dict describing the freshness state (attached to the
    observed/gate events as an additive "freshness" object). A breach
    verdict has the shape {fires, reason, z: 99.0, relevance: 1.0, ...}
    and short-circuits numeric evaluation in observe(). Objectives whose
    tolerances do not set requires_freshness are unaffected."""
    info: dict = {"required": False, "status": "not_required",
                  "data_timestamp": data_timestamp,
                  "proof_mode": proof_mode}
    obj = _objective_by_id(manifest, objective_id)
    tol = (obj or {}).get("tolerances", {}) or {}
    if not tol.get("requires_freshness"):
        return info, None
    info["required"] = True
    info["objective"] = objective_id
    info["max_age_hours"] = tol.get("max_age_hours")

    def _breach(code: str, detail: str) -> dict:
        return {"fires": True, "z": 99.0, "relevance": 1.0,
                "grant": 99.0, "tier": "anomaly",
                "reason": f"{code}: {detail}"}

    if proof_mode == "live_read":
        # Live-read proof (2026-10-14, the principal: fix it correctly, not
        # an ignore). The source prints no stamp because the read is live
        # against the system of record. The proof obligation changes
        # shape, it does not shrink: the worker must supply the retrieval
        # timestamp (page/system clock at the moment of the read) AND
        # assert population completeness. Either missing = breach.
        info["population_complete"] = bool(population_complete)
        if data_timestamp is None or (isinstance(data_timestamp, str)
                                       and not data_timestamp.strip()):
            info["status"] = "missing"
            return info, _breach(
                "MISSING_LIVE_READ_RETRIEVAL",
                f"objective {objective_id} metric is classified live_read; "
                f"no retrieval timestamp supplied — fail-closed, a live "
                f"read must state when it happened")
        parsed_lr = _parse_data_timestamp(data_timestamp)
        if parsed_lr is None:
            info["status"] = "unparseable"
            return info, _breach(
                "UNPARSEABLE_TIMESTAMP",
                f"retrieval stamp {data_timestamp!r} matches no known "
                f"format (ISO 8601 or vendor prose) — fail-closed")
        now_lr = now or _dt.datetime.now(_dt.timezone.utc)
        age_lr = (now_lr - parsed_lr).total_seconds() / 3600.0
        info["parsed_utc"] = parsed_lr.isoformat()
        info["age_hours"] = round(age_lr, 3)
        max_age_lr = tol.get("max_age_hours")
        if max_age_lr is not None and age_lr > float(max_age_lr):
            info["status"] = "stale"
            return info, _breach(
                "STALE_DATA_BREACH",
                f"retrieval stamp {data_timestamp!r} is {age_lr:.2f}h old; "
                f"a live read for objective {objective_id} must be from "
                f"this run (at most {float(max_age_lr)}h)")
        if not population_complete:
            info["status"] = "population_unverified"
            return info, _breach(
                "LIVE_READ_POPULATION_UNVERIFIED",
                f"live_read proof for objective {objective_id} requires "
                f"population completeness — every expected record present "
                f"in the pull, expected zeros included; not asserted — "
                f"fail-closed")
        info["status"] = "ok"
        return info, None

    if data_timestamp is None or (isinstance(data_timestamp, str)
                                   and not data_timestamp.strip()):
        info["status"] = "missing"
        return info, _breach(
            "MISSING_MANDATORY_TIMESTAMP",
            f"objective {objective_id} requires a report Last Updated "
            f"stamp; none supplied — fail-closed, freshness cannot be "
            f"assumed")
    parsed = _parse_data_timestamp(data_timestamp)
    if parsed is None:
        info["status"] = "unparseable"
        return info, _breach(
            "UNPARSEABLE_TIMESTAMP",
            f"stamp {data_timestamp!r} matches no known format (ISO 8601 "
            f"or vendor prose) — fail-closed")
    now = now or _dt.datetime.now(_dt.timezone.utc)
    age_hours = (now - parsed).total_seconds() / 3600.0
    info["parsed_utc"] = parsed.isoformat()
    info["age_hours"] = round(age_hours, 3)
    if proof_mode == "nightly_cadence":
        # Nightly-cadence proof (2026-10-15, TECH_UTIL_NIGHTLY_STAMP_CADENCE
        # sign-off): stamp present and parseable (fail-closed checks above
        # already ran); only the age bound changes, to the manifest's
        # cadence bound. A stamp older than that means last night's batch
        # did not land — still a breach.
        info["cadence"] = "nightly"
        cadence_max = float((manifest.get("freshness_proofs", {}) or {}).get(
            "cadence_max_age_hours", 26.0))
        info["cadence_max_age_hours"] = cadence_max
        if age_hours > cadence_max:
            info["status"] = "stale"
            return info, _breach(
                "STALE_DATA_BREACH",
                f"stamp {data_timestamp!r} is {age_hours:.2f}h old; "
                f"nightly-cadence proof allows at most {cadence_max}h "
                f"(last night's batch must have landed)")
        info["status"] = "ok"
        return info, None
    max_age = tol.get("max_age_hours")
    if max_age is not None and age_hours > float(max_age):
        info["status"] = "stale"
        return info, _breach(
            "STALE_DATA_BREACH",
            f"stamp {data_timestamp!r} is {age_hours:.2f}h old; objective "
            f"{objective_id} allows at most {float(max_age)}h")
    info["status"] = "ok"
    return info, None


# --------------------------------------------------------------------------
# Observe: compare + gate in one call (what cron workers use per checkpoint)
# --------------------------------------------------------------------------

def observe(runlog: RunLog, manifest: dict, name: str, observed: float,
            expected: float, sigma: float, context: str = "",
            evidence: str = "", source: str = "",
            data_timestamp=None, population_complete: bool = False,
            _observed_extra: dict | None = None) -> dict:
    """Log an observation, run the Tier 0 freshness gate, compute the
    numeric delta, score relevance, run the gate, and log the decision.
    Returns the gate verdict.

    data_timestamp: the source report's 'Last Updated' stamp as read by
    the worker (ISO 8601 or vendor prose). When the resolved objective
    requires freshness, a missing/unparseable/stale stamp short-circuits
    to a breach verdict BEFORE any numeric evaluation."""
    relevance, objective_id = relevance_score(f"{name} {context}", manifest)
    # Deterministic metric->objective routing for the freshness gate.
    # Text relevance decides what a metric is "about," but freshness
    # strictness must not depend on how a worker phrased the context
    # (break-test D12e, 2026-10-13: cp_ro_count routed to the 72h
    # accounting objective and a stale stamp passed). The manifest's
    # metric_objectives map wins for freshness: exact name match first,
    # then longest "<key>_" prefix (dated / weekday name variants).
    freshness_objective_id = objective_id
    metric_map = manifest.get("metric_objectives", {}) or {}
    if name in metric_map:
        freshness_objective_id = metric_map[name]
    else:
        for key in sorted(metric_map, key=len, reverse=True):
            if name.startswith(key + "_"):
                freshness_objective_id = metric_map[key]
                break
    # Freshness proof mode: metrics a leg declares live_read (source
    # prints no stamp; live system-of-record read) take the live-read
    # proof bundle; everything else takes the printed-stamp proof.
    proof_mode = "report_stamp"
    for _p in _live_read_prefixes(getattr(runlog, "job_id", "")):
        if name == _p or name.startswith(_p + "_"):
            proof_mode = "live_read"
            break
    if proof_mode == "report_stamp" and _cadence_prefixes(
            getattr(runlog, "job_id", "")):
        # Leg-level cadence proof (2026-10-16 fix). A leg that declares
        # cadence_metrics runs on a nightly-batch source; that is a
        # property of the LEG's source, not of one metric — so the
        # cadence treatment applies to ALL of the leg's observes,
        # business and auxiliary alike. The 2026-10-16 incident:
        # the tech-utilization leg's auxiliary write_readback observe (a
        # same-run verification carrying the same report's stamp) fell
        # through to the strict 4h report_stamp rule and fired a false
        # STALE_DATA_BREACH at 5.81h (after first firing
        # MISSING_MANDATORY_TIMESTAMP with no stamp), while the leg's
        # business metric passed under its verified cadence proof.
        # Fail-closed is unchanged — _freshness_gate still requires
        # the stamp, present and parseable; only the age bound swaps
        # to cadence_max_age_hours. Legs with no cadence declaration
        # keep the strict default.
        proof_mode = "nightly_cadence"
    freshness, breach = _freshness_gate(
        manifest, freshness_objective_id, data_timestamp,
        proof_mode=proof_mode, population_complete=population_complete)
    observed_payload = {
        "name": name, "observed": observed, "expected": expected,
        "sigma": sigma, "context": context,
        "evidence": evidence, "source": source,
        "freshness": freshness,
    }
    if _observed_extra:
        observed_payload.update(_observed_extra)
    if breach is not None:
        observed_payload["z"] = None
        runlog.event("observed", observed_payload)
        runlog.event("gate_decision", {
            "name": name, "z": breach["z"],
            "relevance": breach["relevance"], "objective": objective_id,
            **{k: v for k, v in breach.items()
               if k not in ("reason", "z", "relevance")},
            "reason": breach["reason"],
            "freshness": freshness,
            "allowance_remaining": allowance_remaining(runlog, manifest),
        })
        return breach
    z = numeric_delta(observed, expected, sigma)
    verdict = gate_numeric(z, relevance, manifest)
    # Slow-drift check (v1.2 D13 design; see the drift section above).
    # Runs only when the per-day numeric gate is quiet — drift is the
    # signal the rolling anchor structurally misses. Delta-mode inner
    # calls are excluded here: cumulative metrics take the trajectory
    # check in observe_delta(), never a median of levels.
    drift = None
    if not verdict["fires"] and not (
            _observed_extra and _observed_extra.get("metric_mode") == "delta"):
        drift = _drift_median_anchor(
            manifest, getattr(runlog, "job_id", ""), name, observed, sigma,
            exclude_path=runlog.path, today=runlog.run_date)
    if drift is not None:
        observed_payload["drift"] = drift
    observed_payload["z"] = round(z, 3)
    runlog.event("observed", observed_payload)
    runlog.event("gate_decision", {
        "name": name, "z": round(z, 3),
        "relevance": round(relevance, 3), "objective": objective_id,
        **{k: v for k, v in verdict.items() if k != "reason"},
        "reason": verdict["reason"],
        "freshness": freshness,
        "allowance_remaining": allowance_remaining(runlog, manifest),
    })
    if drift is not None and drift["fires"]:
        dverdict = {
            "fires": True, "z": drift["z"], "relevance": relevance,
            "grant": drift["z"] * max(relevance, RELEVANCE_FLOOR),
            "tier": "anomaly" if drift["z"] > 3.0 else "notable",
            "reason": (
                f"SLOW_DRIFT_MEDIAN_ANCHOR: observed {observed} vs "
                f"{DRIFT_WINDOW_DAYS}-day median anchor {drift['anchor']} "
                f"over {drift['days']} prior days (z={drift['z']:.2f} "
                f">{float(manifest.get('gates', {}).get('numeric_z_threshold', 2.0))}) "
                f"— per-day gate was quiet; accumulated drift against "
                f"the held anchor fires"),
            "drift": drift,
        }
        runlog.event("gate_decision", {
            "name": name, "z": drift["z"],
            "relevance": round(relevance, 3), "objective": objective_id,
            **{k: v for k, v in dverdict.items() if k != "reason"},
            "reason": dverdict["reason"],
            "freshness": freshness,
            "allowance_remaining": allowance_remaining(runlog, manifest),
        })
        return dverdict
    return verdict


# --------------------------------------------------------------------------
# Delta observe: gate the daily increment of a cumulative metric
# --------------------------------------------------------------------------

def _level_history(job_id: str, name: str,
                   exclude_path: str | None = None) -> list[dict]:
    """All logged LEVELs for metric `name` belonging to job `job_id`,
    oldest first: [{value, run_date, ts, path}]. Plain observe() events
    store the level under 'observed'; delta-mode events store it under
    'level_observed' (their 'observed' holds the increment).

    Scoped per (job, metric) — 2026-10-16 fix for
    DELTA_BASELINE_CROSS_LEG_COLLISION. Records are matched on the
    record's own job_id AND the runs-file name prefix
    (<job>-<date>.jsonl, belt and braces). A metric NAME is not a
    population: the advisor-performance leg's mtd_ro_total and
    the tech-utilization leg's mtd_ro_total are different counters, and the
    old global-by-name lookup let one leg anchor to the other's level —
    live false fire 2026-10-16 (advisor-performance gated increment 164
    against tech-utilization's prior 125; z=20.86, where its own prior
    271 gives increment +18, z=0). The current run's own log file is
    excluded — a run must not anchor to itself."""
    if not job_id or not os.path.isdir(RUNS_DIR):
        return []
    out: list[dict] = []
    prefix = f"{job_id}-"
    for fname in sorted(os.listdir(RUNS_DIR)):
        if not fname.endswith(".jsonl") or not fname.startswith(prefix):
            continue
        path = os.path.join(RUNS_DIR, fname)
        if exclude_path and os.path.abspath(path) == os.path.abspath(exclude_path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") != "observed":
                continue
            if rec.get("job_id") != job_id:
                continue
            p = rec.get("payload", {})
            if p.get("name") != name:
                continue
            value = p.get("level_observed", p.get("observed"))
            if not isinstance(value, (int, float)):
                continue
            out.append({"value": value, "run_date": rec.get("run_date", ""),
                        "ts": rec.get("ts", ""), "path": path})
    out.sort(key=lambda r: (r["run_date"], r["ts"]))
    return out


def _prior_observed(name: str, exclude_path: str | None = None,
                    job_id: str | None = None) -> dict | None:
    """Most recent logged LEVEL for metric `name` in job `job_id`'s own
    run logs (see _level_history for the per-job scoping rule and why).
    Returns {value, run_date, ts, path} or None."""
    hist = _level_history(job_id or "", name, exclude_path=exclude_path)
    if not hist:
        return None
    last = hist[-1]
    return {"value": last["value"], "run_date": last["run_date"],
            "ts": last["ts"], "path": last["path"]}


# --------------------------------------------------------------------------
# Slow-drift detection (v1.2 D13 design, agreed 2026-10-13; built
# 2026-10-16). The per-day gate is stateless: a metric creeping +1/day
# under a rolling anchor never accumulates surprise (break-test D13 —
# cumulative z=3.50 against the day-0 baseline was simply never
# computed). The loop now holds the anchor itself, from the job's own
# logged history:
#   * stationary metrics — a 14-day MEDIAN anchor (observe()): the
#     median of the trailing window is robust, so a week of drift
#     barely moves it and the accumulated drift shows against it.
#   * cumulative metrics — a TRAJECTORY band (observe_delta()):
#     expected level = earliest in-window level + expected daily
#     increment x elapsed calendar days; a trailing median of levels
#     is meaningless for a counter that only grows.
# Both checks speak only with enough history, and both record their
# evidence on the observed event (additive "drift" object) whether or
# not they fire. A firing drift verdict is its own tier, like the
# Tier 0 freshness breach: it is computed from the loop's own held
# history, not from worker phrasing, so the relevance floor does not
# veto it.
# --------------------------------------------------------------------------

DRIFT_WINDOW_DAYS = 14
DRIFT_MIN_DAYS = 5        # stationary: distinct prior days required
DRIFT_MIN_POINTS = 3      # trajectory: prior logged points required
DRIFT_MIN_SPAN_DAYS = 2   # trajectory: anchor must be at least this old


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def _metric_kind(job_id: str, name: str) -> str | None:
    """Declared expectation kind for (job, metric name) from
    leg_expectations.json — exact key match, then key+'_' prefix match
    (the same convention as the proof-mode prefix lists). None when the
    leg declares nothing for the metric."""
    try:
        legs = _read_json(EXPECTATIONS_PATH).get("legs", {}) or {}
    except Exception:
        return None
    exps = (legs.get(job_id, {}) or {}).get("expectations", []) or []
    for e in exps:
        if e.get("key") == name:
            return e.get("kind")
    for e in exps:
        k = e.get("key") or ""
        if k and name.startswith(k + "_"):
            return e.get("kind")
    return None


def _drift_window(hist: list[dict], today: str | None) -> list[dict]:
    """History points inside the trailing DRIFT_WINDOW_DAYS of `today`
    (by run_date). Points with unparseable dates are dropped."""
    if not today:
        return []
    try:
        today_d = _dt.date.fromisoformat(today)
    except (ValueError, TypeError):
        return []
    out = []
    for h in hist:
        try:
            d = _dt.date.fromisoformat(h.get("run_date", ""))
        except (ValueError, TypeError):
            continue
        if 0 <= (today_d - d).days <= DRIFT_WINDOW_DAYS:
            out.append(h)
    return out


def _drift_median_anchor(manifest: dict, job_id: str, name: str,
                         observed: float, sigma: float,
                         exclude_path: str | None = None,
                         today: str | None = None) -> dict | None:
    """Stationary drift check: observed vs the median of the metric's
    own logged levels (this job only, last level per day) over the
    trailing DRIFT_WINDOW_DAYS. Returns a drift-info dict, or None
    when the check does not speak: metric declared kind='delta' for
    this job (cumulative — the trajectory check owns it), or fewer
    than DRIFT_MIN_DAYS distinct prior days in the window."""
    if _metric_kind(job_id, name) == "delta":
        return None
    hist = _drift_window(
        _level_history(job_id, name, exclude_path=exclude_path), today)
    by_day: dict[str, float] = {}
    for h in hist:  # oldest first — later writes that day win
        by_day[h["run_date"]] = h["value"]
    if len(by_day) < DRIFT_MIN_DAYS:
        return None
    anchor = _median(list(by_day.values()))
    z = abs(observed - anchor) / sigma
    z_thresh = float(manifest.get("gates", {}).get("numeric_z_threshold", 2.0))
    return {"mode": "median_anchor", "anchor": anchor,
            "days": len(by_day), "window_days": DRIFT_WINDOW_DAYS,
            "z": round(z, 3), "fires": z > z_thresh}


def _drift_trajectory(manifest: dict, job_id: str, name: str,
                      level_today: float, delta_expected: float,
                      delta_sigma: float,
                      exclude_path: str | None = None,
                      today: str | None = None) -> dict | None:
    """Cumulative drift check (trajectory band): expected level today
    = earliest in-window logged level + delta_expected x calendar days
    elapsed since that anchor. Returns a drift-info dict, or None when
    history is too thin (< DRIFT_MIN_POINTS points in the window, or
    the anchor is < DRIFT_MIN_SPAN_DAYS old) — thin evidence is not a
    pass and not a fire; the check simply does not speak."""
    hist = _drift_window(
        _level_history(job_id, name, exclude_path=exclude_path), today)
    if len(hist) < DRIFT_MIN_POINTS or not today:
        return None
    anchor = hist[0]
    try:
        elapsed = (_dt.date.fromisoformat(today)
                   - _dt.date.fromisoformat(anchor["run_date"])).days
    except (ValueError, TypeError):
        return None
    if elapsed < DRIFT_MIN_SPAN_DAYS:
        return None
    expected_level = anchor["value"] + delta_expected * elapsed
    deviation = level_today - expected_level
    z = abs(deviation) / delta_sigma
    z_thresh = float(manifest.get("gates", {}).get("numeric_z_threshold", 2.0))
    return {"mode": "trajectory", "anchor_level": anchor["value"],
            "anchor_run_date": anchor["run_date"], "elapsed_days": elapsed,
            "expected_level": expected_level, "deviation": deviation,
            "points": len(hist), "z": round(z, 3), "fires": z > z_thresh}


def observe_delta(runlog: RunLog, manifest: dict, name: str, observed: float,
                  delta_expected: float, delta_sigma: float, context: str = "",
                  evidence: str = "", source: str = "", data_timestamp=None,
                  population_complete: bool = False,
                  level_expected: float | None = None,
                  level_sigma: float | None = None) -> dict:
    """Observe a CUMULATIVE metric (MTD totals, G5 counts) by its daily
    increment instead of its level. Anchoring a level to yesterday's
    level guarantees Δ = a full day's volume every day, so the level gate
    false-fires structurally (break-test B8/B8b — B8b fired live at the
    already-'calibrated' σ=12). The increment, gated against a volume
    baseline, is the honest signal.

    Prior level = most recent logged level for `name` in THIS JOB's
    own run logs, excluding the current run's log file (per-job
    scoping, 2026-10-16 — see _level_history). Fallbacks when no prior
    exists (both are logged as 'delta_fallback' events):
      * level_expected/level_sigma supplied → observe the LEVEL once
        against that baseline; the logged level seeds tomorrow's prior.
      * otherwise → record the level as a seed ('observed' event with
        metric_mode='delta_seed', no gate evaluation) and return a
        non-firing verdict; tomorrow's run will have a prior.
    The freshness gate applies unchanged (via observe())."""
    prior = _prior_observed(name, exclude_path=runlog.path,
                            job_id=runlog.job_id)
    if prior is None:
        runlog.event("delta_fallback", {
            "name": name, "observed": observed,
            "reason": "no prior logged level for this metric in this "
                      "job's own run logs (excluding this run's own log)",
        })
        if level_expected is not None and level_sigma is not None:
            return observe(runlog, manifest, name, observed, level_expected,
                           level_sigma, context, evidence, source,
                           data_timestamp=data_timestamp,
                           population_complete=population_complete)
        runlog.event("observed", {
            "name": name, "observed": observed, "expected": None,
            "sigma": None, "z": None, "context": context,
            "evidence": evidence, "source": source,
            "level_observed": observed, "metric_mode": "delta_seed",
        })
        return {"fires": False, "grant": 0.0, "tier": "none",
                "reason": "delta unavailable: no prior logged level — "
                          "level recorded as seed, no gate verdict"}
    delta = observed - prior["value"]
    ctx = ((context + " ") if context else "") + (
        f"[delta mode: level {observed} − prior level {prior['value']} "
        f"({prior['run_date']}) = increment {delta}]")
    # Trajectory-band drift check (v1.2 D13 design; see the drift
    # section above): a cumulative level creeping a little every day
    # produces a constant, unremarkable increment — the increment gate
    # cannot see the creep, but the level's deviation from its expected
    # trajectory can. Evaluated from history BEFORE this call logs, so
    # today's level is not its own anchor.
    traj = _drift_trajectory(manifest, runlog.job_id, name, observed,
                             delta_expected, delta_sigma,
                             exclude_path=runlog.path,
                             today=runlog.run_date)
    extra = {
        "metric_mode": "delta",
        "level_observed": observed,
        "prior_level": prior["value"],
        "prior_run_date": prior["run_date"],
    }
    if traj is not None:
        extra["drift"] = traj
    verdict = observe(
        runlog, manifest, name, delta, delta_expected, delta_sigma, ctx,
        evidence, source, data_timestamp=data_timestamp,
        population_complete=population_complete,
        _observed_extra=extra)
    if traj is not None and traj["fires"] and not verdict["fires"]:
        dverdict = {
            "fires": True, "z": traj["z"],
            "relevance": verdict.get("relevance", 1.0),
            "grant": verdict.get("grant", 0.0),
            "tier": "anomaly" if traj["z"] > 3.0 else "notable",
            "reason": (
                f"SLOW_DRIFT_TRAJECTORY: level {observed} vs expected "
                f"trajectory {traj['expected_level']} (anchor "
                f"{traj['anchor_level']} on {traj['anchor_run_date']} + "
                f"{delta_expected}/day x {traj['elapsed_days']}d; "
                f"deviation {traj['deviation']}, z={traj['z']:.2f}) — "
                f"daily increments looked normal; the level has "
                f"drifted off its trajectory band"),
            "drift": traj,
        }
        runlog.event("gate_decision", {
            "name": name, "z": traj["z"],
            "relevance": verdict.get("relevance"),
            "objective": verdict.get("objective"),
            **{k: v for k, v in dverdict.items()
               if k not in ("reason", "z", "relevance")},
            "reason": dverdict["reason"],
            "allowance_remaining": allowance_remaining(runlog, manifest),
        })
        return dverdict
    return verdict


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _utc_today() -> str:
    return _dt.date.today().isoformat()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Curiosity Loop core CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("predict", help="log an expectation")
    p.add_argument("--job", required=True)
    p.add_argument("--leg", default="")
    p.add_argument("--name", required=True)
    p.add_argument("--expected", required=True)
    p.add_argument("--sigma", type=float, default=None)
    p.add_argument("--tolerance", type=float, default=None)
    p.add_argument("--kind", default="numeric")
    p.add_argument("--source", default="")
    p.add_argument("--date", default=None)

    o = sub.add_parser("observe", help="log an observation and run compare+gate")
    o.add_argument("--job", required=True)
    o.add_argument("--name", required=True)
    o.add_argument("--observed", type=float, required=True)
    o.add_argument("--expected", type=float, required=True)
    o.add_argument("--sigma", type=float, required=True)
    o.add_argument("--context", default="")
    o.add_argument("--evidence", default="")
    o.add_argument("--source", default="")
    o.add_argument("--data-timestamp", default=None,
                   help="source report's Last Updated stamp (ISO 8601 or "
                        "vendor prose); mandatory when the metric's "
                        "objective requires freshness")
    o.add_argument("--population-complete", action="store_true",
                   help="live_read metrics only: assert every expected "
                        "record was present in the pull (expected zeros "
                        "included); required live-read proof alongside "
                        "--data-timestamp as the retrieval time")
    o.add_argument("--date", default=None)

    d = sub.add_parser("observe-delta",
                       help="observe a cumulative metric by daily increment")
    d.add_argument("--job", required=True)
    d.add_argument("--name", required=True)
    d.add_argument("--observed", type=float, required=True,
                   help="today's cumulative LEVEL")
    d.add_argument("--delta-expected", type=float, required=True)
    d.add_argument("--delta-sigma", type=float, required=True)
    d.add_argument("--context", default="")
    d.add_argument("--evidence", default="")
    d.add_argument("--source", default="")
    d.add_argument("--data-timestamp", default=None)
    d.add_argument("--population-complete", action="store_true")
    d.add_argument("--level-expected", type=float, default=None,
                   help="fallback level baseline when no prior is logged")
    d.add_argument("--level-sigma", type=float, default=None)
    d.add_argument("--date", default=None)

    s = sub.add_parser("stage", help="stage a learned resolution (unverified)")
    s.add_argument("--signature", required=True)
    s.add_argument("--context", required=True)
    s.add_argument("--root-cause", required=True)
    s.add_argument("--investigation", default=None,
                   help="investigation id behind this resolution; REQUIRED "
                        "(2026-10-31) — staging without one is refused")
    s.add_argument("--correction", default="{}")
    s.add_argument("--job", default=None,
                   help="job whose run log records the resolution_staged "
                        "witness event (optional; pass your leg's job id)")

    c = sub.add_parser("check", help="kill-condition check over a run log")
    c.add_argument("--job", required=True)
    c.add_argument("--date", default=None)

    m = sub.add_parser("spool",
                       help="drain spooled records into the primary stores")
    m.add_argument("--job", default=None,
                   help="job whose run log witnesses the drain (optional)")

    l = sub.add_parser("expectations", help="print a leg's expectations")
    l.add_argument("--leg", required=True)

    args = ap.parse_args(argv)
    manifest = load_manifest()

    if args.cmd == "predict":
        rl = RunLog(args.job, args.date or _utc_today())
        try:
            expected: object = float(args.expected)
        except ValueError:
            expected = args.expected
        rec = predict(rl, args.leg, args.name, expected, args.sigma,
                      args.tolerance, args.kind, args.source)
        print(json.dumps(rec["payload"], indent=2))

    elif args.cmd == "observe":
        rl = RunLog(args.job, args.date or _utc_today())
        verdict = observe(rl, manifest, args.name, args.observed,
                          args.expected, args.sigma, args.context,
                          args.evidence, args.source,
                          data_timestamp=args.data_timestamp,
                          population_complete=args.population_complete)
        print(json.dumps(verdict, indent=2))
        # Non-zero exit when the gate fires so workers can branch on it.
        return 10 if verdict["fires"] else 0

    elif args.cmd == "observe-delta":
        rl = RunLog(args.job, args.date or _utc_today())
        verdict = observe_delta(rl, manifest, args.name, args.observed,
                                args.delta_expected, args.delta_sigma,
                                args.context, args.evidence, args.source,
                                data_timestamp=args.data_timestamp,
                                population_complete=args.population_complete,
                                level_expected=args.level_expected,
                                level_sigma=args.level_sigma)
        print(json.dumps(verdict, indent=2))
        return 10 if verdict["fires"] else 0

    elif args.cmd == "stage":
        # Kill-condition enforcement (2026-10-31): a resolution staged
        # with no investigation behind it is an invented resolution.
        # The library path records what it is given so check_violations
        # can flag it; the CLI — the path legs actually invoke — refuses
        # outright, before anything is written.
        if not args.investigation or not str(args.investigation).strip():
            print("error: stage requires --investigation <id>: a "
                  "resolution staged with no investigation behind it is "
                  "an invented resolution (kill condition)",
                  file=sys.stderr)
            return 2
        # --correction is JSON by contract, but legs pass plain-prose
        # rationale strings in practice; a prose value must never kill
        # the stage with an unhandled JSONDecodeError (the failure
        # shape of the 2026-10-24/25/27 watchdog alerts). Fall back to
        # a wrapped note — the dict shape stage_resolution expects.
        try:
            correction_payload = json.loads(args.correction)
        except (json.JSONDecodeError, TypeError):
            correction_payload = ({"note": args.correction}
                                  if args.correction else {})
        rl = RunLog(args.job, _utc_today()) if args.job else None
        entry = stage_resolution(args.signature, args.context,
                                 args.root_cause, args.investigation,
                                 correction_payload, runlog=rl)
        print(json.dumps(entry, indent=2))

    elif args.cmd == "check":
        rl = RunLog(args.job, args.date or _utc_today())
        violations = check_violations(rl)
        print(json.dumps(violations, indent=2))
        return 20 if violations else 0

    elif args.cmd == "spool":
        rl = RunLog(args.job, _utc_today()) if args.job else None
        counts = merge_spool(rl)
        print(json.dumps(counts, indent=2))

    elif args.cmd == "expectations":
        legs = load_leg_expectations().get("legs", {})
        if args.leg not in legs:
            print(f"unknown leg: {args.leg}", file=sys.stderr)
            return 2
        print(json.dumps(legs[args.leg], indent=2))

    return 0


def _watchdog_alert(payload: dict) -> None:
    """Append one alert line to watchdog_alerts.jsonl. Never raises — the
    watchdog must not break the loop's own exit path."""
    try:
        rec = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
               **payload}
        _append_line(WATCHDOG_PATH, json.dumps(rec) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    # Unhandled-exit watchdog (Gemini action plan item 2; approved by
    # the principal 2026-10-15). If this process dies by an unhandled
    # exception, or exits without main() returning (and without an
    # explicit SystemExit — gate exit codes like 10 are designed
    # completions, not deaths), leave an alert line in
    # watchdog_alerts.jsonl so the death is visible instead of silent.
    # Scope honesty: this catches deaths INSIDE a loop invocation. A
    # cron worker turn interrupted from outside never starts the loop —
    # that failure class is netted by the freshness audit's run-status
    # checks, not by this trap.
    _wd_state = {"completed": False}

    def _wd_atexit() -> None:
        if not _wd_state["completed"]:
            _watchdog_alert({
                "kind": "exit_without_completion",
                "argv": sys.argv[1:],
                "note": "process exited without main() returning and "
                        "without an explicit SystemExit"})

    atexit.register(_wd_atexit)
    try:
        _rc = main()
        _wd_state["completed"] = True
        sys.exit(_rc)
    except SystemExit:
        _wd_state["completed"] = True
        raise
    except BaseException as _e:  # noqa: BLE001 — watchdog must see all
        _watchdog_alert({
            "kind": "unhandled_exception",
            "argv": sys.argv[1:],
            "error": f"{type(_e).__name__}: {_e}"})
        _wd_state["completed"] = True
        raise
