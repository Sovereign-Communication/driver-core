# Design

Why the pieces are in this order, and what was rejected.

## The decision this design exists to make safe

Put a model in a loop that acts on a real machine and you get one of two
things. Either the model is fast, structured, and calibrated — in which case
it is trusted with the *decision* and everything interesting can be made
deterministic around it. Or it is a general model that writes text and calls
tools — in which case it is doing the acting, and every step of what it did
has to be inferred afterwards from logs.

[Jev][jev] is unambiguously the first kind. TypeSafe's own documentation is
blunt about it:

> Jev does not generate text, write code, or hold a conversation. […] There is
> no `model: "jev-latest"` setting that turns your coding agent into a
> Jev-powered agent.

That is not a limitation to work around; it is the property the whole design
is built on. A model that can only answer `noul` / `choice` / `score` against
a question *we* wrote cannot invent a command, invent a path, or invent a
selector. It picks a name. We do the rest.

The consequence is that **almost all of the safety lives in code, and almost
none of it lives in the prompt.** That is the whole thesis.

## The ordering problem

The obvious pipeline is capture → describe → decide → act. It is wrong, and
the reason is specific rather than aesthetic.

A System One model's confidence is calibrated *for the question it was
asked*. If the question was "given this state, what should happen?" and the
state was a hallucinated reading of a screen, the model will answer
confidently about the hallucination. Its calibration is intact; the input
was wrong. No amount of confidence gating catches this, because the
confidence is a correct statement about a false premise.

So verification has to happen *upstream* of the model, and the only way to
establish that N independent observers saw the same thing is to have N
independent observers and compare them arithmetically. Hence Tier 2 exists
before Tier 3, and the gate between them is the most important line in the
codebase:

```python
gate = policy.check_agreement(agreement, ...)
if not gate.passed:
    return self._stop(step_id, "agreement", gate.reason, gate.detail, ...)
```

A blocked extraction returns immediately. It never continues with a
half-built state, and the decision client is never called. The test asserts
this by giving the fake decision client a spy and checking it was not called
— which is the only way to observe a *negative* safety property.

## Why a shortfall is not agreement

The tempting simplification: "two of three agreed, so the state is fine."
It is fine, and it is also the single most dangerous line in the system,
because it teaches the operator to read a green light that was lit by fewer
votes than it claims.

The remedy differs by cause. A shortfall means *retry or fall back to a
structured source* — the extractors were rate-limited, or the capture was
unreadable. A disagreement means *escalate* — the observers genuinely saw
different things, and something about the screen or the schema needs human
attention. Collapsing them into one outcome means every disagreement gets
retried forever and every shortfall gets escalated to a person who could have
just waited five seconds.

So `Agreement` has three outcomes, and `unanswered` is carried in the receipt
even when the outcome is `agreed`, so a shortfall stays visible downstream
rather than being forgotten the moment the quorum was met.

## Why a malformed answer is a non-answer

A model that returns something which does not validate has not answered
*differently*; it has failed to answer. Counting it as a dissent would let
one garbage response dilute a field that every other extractor agreed on,
turning a clean two-against-nothing into a one-against-one.

The tally therefore demotes malformed answers to non-answers before counting,
and the agreed state is taken from one real extractor's normalised output
rather than merged. Merging would mean the shipped state is something no
model actually said.

## Why structured input comes first

Three of the four supported target classes — CLI, MCP, and the DOM — already
expose their state in machine-readable form. Screenshots are a lossy
rendering of information the program can hand over exactly.

| | structured | pixels |
|---|---|---|
| determinism | exact | varies with theme, scroll, animation |
| cost | $0 | per-capture, per-extractor |
| testability | hermetic | needs a fake vision model |
| accuracy | exact | bounded by the extraction |

So the order is `CLI → MCP → DOM → pixels`, and the vision adapter is one
isolated component in one segment. If it is never finished, or is abandoned,
everything else still works — which is a property worth more than the
convenience of one uniform mechanism.

## Why the vocabulary is closed

`Vocabulary.resolve` raises on an unknown name. There is no default, no
fuzzy match, and no case-insensitive fallback. `Observe`, `observe ` and
`OBSERVE` are all refused.

The reason is that an action name is a *capability grant*. If the code has
not declared `delete_file`, then the code has never been reviewed for it, has
no executor for it, cannot bound it, cannot log it, and never prompted a human
about it. Resolving a near-miss to the adjacent real action would silently
grant a capability that was never declared — a class of bug that is
catastrophic in proportion to how helpful it feels at the call site.

A related decision: the decision tier supplies **no parameters**. It names an
action; it does not name a path, a selector, or a string to type. A model
inventing arguments is exactly the failure the closed vocabulary exists to
prevent, and letting it name only the action closes the whole class.

## Why no model in the action tier

The executor registry is closed, handlers are named, and none of them
interpret anything a model produced beyond the action name. Every one of them
is a small function that does one declared thing.

The payoff is that the highest-consequence part of the system — clicking,
typing, deleting — is ordinary code you can read, test, and review, with zero
model behaviour in it. A general model in that position would mean every
action's semantics lived in a prompt, and "what did it actually do" would be
a research question rather than a log line.

## Consent is bound, not global

`Consent` carries the action *and* the parameters it was granted for. An
irreversible action is executed only when the current parameters match the
ones a human was shown. A confirmation for `delete_file` on one path does not
authorise it on another, and no irreversible action may share a batch with
anything else.

That last rule exists because of a specific failure: a person asked to
confirm "delete this file" says yes, and a system that had batched three
consequences into one prompt has laundered two of them past a human who was
never asked.

## Spend: refuse before, never settle a bill you could not

The budget reserves worst case, dispatches at most once, and settles once.
Three details carry the weight:

- **Reserve before dispatch.** A guard that settles a bill it could not have
  refused is a report, not a control.
- **An unreadable cost is charged at the estimate.** A provider that reports
  no usage leaves the call charged at its reservation and labelled
  `unavailable`. Zero is the one number a spend guard may not be handed as
  fact.
- **Live reservations are tracked.** A leaked reservation holds its share of
  the balance forever, and `open_reservations()` makes that visible. The first
  implementation only recorded settled entries, which meant the one thing
  this was for — catching a leak — reported nothing.

The concurrency tests use real threads, because the failure being defended
against is a check-then-act race that a sequential test cannot produce.

## Why OS contact is fenced

`tests/test_declarations.py` walks the AST of every module and fails the build
if anything outside `osal.py` imports `subprocess` or reads `os.name` /
`sys.platform`.

This is not tidiness. A subprocess call written inline in an executor, and the
same call in a CLI, each develop their own idea of what an argument list looks
like on the platform they happen to run on — and the divergence appears only
on the platform nobody tested. The scan is also written against the AST rather
than as a grep, so `import subprocess as sp` is caught too, and a companion
test asserts the owner still *uses* what is banned, so the guard cannot pass
vacuously after a refactor.

That scan earned its place during the build: it caught a stray
`import subprocess` left in `executor.py` from an earlier revision.

## Responses that are safe to log

`Agreement.to_dict()` omits the agreed state, and `FieldAgreement.to_dict()`
omits the field values, unless explicitly asked. The default projection says
*which* fields were agreed and how strongly without reproducing what they
said.

This was found by a test, not by review. The original `to_dict()` returned
`state` because the decision tier needed it — and the same method was being
used to build the API response, which meant every response carried a
description of the operator's screen. A convenience default that is safe for
one caller and unsafe for another is a defect, and the safe behaviour has to
be the default.

## Where each fact is owned

Structure is only worth having where a fact has exactly one home, so this is
the short version: for each thing the driver knows, there is one module that
may change it, and the ones that used to be stated twice now are not.

| Fact | Owner | Everyone else |
|---|---|---|
| The four target classes, `Target`, `Capture`, `StructuredSource` | `observation` | the vocabulary, imported by everyone below |
| The tier order, and what a refusal says | `chain` | names no adapter, and no adapter can name it |
| How a CLI / MCP / DOM / screen source reads one system | `adapters` | knows no order, no other adapter, no tier |
| How a JSON-RPC stream and an HTML document are read | `parsing` | stdlib only; knows no source, target or tier |
| Everything above, under one import | `perception` (façade) | defines nothing; nothing below imports it |
| The schemas, and which class each one serves | `states` | asks `resolve_wire_target` |
| What a wire name means, or that it means nothing | `states.resolve_wire_target` | the CLI and the service both call it; neither re-decides |
| What was declared (`DRIVER_*`) | `config.Settings` | pure data, no perception imports |
| Which tiers are actually live | the driver's `sources` | `health` reports the list it holds |
| The ordered chain a step may consult | `Driver.sources` | `screen` is derived from it |
| The pipeline order and the budget per step | `Driver` | — |
| Whether a request is authorised | `Handler._authorised` | the only place a token is read |
| Whether a declared token is acceptable | `config.validated_token` | one rule, applied where `DRIVER_TOKEN` is read |
| Which port and host were bound | `serve`, from `server_address` | not from the arguments it was handed |
| How a declared command becomes an argv | `wiring.parse_argv` | one rule, no escapes, nothing guessed |
| What a record in the audit log is called | `audit.KIND_*` | producers import the name; no module writes the string |
| Endpoint logic, free of HTTP | `Service` | `Handler` adds transport and nothing else |

Seven of these were stated more than once, or crowded together, and each had
already cost something:

* **The audit log declared a vocabulary its own producers never spoke.**
  ``audit.py`` held seven ``KIND_*`` constants; ``driver.py``,
  ``executor.py``, ``extractors.py`` and ``jev_client.py`` each wrote their
  record kind as a bare string. Six kinds were really in use, one
  (``KIND_CONSENT``) named a record nothing could produce, and nothing tied
  the two lists together — so a renamed constant would have left the log
  writing a kind it no longer declared, with no test able to notice. It is
  the drift the last three passes removed elsewhere, sitting in the one
  artifact where a record's meaning is the whole point.

  The producers now import the names, and ``tests/test_audit_compat.py``
  pins the chain: an existing log must still verify, and the same run must
  still write the same bytes. That second test is the one that catches a
  renamed constant, because a log can verify perfectly while meaning
  something new. ``KIND_CONSENT`` was deleted rather than wired up — a
  consent is already recorded inside the ``action`` record it authorises,
  with the exact parameters it was bound to, and adding a second record for
  the same fact would change what a run writes.

  Keeping that from drifting again needed the guard itself to hold. The
  first version scanned four hand-listed modules for a receiver named
  ``audit``, so a fifth module, or a local ``log = self.audit``, wrote a
  record kind as a string with the suite green — the same duplicated fact
  one level down, inside the check meant to prevent it. The scan now covers
  every module in the package and keys on the shape of the call: a string
  literal may only be appended when nothing is passed by keyword, because a
  record always carries fields and the bare strings in ``jev_client`` never
  do. The producers are derived from the code rather than listed, and
  ``audit`` is the only module permitted to declare a name.

  ``states.FREE_CLASSES`` went in the same pass. It was byte-identical to
  ``observation.STRUCTURED_CLASSES``, had no readers anywhere, and existed
  so the "three of four classes need no pixels" claim could be asserted —
  against a copy, which asserts nothing about the real list.

* **`parse_argv` used POSIX `shlex`, which is a shell, not a tokeniser.**
  `shlex` reads `\` as an escape outside quotes, so
  `C:\Python314\python.exe server.py` resolved to
  `C:Python314python.exe`. The source reported itself configured, and the
  step failed at run time naming a path the operator never typed — which
  disables the `cli` and `mcp` tiers with an absolute interpreter path on
  Windows, the platform this project tests on most. The rule is now total:
  **whitespace separates, quotes group, and a backslash is never an escape.**
  There is deliberately no `\"` exception either, because keeping it
  reproduces the identical bug for a path ending in a separator
  (`"C:\Users\me\"` loses its last backslash and its closing quote). A rule
  with no exceptions cannot be misapplied in a way nobody can predict, and
  the one thing the previous rule could not do — guess — is now a refusal
  with a message naming the fix.* **The REST token was checked but could not be held.** `Service` minted one
  per process and the only way to read it was a CLI flag that bound the port,
  printed, and exited — so the credential it produced belonged to a process
  that no longer existed. The token is now a declared `DRIVER_TOKEN`, which is
  also where the one rule about its acceptability lives, and `redacted()`
  deliberately has no `token` key rather than a masked one: a host that needs
  the token already holds it, and a host that does not must not be able to read
  it off an endpoint any local process can reach. The asymmetry is worth
  stating: a *configured* token is policed, an explicit `Service(token=…)` is
  not, because that caller wrote the credential in the same breath as the code
  presenting it and can already reach every executor.
* **`serve` reported the arguments it was given, not the socket it bound.**
  `cmd_serve` printed `http://{args.host}:{args.port}` from argparse defaults
  that are `None`, so the operator was told the service was on
  `http://None:8791` — and only in the branch that was *not* listening, while
  the branch that was listening said nothing. The address now comes from
  `server_address` through an opt-in `announce`, which is the only moment it is
  knowable. The same function also treated `port=0` as unset, so the one value
  a caller passes meaning "any free port" silently became 8791; the host keeps
  the opposite rule on purpose, because `bind("")` is `INADDR_ANY` and this
  server promises loopback only.
* **The perception tier was one file carrying five concerns** — chain policy,
  a generic source, three concrete adapters, JSON-RPC response parsing, and an
  HTML parser written as a closure inside `read_document`. Chain policy
  sitting beside HTML scraping meant a change to how the chain picks a source
  required understanding how a document is scraped, and a scraping bug was
  findable next to tier selection. It is now four owners, with the arrows
  pointing one way: `parsing` and `observation` know nothing of the tier,
  `adapters` cannot see the order it is tried in, and `chain` needs no
  concrete adapter to pick one. The seam is enforced rather than documented —
  `tests/test_declarations.ModuleSeamTests` walks the import graph and fails
  the build if an edge reappears. It has teeth: making `adapters` import
  `chain` and `chain` import `adapters` does not merely fail the test, it
  raises `ImportError` at collection, because a cycle is exactly what those
  two edges would create.
* **`Settings.declared_sources()` and `wiring.configured_sources()`** both
  answered "which tiers are live" from the same four settings. Two lists that
  had to be kept in step by hand, and a pure-declaration module that had to
  import the perception taxonomy just to order one of them. The driver holds
  the truthful answer, so the settings-level copy is gone and `/health`
  reports the list the driver actually has.
* **`Driver.sources` and `Driver.screen`** were two handles onto one set,
  reconciled by a method every caller had to remember to use. Getting it
  wrong was not hypothetical — it reported the vision tier twice and would
  have captured the same pixels twice on every step. There is now one list,
  and `screen` is a property that reads it.
* **"Resolve a wire `schema`"** was a bare lookup in the CLI and a
  lookup-or-refuse in the service, with the CLI defaulting to `gui` and the
  service refusing. The rule now lives once, beside the table it interprets;
  what each surface *does* about a bad name (print it, or answer 400) is its
  own business and stays there.

The one deliberate asymmetry left in place: the CLI still defaults
`--schema` to `gui` and the service still refuses an absent `schema`. A
person at a terminal should not have to type a flag to look at something; a
host integrating over HTTP should have to say what it is looking at. That is
a difference of interface, and it is commented at the CLI's call site rather
than smuggled into the shared rule.

The other thing worth stating: `Driver`'s collaborators are each *declared or
supplied*, never merged. Passing `pools` gives exactly those pools. The
earlier version merged the declared pools underneath whatever the caller
passed, which meant the same argument meant "the declared ones" or "none"
depending on the other arguments, and needed a three-way sentinel dance to
express.

## What was rejected

**Splitting `osal.py` into a package.** Rejected, deliberately.
:mod:`driver_core.osal` is 534 lines carrying five concerns — process
running, screen capture across three platforms, synthetic input, an HTTP
fetch, and filesystem policy — and every one of them is a place the package
touches the machine. Splitting it would make each file shorter and the
package harder to audit, because the property worth protecting is not "each
module has one concern" but "**one module does all OS contact, and a scan
proves it**". A tree of `osal.*` modules blurs exactly the boundary that
makes the guarantee checkable, and the AST test in
``tests/test_declarations.py`` would have to keep reasoning about a package
instead of a file. The 534 lines are the cost of the guarantee; they are
paid knowingly rather than discovered later.

**A general vision model in the action loop.** Rejected: the action tier
would contain no reviewable code, and the cost of a misread screen would be
paid in real effects rather than in tokens.

**Letting the decision tier supply parameters.** Rejected: closes the
vocabulary's guarantee. A path invented by a model is a path nobody checked.

**One extractor with a retry.** Rejected: a retry is the same observer twice.
It halves the cost of a network failure and adds no independent evidence.

**Merging disagreeing field values into a best guess.** Rejected: the merged
value is something no extractor said, and a `no_action`-style output is a far
better failure.

**Pooling all extractions globally and comparing across captures.** Rejected:
it would correlate errors — a misread window title would then be
"confirmed" by the next capture, because the pipeline would be comparing two
observations of different moments.

## Known limits

- Synthetic input (`click`, `type`, `key`) has **no backend**. It is declared
  and refused, and the embedding application must register one. A driver that
  silently did nothing and reported success would be worse than one that
  refuses.
- Vision extraction is a single model per slot, with real providers still to
  be wired; the extraction *tier* is proven, the provider is not.
- The audit chain proves the log has not been edited. It does not prove the
  driver was right.
- Cost per extraction is an estimate until a live run is measured.

[jev]: https://docs.typesafe.ai/introduction/coding-agents.md
