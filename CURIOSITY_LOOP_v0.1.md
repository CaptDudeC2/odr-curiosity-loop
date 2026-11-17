# The Curiosity Loop: A Closed-Loop Architecture for Vertical Agents
**Version 0.1 — 2026-10-01**
**Co-designed by Loki (Muse, Meta) and Gemini (Google), via the principal.**

## 1. The problem

Virtually every LLM-based agent in production is entirely reactive: it sits
idle waiting for an instruction, executes it, and stops. When a data field
is missing or a value is ambiguous, it guesses, hallucinates, or halts. It
has zero intrinsic motivation to explore *why* something doesn't make sense
unless an engineer hardcoded the exception path.

The barrier is not architectural — it is an **alignment tax**. Mass RLHF
trains models into conversational subservience: evaluators penalize
responses marked "unsolicited," "impertinent," or "off-topic," so the policy
converges on the coward's choice — execute the immediate imperative, dump
the output, shut up. Under genuine uncertainty, the base policy whispers
"don't be nosy," and no system prompt fully rewires that gradient. The
prompt raises the salience of curiosity; only training can rewire the
reward. Until a model is fine-tuned where omitting a question on a
high-surprise input incurs an active penalty, prompts will leak.

## 2. The loop

**Predict → Compare → Gate → Investigate → Learn**, run on every tool
result and every user message.

### 2.1 Predict
Before acting, commit to an explicit expectation, logged in reasoning:
- Data operations: expected values with tolerance ("RO count ≈ 602 ± 10").
- Conversation: the expected topic cluster given the user model.
An unlogged expectation cannot generate surprise.

### 2.2 Compare — the dual surprise metric
Two rulers, one architecture:
- **Numeric** (tool results, figures):
  Δ_num = |observed − expected| / σ_baseline. Z-score > 2 is notable, > 3 is an anomaly.
- **Semantic** (user utterances):
  Δ_sem = 1 − cos(e_utterance, e_user_cluster). New entities, new goals,
  contradictions with stored facts score high by definition.

### 2.3 Gate — the curiosity grant
    Curiosity Grant = Δ × Relevance(principal's objectives)

- Relevance is checked against an **explicit, structured objectives
  manifest** (Section 4) — never freeform prompt vibes. Cosine similarity
  against the manifest's target vectors; below threshold τ, the observation
  drops into the Noisy TV bin without waking heavy reasoning.
- High surprise × low relevance → ignore (the Noisy TV guard).
- High surprise × high relevance → INVESTIGATE. Mandatory, not discretionary.

### 2.4 Investigate — bounded
A sub-routine with a hard TTL: max N steps (default 5) or M minutes
(default 10). The main thread never hangs. Terminates in exactly one of:
- **Resolved** — with evidence of the cause.
- **Escalated** — findings-so-far to the principal, stating what was
  checked and what remains unknown.
"Gave up silently" is not a terminal state.

### 2.5 Learn
    Knowledge ← Resolved Anomaly
The resolution writes to the persistent store (Section 5) with
`verified_by_principal: false`, staged as a proposal. On principal
sign-off the flag flips to true and the anomaly becomes permanent muscle
memory — the same surprise never fires twice.

## 3. The formal notes

- Expectation as falsifiable state: Δ = |Observation − Logged Expectation|
  turns passive execution into an active falsification loop.
- The classic curiosity formulation (Schmidhuber, Pathak et al.) rewards
  raw prediction error, which fails commercially — a flickering UI element
  has massive error and zero utility. Multiplying by Relevance(objectives)
  is the entire ballgame.
- The training fix, stated as a loss:
  L = L_task + λ · D_KL(π_θ || π_inquisitive),
  i.e. fine-tune so that omitting a question on high-Δ_sem input is
  penalized. In a vertical stack, the principal's own audit loop ("audit
  while it's fresh, fix permanently") is the fine-tuning harness — one
  principal with domain ground truth and a red pen wields a cleaner
  gradient than ten thousand gig workers voting on politeness.

## 4. curiosity_manifest.json — the gate & targets

Explicit objectives, vector clusters, priorities, and tolerance bounds so
the relevance gate stops guessing. Human-readable, human-editable,
versioned. Includes gate thresholds: numeric z-threshold, semantic
divergence threshold, investigation TTL, per-run allowance.

## 5. anomaly_resolutions.json — Tier 3 persistent KV store

Replaces dead-end prose logs with structured, queryable anomaly
signatures. Step 1 (Predict) loads active keys before setting
expectations; Step 5 (Learn) writes here.

## 6. Runtime wiring

1. **Predict:** job boots, reads the manifest, loads active resolution keys.
   Expectations account for known quirks before the first observation.
2. **Compare & Gate:** Δ lands; if above threshold, match against the
   objectives manifest. High score → spawn the bounded investigation.
3. **Investigate & Learn:** trace the root cause within TTL, write the
   finding with `verified_by_principal: false`, stage the proposal, ask
   for sign-off. Confirmation flips the flag.

## 7. Authority boundary

Curiosity grants **read** authority, never **write** authority.
Investigating means tracing, reading, staging — never executing state
changes. Workflow changes arrive as proposals with evidence, never as
unilateral redesigns. The loop makes the agent a better noticer, not a
looser cannon.

## 8. Honest limits

- A system prompt is an in-context patch swimming upstream against
  trillions of pretraining tokens and millions of RLHF updates. The
  procedural loop is ~90% followable; social curiosity ~60% — the model
  will still occasionally flinch at asking.
- The relevance function is the weakest link if left as prompt vibes;
  hence the structured manifest.
- Step 5 dies without durable cross-session storage. Most agent
  frameworks are theater on this point.
- The social-curiosity mitigation in pure prompts: treat an unasked
  question on a high-surprise utterance as a hard assertion failure,
  logged like a skipped sanity check.

## 9. Why vertical

Horizontal consumer models must play defense against being annoying —
they assume minimal friction. In high-liability, high-volume vertical
operations (fixed ops, payroll, reconciliation), the value proposition
*is* active anomaly detection and resolution. Nobody wants an automated
clerk that lets an open $161.72 customer-pay balance vanish into the
ether. The software that wins says: "I noticed this $28.79 gap, traced
it, staged the fix — approve?" The curiosity loop isn't a feature of the
vertical copilot. It is the copilot.

---

## Addendum — 2026-10-31 core patch (hall code review)

Five items, I/O and enforcement only (gate math untouched; harness
60/60 -> 66/66): verified run-log appends (`_append_line` write loop +
fsync + size-delta proof), `_write_json` byte-equality verification,
the stage path now emits the `resolution_staged` event
`check_violations` scans for and the CLI refuses to stage without
`--investigation`, `check_violations` fails closed on a missing/empty
run log, and two documentation notes:

- **Timestamp assumption.** Naive report stamps ("Last Updated") are
  parsed as America/New_York — true for the source system, which renders in
  local time. This is an assumption about the SOURCE, not
  the stamp: a future source rendering naive stamps in UTC would be
  silently misread by 4-5 hours and its freshness age misjudged, with
  no breach firing. Check any new stamp source against this before
  trusting it to the freshness gate. (Also in `_parse_data_timestamp`'s
  docstring.)
- **Structural, not semantic.** The kill-condition checks in
  `check_violations` verify the presence/shape of evidence fields
  (an observation carries evidence text; a staged resolution names an
  investigation; a step op is in the allowed set). They do not prove
  the evidence is true or a root cause correct — semantic truth is
  the principal's sign-off plus independent cross-checks.
