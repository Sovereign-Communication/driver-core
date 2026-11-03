# Jev can see: what image input changes, and where Jev goes next

*Written against driver-core `53b435a` and the TypeSafe docs as of 2026-10-14
— the day Jev 1.14 shipped. Everything below is either read from this repo or
read from `docs.typesafe.ai`; nothing is inferred from memory. The TypeSafe
skill (`.agents/skills/typesafe-ai`) is installed and this document follows
it: the live docs are the source of truth, and they call images and Box
**dreamer-ready**, not production-ready. That label governs everything
downstream.*

---

## The headline: the canonical constraint is superseded

`docs/harness-canon-jev-semantics-addendum.md` §5a and
`docs/jev-live-receipts.md` carry a canonical constraint:

> **CANONICAL CONSTRAINT — Jev is text-only. Images cannot reach it.
> Redirect, do not attempt.** Any future plan proposing base64, a data URL,
> or "just send the screenshot" is wrong and has been disproved against the
> live endpoint.

As of today, **images can reach Jev**. The System One request accepts images
in `state`, in a question's `instructions`, and in its `criteria`; every
question type accepts them; and a new `box` question type returns *where* in
an image something is. This is not a reinterpretation of the old receipts —
it is a new request surface that did not exist when they were taken.

### Why the old receipts remain true

The 2026-10-05 probe sent base64 **as text inside `state`** — no `assets` map,
no header — and measured what that shape does: 8 KB of JPEG became 8,129
input tokens for 11,183 base64 characters, because base64 characters are
tokenized as text. Everything about that receipt is still correct, and the
rule it produced still holds:

**Base64-as-text in `state` is still wrong and stays prohibited.** It is
expensive noise: the model reads characters, not pixels.

What changed is the *shape*. Images now travel in a top-level `assets` map,
keyed by reference strings that are written wherever the image belongs (as a
whole value or mid-sentence), and the request carries
`TypeSafe-Danger-Zone-Images: true`. The reference stands for the image; the
image arrives as pixels. The old probe had no such surface to use — "images
cannot reach it" was true of the endpoint as it was.

### What the new surface actually is (facts, not hopes)

- **Models.** Jev 1.13 and Jev 1.14 both accept images. `box` needs 1.14 —
  send `jev-1.14.0` or `jev-preview`. Jev 1.14 is available from 2026-10-14.
  `jev-latest` still points at `jev-1.13.0`; `jev-preview` points at
  `jev-1.14.0`.
- **Placement.** `state` (shared by every question), `instructions` (one
  question), or `criteria` (options/levels defined by example pictures).
  A state can hold several images, including a frame history as an array.
- **Opt-in.** Images and `box` are off by default; the
  `TypeSafe-Danger-Zone-Images: true` header turns them on per request.
  A request with an image and no header returns `400`.
- **Limits.** PNG/JPEG stills only; 256–1568 px per side; 78,400–2,458,624
  total pixels (280²–1568²); 16 MiB per image; 32 MiB request body, all
  images together, and expanded request. **An out-of-range image is rejected,
  never resized — and one rejected image fails the whole request.** There are
  no partial answers.
- **References.** Every asset must be referenced or the request is `400`.
  Every *use* of a reference re-adds the image to what the model reads, and
  every copy counts toward tokens and the expanded-request limit. Object
  keys are never replaced.
- **Cost.** Image tokens are billed at the same input price as text
  ($0.042/Mtok — which is exactly `JEV_INPUT_PRICE_PER_MILLION` in
  `driver_core/config.py`, so this repo already prices it right). TypeSafe
  has **not yet published an image-token estimator**, so a pre-flight
  estimate cannot be derived from byte length — the one thing the old probe
  proved is that byte length is the wrong proxy.
- **Maturity.** "Dreamer-ready": the request format, answers, and limits can
  change between releases, and accuracy will change as models update. That is
  a licence to prototype behind switches, not to widen a default.

So the canonical constraint survives as a **gate, not a prohibition**: pixels
are now a real, declared, opt-in channel — and they stay the last tier, the
expensive one, and the one whose every step is recorded.

---

## What does not change

The architecture's load-bearing arguments all survive contact with images,
and most of them get *stronger*:

1. **The tier order `CLI → MCP → DOM → pixels` stays.** Structured sources
   are free, deterministic, and exact; pixels are billed, lossy, and now
   also *tokenized unpredictably*. The images docs' own advice — "send fewer
   images, send smaller images … look closer when needed" — is the same
   preference `select_capture` already encodes in types. Pixels become the
   honest last resort instead of a faked one; they do not become the first
   choice.
2. **Cross-verification before the decision model stays.** A calibrated
   model fed a wrong extraction returns a confidently wrong answer about the
   wrong state. Seeing pixels removes the *transcription* layer of that risk;
   it does not remove the risk. The tally still owns the verdict; a specialist
   model may explain it, never overrule it.
3. **The service wire contract is untouched.** The 11 keys of `POST /step`,
   the 9 `STOP_REASONS`, refusal-as-200, and the 4xx boundary are what the
   harness adapter ports. Images change what the extraction tier sends
   *upstream to TypeSafe*; they change nothing on the `/step` surface.
   `harness/perception_client.py` and `DRV-1` are unaffected.
4. **"Invent no capabilities" still binds.** `box` returns coordinates;
   nothing in this repo can act on them. Synthetic input (`click`, `type`,
   `key`) remains **declared and refused**. A box is an *observation*; a
   click is an *action* with no backend. The receipt that says so stays.
5. **Nothing which came off a screen comes back out.** The vision audit
   record is metadata-only by explicit allowlist, and the sentinel tests
   enforce it. Image-derived state is still screen content; the pixel path
   inherits that guarantee unchanged.
6. **Namespaces stay separable.** `TypeSafe-Danger-Zone-Images` is a header
   on the outbound TypeSafe call, not a new environment namespace.
   driver-core still reads only `DRIVER_*`.

---

## What changes in this repo, fact by fact

### `driver_core/extractors.py` — the capture becomes evidence

`VisionExtractor.uses_capture = False` is declared today with the comment
*"reads the window through `osal`, so it has no use for a capture and does
not accept one — a capture is not evidence it can use."* With images, that
last clause inverts: **the capture is exactly the evidence.** The pixels
path already exists — `ScreenSource` can take a screenshot, `osal.capture_screen`
writes a PNG — and is currently *skipped* because nothing reads the payload
("a screenshot being taken for a payload no vision extractor looks at" is
the recorded reason). The pixels tier makes that path load-bearing again.

The honest shape is two modes, not a replacement:

- **words mode** (default, today's behavior): `osal.describe_screen()` →
  window title, foreground app, class. Free of image limits, exact where it
  speaks, and still the right default.
- **pixels mode** (opt-in, behind the vision tier's own `DRIVER_SCREEN`
  switch): the capture goes into `state` as one asset; the schema-generated
  question pack is asked *against the image* instead of against a
  description. `_extraction_questions` already generates per-field
  `noul`/`choice` from the schema, with `<absent>` for enums — that pack
  ports almost unchanged; the instructions change from "according to the
  description" to "according to `screen`".

One rule from the images docs lands hard here: **put the image in `state`
once and reference it by path from each question.** A pack that repeats the
reference in every question's `instructions` re-adds the image per question —
a 7-field schema is 7 copies of the screenshot in the expanded request. The
pack already separates state from questions, so this costs nothing to obey.

The `observation` gate question ("is this a real observation?") survives as
a Noul over the image itself, and the `_observed_state` parser shape is
untouched: answers carry the reference, not the image.

### `driver_core/osal.py` — preparation is now a duty

The API rejects, never resizes. So the capture path owes:

- **Scale to ≤1568 on the longest side.** A 1920×1080 virtual screen is
  2,073,600 total pixels — under the 2,458,624 cap — but its 1920 width
  exceeds 1568, so a full-HD screenshot is **rejected as-is**. A
  multi-monitor virtual screen (the Windows capture uses `VirtualScreen`) is
  worse: 5760×1080 exceeds both the width and the total-pixel caps.
- **Respect the minimum.** 256 px per side and 78,400 total pixels: a
  capture of a small window must be padded or scaled *up*, and a 256×256
  image is too small.
- **Rotate upright.** The API strips metadata and ignores EXIF orientation.
  Screenshots are usually upright; a prepared pipeline does not rely on
  "usually".
- **PNG or baseline JPEG, still, ≤16 MiB.** Screenshots are already PNG.

### `driver_core/transport.py` — one more body field, one more header

`call_service` already takes `body_extra`; an `assets` map rides in it, and
the danger-zone header goes on **only** requests that carry an image or a
Box question. Keeping the default surface default is the whole point of an
opt-in flag — and of the header being explicit rather than inferred.

### Budget — the estimator is the sharp edge

`_estimate_capture_tokens` prices a call as `serialized_bytes / 4`, which is
a *text* heuristic. The old probe measured exactly how wrong that is for
image-ish payloads (11,183 characters "cost" 8,129 tokens — far above
bytes/4), and TypeSafe now publishes no image-token formula at all. The
settlement path is fine (`usage.input_tokens`, actual → estimated → full
reservation, never zero); the **reservation** needs a pixel-aware upper
bound — erring high, because an oversized reservation costs a refusal while
an undersized one under-bills. Context counts too: 64k per-request and
32k per-question by default (262k with the long-context header), and image
tokens count toward both. A three-frame history in `state` is three images
against those numbers.

### `driver_core/config.py` — pin the model when thresholds are tuned

`jev_model` defaults to `jev-latest`, which points at `jev-1.13.0` today and
*will move*. TypeSafe's own guidance: if you have tuned confidence thresholds
against one version, send the versioned ID. §5b of the addendum already
found the default threshold (0.70) clears the observed confidence band
(0.63–0.67) by less than the band's own width (0.04) — a threshold that
tight cannot also absorb an alias silently swapping the model underneath it.
Box work must pin `jev-1.14.0` or `jev-preview` regardless.

---

## Where Jev goes further

These are the plays the new surface opens, ordered by how directly they
attach to machinery that already exists. Per the repo's own rule
(addendum §5b): **a pattern that ships without a caller and a face is
`partial`, not complete** — so each entry names where it lands.

### 1. Two independent observers of one screen, in different media

Today a vision pool is N text judgments of one text description. The new
move is a pool that mixes: a words slot (window manager) and a pixel slot
(screenshot) over the same moment. Their failure modes are genuinely
different — one cannot see a dialog that has no title; the other can be
blinded by a rendering the window manager names exactly — and the per-field
tally already knows what to do with that: `extraction_disagreement` is a
declared reason, and text-vs-pixels disagreement is *signal*, not noise.
Lands in: `build_vision_pool` slot composition, `consensus` unchanged.

### 2. Frame history supplies a semantic stability judgment

The images docs' "what changed" pattern is a state holding
`frames[{seconds_ago, frame}, …]` and a question like "did anything in
`frames` change?". `policy.check_decision(..., require_stable=True)` guards
on stability today with a code-owned rule; a Score ("how settled is this
screen") or Noul ("is this screen still changing") over a short frame
history gives that guard a probability to consume. Keep the guard; feed it
a judgment. Lands in: `policy.py`'s stability guard, questions from
`driver.py`'s decision pack.

Implemented behind `DRIVER_JEV_IMAGES` + `DRIVER_JEV_FRAMES`: each extractor
session retains three prepared frames in memory and adds a timestamped-history
Noul to the extraction pack. It shares the pinned image model and the existing
whole-request reservation; the decision pack consumes the returned probability
without asking its single-frame stability question again. A validated judgment
over identical frames is reused while that prepared image remains unchanged;
ordinary extraction still bills normally. Changed frames invalidate reuse.
Warm history with a failed or malformed judgment blocks the existing guard.
Words fallback clears history, and `FrameHistory.clear()` releases session
pixels and judgments explicitly. Frames are never persisted in audit records.
This supplies semantic evidence; threshold calibration remains separate.

### 3. `box` answers "where", with confidence, cross-verifiable

Implemented in the existing `VisionExtractor.locate` caller surface: capture
and preparation happen once; each configured quorum opinion (two by default)
runs the existing bounded presence/Box/edge-repair ladder on that same image.
Every dispatch uses the pinned image model and its own ordinary admission and
actual-charge settlement. All boxes map back to source fractions before
`consensus.tally_boxes` compares them with the declared `BOX_IOU_FLOOR=0.8`.
This is an agreement policy, not a calibrated accuracy threshold. Every pair
in an agreeing cluster must meet it; overlapping chains cannot imply unanimity.
The configured agreement fraction is applied over valid answering opinions,
after checking quorum. A missing, failed or unqualified box is a shortfall
(`insufficient_agreement`); conflicting valid presence/Box opinions are
`extraction_disagreement`. Agreement returns an observed box rather than
averaging coordinates. Agreed absence remains `absent`. Same-model agreement
does not establish independent accuracy. Production caller wiring remains separate.

After exhausted low-confidence repair, locate now searches one fixed set of four
overlapping crops from a call-owned snapshot at original resolution. Each crop
uses the existing preparation, model pinning, admission and three-rung ladder.
Presence/Box and a qualified none/one/several Choice must resolve every tile;
incomplete, clipped or ambiguous searches refuse. Padding and crop origins are
undone before the same source-space consensus. No recursion: at most fifteen
dispatches per opinion (thirty at the default quorum), with actual usage settled
for every dispatched rung. All existing thresholds are retained; calibration
remains pending. Windows crops were exercised live; macOS/Linux crop adapters
remain unverified on their native platforms.

A Box answer is four edges as fractions of the image — resolution
independent, so a box from a scaled capture maps back to screen pixels by
multiplication. Two honest caveats from the docs, both of which map onto
existing machinery:

- **There is no "not found".** A Box about something absent still returns
  four edges. The docs prescribe a companion Noul ("is X in `screen`?") —
  which is the same "a slot that did not answer is not a vote" discipline
  applied to presence.
- **A box can cut its target.** The four-edge-question repair (one Noul per
  edge: "does the target continue past this edge?") caught 30 of 36 known
  cuts — better than a single containment question (26) or an outlined
  overlay (20). That is a deterministic escalation ladder driven by Box
  confidence: exactly the shape `confidence_below_threshold` wants as a
  remedy instead of a bare stop.

For a future gui action backend — which is `DRV-2`'s business, not this
landing's — coordinates are the missing half of a click, and two independent
boxes agreeing within an IoU tolerance is the cross-verification story for
"where". **Not today: no backend, no acting.** But the tally now has
something geometric to agree on. Requires `jev-1.14.0`/`jev-preview`.

### 4. Confidence-gated routing, per action class

`config.confidence_threshold` is one global number. The confidence-routing
pattern is a *floor* plus *per-action* thresholds chosen by consequence: a
reversible observation acts at 0.6; an irreversible action needs 0.85 or a
confirmation step. driver-core's actions are all refused today, which makes
this the cheap moment to declare the ladder in config — code owns the
thresholds, the model owns only the probabilities. §5b's finding binds here:
every rung of the ladder must clear the model's jitter band (observed width
0.04), not merely look separated on paper. Lands in: `config.py` (declared
ladder), `policy.check_decision` (per-action lookup).

### 5. Guardrails at the execution gate

The guardrails cookbook's shape — one request, Noul hazard questions plus a
Score severity, thresholded in code to pass / review / block — attaches to
the executor boundary, where `execution_refused` and the consent machinery
already live ("consent is bound, not global", per `docs/design.md`). The
question pack judges the *proposed action against the observed screen*:
"would this delete data visible in `screen`?", severity scored. This gates
**acting**, never observing, and it composes with (4): low confidence routes
to review, a hazard routes to block. Lands in: the consent/execution gate.

### 6. The grounding loop can be shown receipts

`jev_context.CONTEXT_BUCKETS` already names `receipts` — "live observed
output from a real run". A screenshot is the most literal receipt there is,
and `ask_until_grounded`'s supplier only sends what the model *asks for*,
round by round — which is precisely the cost discipline images need, since
state is re-sent (and re-billed) every round. A supplier that answers
`receipts` with a prepared image asset gets grounding that words cannot
convey. Lands in: the `supplier` seam, no protocol change.

### 7. Batch the questions, keep the gate

The parallel-questions cookbook measured 12.2× cheaper and 10.0× faster
from batching independent questions into one call. The extraction pack
already does this. The *decision* stays a separate call behind the
agreement gate — batching is an optimization within a tier, never across
the gate.

---

## What not to do

- **Do not rewrite the old receipts or delete the constraint.** They
  document the shape that stays prohibited (base64-as-text) and they are the
  evidence trail. The constraint is amended, not erased.
- **Do not send pixels by default.** Dreamer-ready means the pixel path is
  opt-in behind `DRIVER_SCREEN`, pinned to a model, and recorded — one more
  switch that enables exactly one tier, same as `tools/configure_run.py`
  already demands.
- **Do not estimate image cost as bytes/4.** That is the exact mistake the
  2026-10-05 probe measured. Reserve high on a pixel-based bound; settle on
  `usage.input_tokens`.
- **Do not repeat the image reference in every question.** Each use adds the
  image again, in tokens and against the 32 MiB expanded-request cap.
- **Do not hard-fail on model drift.** Print the versioned `model` from the
  response — `Extraction` and the audit record already carry it — and treat
  a version bump as evidence, not a failure. The alias will move.
- **Do not let a box reach an executor.** Coordinates are observations. The
  synthetic-input actions remain declared and refused until `DRV-2`'s real
  backends exist.
- **Do not weaken the sentinel guarantee.** An image path is a new way for
  screen content to travel; "nothing which came off a screen comes back out"
  covers pixels no less than words.

---

## Landings, smallest first

Each ships with a caller and a face, or it is `partial` on merge.

1. **Asset plumbing + image preparation.** `transport.call_service` carries
   `assets` and the danger-zone header; `osal` gains the prepare step
   (upright, ≤1568 longest side, ≥256/78,400, PNG/baseline JPEG). Hermetic:
   the limits are arithmetic.
2. **Pixels mode in `VisionExtractor`.** Behind the existing vision switch;
   words mode stays default; audit record unchanged (metadata-only);
   reservation switched to a pixel-aware upper bound.
3. **Frame history → stability judgment.** `state_not_stable` gains a
   calibrated input; the code guard remains the thing that stops the step.
4. **Box, pinned to `jev-1.14.0`.** Presence Noul + Box per target, edge-
   question repair ladder as the `confidence_below_threshold` remedy, box
   agreement (IoU) as a *reported* tally — not yet wired to any action.
5. **Per-action confidence ladder + guardrail pack** at the execution gate,
   thresholds declared in config and calibrated past the jitter band.

And the standing rule from the addendum, unchanged by any of this: the
hermetic suite cannot catch a pixel-path regression — a fake transport
neither tokenizes images nor reads a screen. **The live-provider gate is the
only thing that protects this tier**, and it is already an open canon item.

---

## Stale claims catalogued (to repair in the landing that touches them)

| Where | Claim | Status |
|---|---|---|
| `docs/harness-canon-jev-semantics-addendum.md` §5a | "CANONICAL CONSTRAINT — Jev is text-only" | superseded (noted there, 2026-10-14) |
| `docs/harness-canon-jev-semantics-addendum.md` §5 `JEV-SEM-text-only` row | "redirect any pixel-based proposal" | superseded by this doc's gate, not prohibition |
| `docs/jev-live-receipts.md` § constraint | "Jev cannot see images" | superseded (noted there, 2026-10-14) |
| `driver_core/extractors.py` `_capture_state` | "The endpoint is text-only" | false as of 2026-10-14; the *default* path is still words |
| `driver_core/osal.py` `describe_screen` | "This exists because the decision model cannot see" | the default path survives on cost and exactness, not on inability |

The receipts those claims rest on stay valid evidence against
base64-as-text. Nothing in the audit chain is rewritten by this document;
it is a new record, which is how this repo amends a compatibility surface.
