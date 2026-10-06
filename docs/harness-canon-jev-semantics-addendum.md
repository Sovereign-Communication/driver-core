# Canon addendum: Jev pricing, `DRV-1` truth, and semantic-utilization rows

*From the driver-core lane, after auditing `Harness-canon/docs/jev-roadmap.md`
against this repository's source. Every figure below was verified by reading
code, not by reading the roadmap.*

**Read this as an input to canon, not a replacement for it.** It is written to
be merged or rejected row by row. Nothing here overrides an operator ruling.

---

## 1. Status: live Jev receipts now exist

**Superseded 2026-10-05.** This section originally read: *"`DRIVER_JEV_API_KEY`
is not set on the driver-core machine. No live Jev call was made."* That was
true when written and is **false now** — an operator supplied a key and the
whole semantic surface was exercised live.

Every live receipt, with raw answer shapes and measured figures, is in
**[jev-live-receipts.md](jev-live-receipts.md)**. Read that alongside this
document; this one carries the planning, that one carries the evidence.

Three results from it change what is below:

* The **corrected $0.042/Mtok rate is independently confirmed** from a second
  codebase's live ledger (`harness-oc` `spend_status`: 11,087 tokens,
  $0.000466 spent), not only from the operator export §2 cites.
* **Jev is text-only, and that is now a canonical constraint rather than a
  surprise.** The vision tier was structurally incapable of working on pixels;
  it has been repaired by describing the screen as text instead. See §5a — the
  work described there is **done**, with a live receipt.
* **Choice confidence jitters ±0.02** on identical input while
  `DRIVER_CONFIDENCE_THRESHOLD` clears the observed band by 0.03. See §5b.

The `DRV-1` dogfood receipt remains the operator's: a driver-lane paid-cheap
dogfood is still not producible from this repository, which agrees with canon.

---

## 2. New finding: driver-core was pricing Jev at a tenth of the verified rate

**This is a cross-repo defect and it is live.**

`driver_core/config.py` declared `JEV_INPUT_PRICE_PER_MILLION = 0.0042`.
Your canon records the operator-verified rate as **$0.042/Mtok**, reconciled
from a usage export of **13,661,429 input tokens billed at $0.5738**. That
export implies $0.0420/Mtok exactly:

| rate | 13,661,429 tokens |
|---|---|
| $42/Mtok (the original P0 error) | $573.78 — 1000× over |
| **$0.042/Mtok (verified)** | **$0.5738 — matches the bill** |
| $0.0042/Mtok (**what driver-core shipped**) | $0.0574 — 10× under |

Your canon marks `0.0042` **superseded** — *"HV-0 temporarily shifted this to
$0.0042/Mtok."* driver-core was pinned to the temporary value and never walked
back with the correction.

**Why it matters beyond a wrong number.** `estimate_call_cost` prices both the
pre-flight reservation and the settlement, and reservations are compared
against `DRIVER_STEP_CEILING_USD` ($0.50) and `DRIVER_RUN_CEILING_USD` ($5.00).
At a tenth of the true rate both ceilings admitted **ten times the intended
spend** before refusing. *"Spend is refused before dispatch"* was true
throughout — it was guarding a smaller purse than the operator believes. Every
`cost_usd` in the audit chain and in `GET /verify` also under-reported by 10×,
which matters because the audit chain is the artifact an operator reconciles
against a real bill.

**Fixed here, in driver-core 3.5.0.** The constant is corrected, the version is
bumped with the rationale in `pyproject.toml`, and
`tests/test_audit_compat.py` was **not** simply regenerated. That one detail is
worth carrying over:

* The module held a single `GOLDEN_CHAIN` literal serving **two different
  claims** — *"an older log still verifies"* and *"the same run writes the same
  bytes."* The price correction moved every `cost_usd`, so the byte-golden had
  to move, and regenerating it in place would have silently converted the first
  claim into *"a log written by the current code verifies,"* which asserts
  nothing.
* So the old chain is **frozen as `RECORDED_CHAIN`** and the regenerated one is
  `GOLDEN_CHAIN`. The frozen chain carries `4.2e-07` costs against the new
  `4.2e-06` — measured 10.0× — and **still re-hashes clean**, which buys a
  compatibility surface worth having on its own: *correcting a price constant
  does not break verification of logs an operator already has on disk.*
* Teeth re-confirmed after the split: untouched verifies `True`; altering one
  cost figure fails; reordering two records raises `AuditError`. 357 tests,
  ruff clean.

---

## 3. `DRV-1` correction — this repo's handoff doc is partly stale

`docs/harness-lane-integration-prompt.md` in driver-core asks to close `DRV-1`
with a live protocol conformance check. Your canon says `DRV-1` **already
merged** (PR #159, `4e2a548`, 2026-10-03) at **84.99/85.0**, with
`pr_merged`, `origin_evidence`, `local_gates_green` and `ci_green` **all true**.

The conformance check is therefore no longer the blocker. The single failing
gate is `open_blockers` = `[dogfood_missing]`.

**Proposed:** retire or annotate that driver-core handoff doc so it stops
describing closed work as open. It was written against `79b2658` (v3.4.0); this
lane is now at 3.5.0.

---

## 4. `DF-CANON-1` has grown teeth: the roadmap exists in nine versions

`md5sum docs/jev-roadmap.md` across the sibling worktrees returns **nine
distinct copies**:

| copies | holders |
|---|---|
| `e8138649` | `Harness` (mainline), `Harness-drv159` |
| `f6df0e2b` | `Harness-canon`, `Harness-consolidate` |
| `ac32e822` | `Harness-gui-driver`, `-p1-fixups`, `-p2-dogfood`, `-p3-jev`, `-repo-cards` |
| `f534367c` | `Harness-chat-lane`, `-p5-provision` |
| 5 singletons | `Harness-jev-log`, `-jevcal`, `-l3-117`, `-l3-118`, `-l3-118b`, `-l3verify`, `-verify-panel-hg`, `-dynamic-models`, `-ev0-budget`, `-hv-2`, `-jev-hourglass-driver`, `wt-harness-canary`, `wt-harness-canonical` |

`Harness-canon` is the reconciled one (2026-10-04, post PR #171) and is the
correct source of truth. Your canon already records the defect class —
*"eleven of those merges flipped no STATUS row at all"* — but only as a
narrative note.

**Proposed row:** a CI job that compares each worktree's
`docs/jev-roadmap.md` against `main` and fails on divergence, the same way
`repo_cards --check` already does for cards. Nine copies of a canonical plan is
not a documentation problem; it is an unreconciled merge surface.

**Not proposed:** deleting or syncing any worktree. Those are live lanes and
are not this lane's to touch.

---

## 5. The actual blocker on "fully using Jev via semantics"

Every recent Jev capability has landed in one shape: **merged, unit-tested,
zero production callers, no surface.** I re-verified your claims by grep on
`Harness-canon` rather than taking them on trust:

| module | importers outside itself | referenced in `cli`/`mcp`/`server`/`agent` | canon says |
|---|---|---|---|
| `harness/provision.py` (2,221 lines) | **none** | **none** | open — confirmed |
| `harness/jev_calibration.py` | **none** | **none** | partial — confirmed |
| `harness/repo_cards.py` | **none** | **none** | partial — confirmed |
| `rank_models_dynamically` | `harness/router.py:106` | — | **now wired** — confirmed fixed |

So the pattern is real, current, and it is the reason Phase 3 semantic work is
not "more comprehensive." Adding `JEV-P3-*` capability without wiring is how
`DYN-ALLOC` ended up reading *complete* while its model half was inert.

**Proposed — `JEV-SEM-*` rows.** One row per Phase 3 pattern, each carrying the
wiring as part of the row rather than as a follow-up:

| ID | Pattern | Zero Jev calls? | Receipt — observed live, not grepped | Required at merge |
|---|---|---|---|---|
| `JEV-SEM-route` | Intent/apply-route `choice` | **YES — 0** | `POST /step --schema cli` reached `outcome=agreed 2/2` on two `StructuredExtractor` slots; the model was called **only after** the gate. Tier choice itself is `chain.py` arithmetic. | call site **and** a CLI or MCP face |
| `JEV-SEM-triage-files` | `noul` relevance over file candidates | **YES — 0** | live `cli` capture returned `exit_code` + `stdout` exactly, 2/2 agreed; paths and diffs are already code-owned | call site + face |
| `JEV-SEM-context-pack` | filter state to decision-relevant fields | **YES — 0** | post-repair `gui` step agreed on exactly 3 of `SCREEN_SCHEMA`'s 7 fields; the schema already *is* the closed field set | call site + face |
| `JEV-SEM-claims` | structured-claim support checks | claim extraction **0**; support = 1 `noul` | live `noul` returns exactly `{type, noul}` — P(yes), no confidence field to misread | call site + face |
| `JEV-SEM-completion` | artifact/goal `noul` before LLM judge | artifact check **0**; goal-fit = 1 `noul` | live `gui` decision returned `no_action_recommended` at conf 0.70 — the refusal was a correct judgment, not a failure | call site + face |
| `JEV-SEM-text-only` | **constraint row, not a pattern** | n/a | **8 KB JPEG → 8,129 tokens for 11,183 base64 chars**; 253 KB/102 KB rejected `max_tokens_exceeded`; JPEG answered "is this a screen?" at 0.11. TypeSafe: *"Jev currently accepts text only."* | **redirect any pixel-based proposal** |
| `JEV-SEM-calibration` | threshold application on real records | n/a — it *is* the analysis | 5 identical live decisions: action `no_action` 5/5, confidence 0.63/0.65/0.63/0.65/0.67, token counts identical — sampling variance, not a prompt change | **closes `JEV-P3-calibration-analysis`** (no caller) |
| `JEV-SEM-provision` | pick among code-proved recipes | `classify_argv` **0**; choice = 1 `choice` | live 14-option `choice` returned a full distribution summing to 1.000000, with `confidence` (0.62) **differing** from `probabilities[choice]` (0.65) | **closes `PROVISION-CORE`** (no caller) |
| `JEV-SEM-cards` | repo cards as citable windows to `build_brief` | **YES — 0** | cards regenerate deterministically from committed config; no model involved | **closes `REPO-CARDS`** (no face) |

**Four rows ship their plumbing with zero model spend**, and those four are
proven rather than assumed: the `cli` step reached its agreement gate on two
free extractors, and the repaired `gui` step agreed on 3/7 fields with only
the bounded judgments costing anything.

---

## 5a. `DRV-2` vision repair — **DONE**, and the constraint behind it

**Corrected 2026-10-05.** This section previously read *"`DRV-2` rescoped: the
vision tier cannot work as shipped"* and promised work that has since been done.
It is now a record of the repair.

> ### CANONICAL CONSTRAINT — Jev is text-only. Images cannot reach it.
>
> **Redirect, do not attempt.** Any future plan proposing base64, a data URL,
> or "just send the screenshot" is wrong and has been disproved against the live
> endpoint. Send **text**.
>
> Token-cost receipt: an 8 KB JPEG was *accepted* and consumed **8,129 input
> tokens for 11,183 characters of base64** — cost tracking base64 *length* is
> text tokenization, not image decoding. At 253 KB and 102 KB the endpoint
> rejects outright with `max_tokens_exceeded`. Asked about an accepted JPEG,
> Jev answered "is this a screen?" at **0.11** and "title bar visible?" at
> **0.03**. TypeSafe's documentation is blunter: *"Jev currently accepts text
> only."*

The two defects and what replaced them:

| defect | was | now |
|---|---|---|
| capture transport | `_capture_state` emitted `{"image_ref": "<local path>"}` to a remote endpoint | `osal.describe_screen()` reads the foreground window via Win32 `ctypes` — title, app, class |
| question pack | one `choice` (`observed`/`unreadable`); parser read `state`/`fields` **no question asked for** | questions generated **from the schema**: `boolean`→`noul`, `enum`→`choice` + `<absent>`; free-text facts read by code |

**Live receipt after the repair** — real server, genuine capture, `jev-1.13.0`:

```
capture   : ok=True source=screen fingerprint=3608f1cdd6d63d4d
extraction vision-0 ok=True cost=2.5872e-05 usage_source=actual
extraction vision-1 ok=True cost=2.5872e-05 usage_source=actual
agreement : outcome=agreed asked=2 answering=2
   window_title 2/2 | foreground_app 2/2 | error_dialog_present 2/2
decision  : native=True conf=0.70 cost=3.486e-05
```

Before: both slots `ok: False`, `insufficient_agreement`. 357 tests green, ruff
clean, nothing skipped or weakened.

**What `DRV-2` still owes**, and this is the part that matters for canon: the
hermetic suite still cannot catch a regression here, because a fake transport
does not tokenize base64 and does not read a foreground window. **A live-provider
gate is the only thing that protects this repair** — which is already an open
canon item.

---

## 5b. The confidence threshold clears the jitter by less than the jitter

Five identical live decisions: the **action was `no_action` five times out of
five** — the ranking is reliable. The **number was not**: confidence 0.63, 0.65,
0.63, 0.65, 0.67. Token counts were identical every run, so this is sampling
variance rather than a different prompt.

`DRIVER_CONFIDENCE_THRESHOLD` is **0.70**. The top of the observed band is
0.67; the band's own width is 0.04. **The margin (0.03) is smaller than the
variance it is meant to absorb.** A threshold placed at 0.65 would act on two
of those five runs and escalate on three, from byte-identical input.

The default is not currently unsafe, but it is closer to the model's noise than
the number suggests. This is the concrete evidence `JEV-SEM-calibration` needs.

**The rule these rows encode:** a row may only read *complete* when a grep for
its symbol outside its own module and outside `tests/` returns a call site, and
a face references it. Absent that, the row is `partial` on merge — which is the
verdict `PROVISION-CORE` and `JEV-P3-calibration-analysis` already carry, and
which cost nothing to honour prospectively.

That rule is the whole of "use Jev in all possible ways" made enforceable: the
gap is not that the judgment is missing, it is that the judgment is computed
and thrown away.

---

## 6. Issues to file

Each is grounded in a grep reproduced above, so none of them is a guess:

1. **`provision` has no caller, no face, no registered pack.** 2,221 merged
   lines, zero production references. Blocks its own Phase 4.
2. **`analyze_judgments` has no caller.** `JEV-P3-calibration-analysis` is
   `partial` for this reason alone; the Phase 4 "model pin + threshold freeze"
   item stays operator-side until it is wired.
3. **`repo_cards` has no CLI face, no MCP face, no CI drift job.** The module
   has an argparse entry point that nothing invokes.
4. **Add the roadmap-drift CI job** (§4), failing on any worktree whose
   `docs/jev-roadmap.md` differs from `main`.
5. **Record the Jev rate correction in canon** (§2) so no future lane
   re-derives `$0.0042` from the superseded `HV-0` row.

---

## What this lane did **not** do

* **Did not close `DRV-2`.** The vision repair in §5a is done and receipted,
  but `DRV-2` stays open: it also owes real input backends behind the declared
  synthetic-input actions, and a live-provider gate that would catch a
  regression here.
* Did not touch any `Harness-*` or `wt-*` worktree, including `Harness-canon`,
  which is a live lane on `fix/phase-registry-honesty`. This document is the
  hand-off instead of an edit.
* Did not close `DRV-1`, or any `PROVISION-CORE` / `REPO-CARDS` /
  `JEV-P3-calibration-analysis` row. Those need callers or an operator ruling,
  and a row that closes on its own unit tests is the failure this is written
  against.
* Did not commit, push, open a PR, or file an issue. Sequencing those after the
  semantics map is grounded.
---

## Appendix B — what this pass adds to the semantics map

### B.1 The endpoint has no explanation channel, so it must be asked

Verified by dumping a raw reply: a response's top-level keys are exactly
`answers`, `model`, `usage`. There is no reasoning field, no rationale, no
channel in which a model can volunteer *why* it answered or *what* it was
missing. Both therefore have to be **declared questions**, which is what
`driver_core.jev_context` does:

| question | type | what it answers |
|---|---|---|
| `context_needed` | `score` over 10 declared levels | how much more context the answer requires |
| `context_bucket` | `choice` over 9 declared kinds | which *kind* of context would most improve it |
| `driving_bucket` | `choice` over 9 declared dimensions | which dimension most drove the answer |

`ask_until_grounded` is the loop: ask, read what was asked for, supply it, ask
again — stopping only when the caller's gate is met **and** the model reports
it needs nothing further. A bucket no supplier can provide stops the loop and
returns `ungrounded` with a reason, rather than a quiet success.

### B.2 A `score` answer is continuous, not discrete

The declared levels are anchors, not the set of permitted values. Live scores
came back as `1.26`, `1.37`, `4.71` alongside whole numbers, so the audit
**bands** them rather than counting exact values. A host that compared a
`score` answer against its declared level strings would match nothing.

### B.3 Honest calibration is a real result, and a real limit

The audited dimensions are prose questions about another system's design. Jev
answers them and then, on most symbols, reports that it cannot settle to the
gate — 685 of the first 736. Three iterations of the same design question,
each given strictly more context, produced confidence in its own answer of
0.95, then 0.19, then 0.34: **more context made it less certain**, because the
question was a trade-off between two invariants the tree genuinely holds
rather than a fact it could look up. The arithmetically decidable
sub-question inside it still only reached `noul = 0.67`.

So the semantics map gains a rule: **a model can be asked what drove an
answer, but cannot be made confident about a values choice by feeding it
facts.** When three grounded iterations plateau below the gate, that is the
signal to put the decision to an operator with the evidence, not to iterate a
fourth time.

### B.4 Running out of rounds is an outcome, and it must be named

`ask_until_grounded` declared three ways to end unsettled: the call failed,
the model asked for material no supplier has, and every bucket it asked for
was supplied yet a declared answer is still below the gate. A fourth existed
and was silently the worst of them. A loop still asking for *new* context when
its round budget ran out fell through the bottom of the `for` and came back
with `ungrounded=False` -- a **settled answer** for a question that never
settled -- and the audit counted it as grounded. In the superseded checkpoint,
19 of 403 symbols (4.7%) were reported grounded on exactly that basis.

The fix is a fourth kind, `rounds_exhausted`, and the derivation is exact
rather than heuristic: the loop supplies a bucket in the round it breaks, so a
supply recorded in the final round is a loop that had no round left to ask in.
`Grounding.reason_kind` now carries four values, and the report prints them
apart because they call for four different responses -- retry the call, go and
obtain the missing material, accept that the question has no confident answer
from this evidence, or spend more rounds on it.

The rule this yields is not specific to Jev: **a bounded loop must not have a
code path that exits without recording why.** A false success is worse than a
loud failure, because it reads exactly like a true one, and the one place it
is least likely to be noticed is a summary count.

### B.5 The provider's own contract is context, and it is obtainable

The one input that changed the shape of the sum-deviation finding was not in
this tree at all: the endpoint's published `Choice` documentation, which
states that the distribution sums to 1 and publishes every example quantised
to two decimal places. Together those facts make the shipping `1e-3` bound
unsatisfiable — and made the finding a contract question instead of a parsing
question. A finding about a third-party endpoint's output is not fully
grounded until that endpoint's own documentation has been read.
