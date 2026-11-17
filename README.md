# Óðr — Curiosity Loop: production code, packaged as a reference

**What this is.** The production code of a curiosity loop for agent
self-auditing — Predict, Compare, Gate, Investigate, Learn — that ran a
real multi-workflow agent operation (a dealership service department's
daily reporting, payroll, warranty, and audit workflows) from late
September through mid-November 2026. It is shipped as a design
reference: the mechanism, its configuration contract, its break-test
harness, and the field report written about it.

**Provenance.** Designed and directed by the principal — a working
fixed-operations director, not an engineer — and co-designed with his
agents Loki (Muse, Meta) and Gemini (Google). The principal's standing
rule governed the whole run: the loop may read, trace, and stage; only
the human flips a staged resolution to verified. Kill conditions were
set before the first run and never tripped.

## Contents

| File | What it is |
|---|---|
| `curiosity_loop.py` | The loop itself (v1.5.0 + the 2026-10-31 core patch): Tier 0 freshness gate, numeric/semantic gates, investigation TTLs, spool fallback, watchdog, CLI. |
| `CURIOSITY_LOOP_v0.1.md` | The design spec the code implements. |
| `curiosity_manifest.json` | The configuration contract: gate thresholds, freshness-proof modes, drift detection, objectives. |
| `leg_expectations.json` | Per-workflow Predict-step expectations and freshness declarations (live-read / nightly-cadence metric prefixes). |
| `break_test_v2_harness.py` | The break-test harness: 66 checks replaying real production incidents against the loop. |
| `packet-2026-11-17-FINAL.md` | The field report: design, production evidence, failure taxonomy, and run statistics through 2026-11-17. |

## Running the harness

```
python3 break_test_v2_harness.py
```

The harness loads `curiosity_loop.py` from its own directory and runs
against a temporary state directory; nothing in this bundle is written
to.

**Expected results, stated plainly.** Against the production code with
its production configuration, the harness passes **66/66** (re-verified
2026-11-17). Against this scrubbed copy it passes **65/66**. The one
difference is check **D12f**, a probe the harness itself labels a
retained caveat: a metric whose name is mapped to no objective is
routed by *text relevance* against the manifest's objective entities,
so its borderline verdict depends on the operator's real vocabulary.
Scrubbing the entity names (vendor systems generalized) changed that
vocabulary and flipped the probe. The production configuration was
bisect-tested to confirm the entity scrub — and nothing else — causes
the flip. We ship the scrubbed result and disclose it rather than ship
the operator's real system names to make the number prettier; that is
the same disclosure rule the packet in this bundle was written under.

## Scrubbing

This copy was generalized for release: the principal's name, the
dealership, and vendor system names were replaced with generic terms
("the principal", "the source system", "the timecard system", "the
survey platform"); workflow leg names were generalized consistently
across code, manifest, expectations, and harness; one unrelated
venture-formation objective was renamed with generic entities. Two
functional changes were made for portability, both mechanical:

1. `curiosity_loop.py`: the state directory is now the module's own
   directory instead of a hardcoded workspace path.
2. `break_test_v2_harness.py`: the module under test is loaded from
   the harness's own directory instead of an absolute path.

Internal `cron_file` paths in `leg_expectations.json` were removed;
they described the operator's local layout and carry no design content.

## What is not in this bundle

The live data stays with the operator: the anomaly store (276 staged
resolutions, 107 principal-verified), 478 per-leg run logs, and the
watchdog alert log. The packet's §8 reports their statistics. The
packet is included exactly as the principal reviewed it; its §2
receipts retain internal record numbers as evidence pointers and
contain no customer or employee data.

*Packaged 2026-11-17, on the principal's direction, as a reference for
whoever is building agent containment next.*
