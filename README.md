# driver-core

**Verified extraction → Jev decision → deterministic action.**

A driver that watches a machine, decides what should happen next, and acts —
where the *decision* is made by a fast, calibrated decision model ([Jev][jev],
TypeSafe's System One model) and the *acting* is done by code with no model
in it at all.

```
 capture ──► extract (N independent) ──► tally ──► GATE ──► decide ──► GATE ──► execute
 (structured  cheapest-capable         (deterministic)          (Jev)               (no model
  first)       models, in parallel)                                in)                 in this tier)
                                                            ↑ confidence
```

## The one idea worth reading

**Cross-verification happens before the decision model, never after.**

A System One model returns calibrated confidence about *the question it was
asked*. Feed it a hallucinated screen extraction and it will return a
beautifully calibrated, confidently wrong answer — about the wrong state. It
cannot detect the error, because the error happened upstream and is invisible
in the payload.

So agreement between independent extractors is established *first*, by
arithmetic, and a state that did not reach agreement never reaches the model
at all. Three rules make that real:

1. **A slot that did not answer is not a vote.** Two extractors that agree
   while a third times out is a *shortfall*, not unanimity — different
   remedy, different meaning, and conflating them teaches an operator to
   trust a green light lit by two votes out of three.
2. **The tally owns the verdict.** A specialist model may explain the tally;
   it may not overrule it.
3. **Per field, not per document.** Four agreed fields and one contested
   field is neither agreement nor disagreement — it is four agreed and one
   contested, and that is what lets a caller use the four.

## Why structured input first

A CLI's stdout, an MCP tool's JSON, and a DOM or accessibility tree are
already the state a human would reason about, in a form a program reads
exactly. A screenshot of the same application is a lossy rendering of it that
costs money to interpret and can be read two ways by two models.

So the order is `CLI → MCP → DOM → pixels`, and pixels are reached only when
the structured sources cannot answer. Most runs are therefore free,
deterministic, and reproducible — and the vision adapter is a small, isolated,
last-resort component rather than the core of the system.

### The ordering is structural, not advisory

"It tries pixels last" is easy to claim and easy to break by accident, so it
is enforced by types instead:

```python
from driver_core.perception import DOM, CliSource, DomSource, Target

capture = select_capture(
    Target("dashboard", DOM),
    [DomSource("https://example/report"), screen_source])
```

A `Target` carries the class it was declared to be — `cli`, `mcp`, `dom`, or
`gui` — and every source declares which classes it serves. For a `cli`, `mcp`
or `dom` target the screen source **is never a candidate**, so it cannot be
called, billed, or quietly preferred. The claim becomes checkable: a source
that would violate the ordering cannot be reached, and a test can assert it
with a screen source that raises if touched.

| class | answered by | cost |
|---|---|---|
| `cli` | `CliSource` — a real argv subprocess | free |
| `mcp` | `McpSource` — real JSON-RPC over stdio | free |
| `dom` | `DomSource` — real fetch, stdlib HTML parser | free |
| `gui` | `ScreenSource` → `VisionExtractor` | billed |

Three of the four classes never reach a vision extractor, and extraction is
a *pool* — so the alternative is paying N times to describe a lossy rendering
of state that was already available exactly.

### The vision tier uses the same client as everything else

`VisionExtractor` reaches the model through the same `Settings`, the same
`SYSTEM_ONE_URL`, the same `Budget`, the same `AuditLog`, the same
`transport`, and the same price list the decision tier uses. There is no
second provider, no second key, and no way to declare one — the constructor
has no parameter through which a caller could introduce a different endpoint.

That consolidation is the point rather than tidiness. The vision extractor
previously took its own `endpoint` and `api_key`, so extraction could be
pointed at a different model than the decision tier, and it charged
**nothing at all** — meaning a vision pool could spend straight past a run
ceiling sized for the decision tier alone. Extraction is a pool, so that was
N unaccounted calls per step. Vision spend is now on the shared budget and
settles like the decision tier's: `actual` when the provider reports usage,
`estimated` when it reports some, and the **full reservation** when it
reports none — never zero.

### Nothing which came off a screen comes back out

The image goes in, validated fields come out, and nothing in between is
written into an envelope, a result, or the audit chain. The vision audit
record is an explicit allowlist of metadata — `ok`, `reason`, `model`,
`cost_usd`, `usage_source` — so a field added to `Extraction` later cannot
leak into the log by default.

That is enforced by a test with a sentinel rather than by care: a capture is
loaded with a unique marker, the fake provider is made to **echo that marker
back** in its response (the hostile case — it models a provider including
the request echo), and the audit chain, the votes, the consensus receipt and
the on-disk chain are all searched for it. The guard has teeth: injecting a
one-line leak into the audit record fails four of those tests.

A bare string target is still accepted, and is deliberately weaker: an
undeclared class permits any source, pixels last. That preserves every
existing caller while making the strong form available to those who want it.

When nothing answers, the refusal says whether the screen tier was *excluded
by class* or merely declined — different problems, and the first has an
obvious fix.

### What each tier actually does

All three structured tiers are stdlib-only, deliberately:

- **`CliSource`** runs a declared command through `osal.run` and returns its
  exit code, stdout and stderr. The target is appended as its own argv
  element rather than substituted into the command, so a hostile target can
  only ever be one argument.
- **`McpSource`** speaks newline-delimited JSON-RPC to a declared child
  process: `initialize`, `notifications/initialized`, then `tools/call`. Text
  content blocks are **refused, not parsed** — they are the MCP equivalent of
  reading pixels, and parsing them here would reintroduce the unreliable tier
  at the end of a chain whose argument is that the structured tiers are not
  guesses.
- **`DomSource`** fetches a declared URL through `osal.http_get` and extracts
  the title and visible text with `html.parser`. Script and style bodies are
  dropped: they contain the words a schema asks for most often, and a vision
  model does not read them either.

The DOM target is **not** substituted into the URL. A declared URL is safer
and more auditable than splicing a caller string into one.

### Declaring a source

The tiers are useless unless something registers them, so the shipped CLI and
service read them out of the same `DRIVER_*` namespace as everything else:

| Setting | Tier it enables |
|---|---|
| `DRIVER_CLI_COMMAND` | `cli` — a command, tokenised and run |
| `DRIVER_MCP_COMMAND` + `DRIVER_MCP_TOOL` | `mcp` — both, or neither |
| `DRIVER_DOM_URL` | `dom` |
| `DRIVER_SCREEN` | `screen` — the vision tier, and the only thing that spends |

```bash
$ DRIVER_CLI_COMMAND='python -c "import sys; print(sys.argv[1])"' \
      driver-core --json step --schema cli "my-app"
{
  "agreement": {"outcome": "agreed", "asked": 2, "answering": 2, ...},
  "capture": {"detail": "", "fingerprint": "c3cd13068bb7c667", "ok": true,
              "source": "cli", "target": "my-app"},
  "detail": "decision status 'unkeyed': no decision key configured",
  "ok": false,
  "reason": "decision_not_usable",
  "receipt": {"schema": "driver-core-cli@1.0.0", "extraction_id": "d9f55b19ec13", ...},
  "step_id": "d9f55b19ec13",
  "stopped_at": "decision",
  ...                                   # cost_usd, decision, execution
}
```

A real child process ran, both free extractors read it, the tally was
unanimous, and the step stopped at the decision tier because no key is
configured — which is the whole pipeline behaving correctly on a machine
without credentials. Nothing off the capture appears anywhere in that
response, and `driver-core verify` re-hashes the chain afterwards.

Ask for a tier that is not configured and it says so rather than
substituting another one:

```bash
$ driver-core step --schema gui "my-app"
[refused] no_capture: target 'my-app' is declared 'gui' and no configured
source serves that class; nothing was tried. Configured sources: ['none'].
```

Two rules make this safe to expose as configuration at all:

- **A tier is off unless it was named.** Nothing is inferred from another
  setting's presence — in particular, no other setting enables pixels, and
  `health` reports the sources the driver can actually observe rather than
  leaving you to infer it from a `no_capture`. With nothing declared the
  driver observes nothing and says so; it never quietly falls back to a
  screenshot.
- **A declared command is tokenised, never shelled.** `shlex` splits it and
  nothing else happens: no globbing, no `$VAR`, no `|`. A configuration
  string must not be able to become code — that is the one rule
  `driver_core.osal` exists to enforce, and a redirect written into
  `DRIVER_CLI_COMMAND` is a filename argument rather than a redirection.

A `dom` target is validated against its own schema (`window_title`, plus
optional `visible_text`) rather than the screen schema, because a document
cannot honestly report which application is in the foreground or whether an
error dialog is up. Inventing either would be a guess presented as an
observation.

## Guarantees

| | |
|---|---|
| **A disagreement never reaches the model** | enforced in the pipeline; the test asserts the fake decision client was never called |
| **Three of four targets never see pixels** | declared per target class; a screen source is not a candidate for `cli`, `mcp` or `dom` |
| **A tier is off unless it was named** | no setting enables another; the vision tier has its own switch, and `health` reports the sources the driver can observe |
| **An absent or unknown `schema` is refused** | 400, not a default — the old default left the class undeclared, which permits pixels last, so the strongest tier was reachable by forgetting a field |
| **Screen content never comes back out** | a sentinel test greps the audit chain, votes and receipt; a one-line leak fails it |
| **Unanswered ≠ agreeing** | three distinct outcomes: `agreed`, `disagreed`, `insufficient` |
| **Spend is refused before dispatch** | reserve → dispatch once → settle once; a call whose usage is unreadable is charged at the full estimate, never zero |
| **No model past the action tier** | executors are named, registered, and closed; an unregistered name is a refusal, not a skip |
| **Consent is one exact `(action, params)` pair** | no wildcard exists, for anything with consequences; see below |
| **Writing is off until you say so** | mutating and irreversible executors are registered only under `DRIVER_ALLOW_WRITE` |
| **Irreversible actions need exact consent, once** | bound to the action *and* its resolved parameters, and spent by the use it authorised |
| **Responses never carry screen content** | captures are summarised by fingerprint; the agreed value is opt-in, not the default |
| **Every step is auditable** | hash-chained log; `verify` re-hashes the chain and says so out loud |
| **OS contact lives in one module** | an AST scan fails the build if anything else imports `subprocess` or probes `os.name`/`sys.platform` |

## Consent, precisely

A consent is a **capability for one exact `(action, params)` pair**. It is
reusable — that is what makes "click Next five times" safe under a single
confirmation — but it is not transferable, and **there is no wildcard**.

| class | requirement |
|---|---|
| `read_only` | none; observation is not a consequence |
| `mutating` | exact action, exact params |
| `irreversible` | exact action, exact params, **once** |

Three things follow from that, and each of them is a refusal rather than a
convention:

- **A blanket grant authorises nothing.** `{"granted": true, "action": "*"}`
  is not "everything mutating"; it is nothing with consequences. Somebody who
  typed *yes* without being shown a path has not agreed to delete that path,
  and a rule that pretends otherwise teaches an operator that a green
  checkbox means less than it appears to.
- **Params are compared in the resolved form.** Consent for `~/notes` does not
  match a proposal of `/home/x/notes`. The path is resolved *before* anything
  is displayed, so what the operator is shown, what is compared and what runs
  are the same string. Resolving during comparison would silently widen every
  grant.
- **An irreversible consent is spent by its use.** A standing grant cannot
  mean "delete this path, whenever this driver next runs", because nobody saw
  the path when they said yes on the first run. A second deletion needs a
  second person.

Registration and consent are two independent gates and both are required.
Registration answers *whether the capability exists*; consent answers *whether
this call was approved*. A build that cannot write cannot be talked into
writing.

## What driver-core will not do for you

**It ships no input backend.** Moving a mouse or a keyboard needs platform
APIs — `SendInput` on Windows, Accessibility on macOS, XTest on Linux — and
each arrives as a dependency or as `ctypes` against a DLL whose ABI is not
this package's to depend on. A driver that acts on a machine is the last
place to add a supply chain to. So `osal.send_input` is a *declaration* with a
pluggable backend, and the shipped state is unregistered:

```python
from driver_core import osal

def my_backend(kind, *, value=None, target):
    ...                        # your platform code, your permission checks
    return True, "sent"

osal.register_input_backend(osal.platform_name(), my_backend)
```

Until you do, `click`, `type_text`, `press_key`, `focus`, `scroll` and
`submit_irreversible` **refuse by name** rather than appearing to succeed
while doing nothing. `osal.disable_input_backend()` withdraws one again, which
is how a host arranges to observe a machine without being able to drive it.

The two filesystem actions need no backend — `write_file` backs up and then
replaces atomically, and `delete_file` refuses a missing path, a symlink and a
directory before it commits — so driver-core performs those itself.

## Install

```bash
pip install driver-core          # zero dependencies, stdlib only
```

## Use

```bash
driver-core health                       # settings, budget, audit state
driver-core vocabulary                   # the closed action set
driver-core schema                       # the declared extraction schemas
driver-core --dry-run step "my-app"      # rehearse a step; no side effects
driver-core verify                       # re-hash the audit chain
driver-core serve --print-token          # loopback REST surface
```

A step needs to know *what kind* of thing it is observing, and that is
explicit on both surfaces — `--schema` on the CLI (default `gui`) and a
required `schema` in the body of `POST /step`. It is not cosmetic: the class
decides which sources may answer, and an undeclared class permits any of
them, pixels last.

```bash
DRIVER_CLI_COMMAND='python -c "import sys; print(sys.argv[1])"' \
    driver-core step --schema cli "my-app"
```

Consent on the command line names one action and its parameters, and echoes
the resolved pair before acting — because a person has to be able to see what
they are agreeing to:

```bash
DRIVER_ALLOW_WRITE=1 driver-core step "my-app" \
    --grant delete_file --grant-path ./drafts/old.txt
# [consent] {"action": "delete_file", "by": "cli", "granted": true,
#            "params": {"path": "/home/you/drafts/old.txt"}}
```

There is no `--grant-write`. It used to exist and used to authorise every
mutating action; under the current law it would authorise nothing at all, and
a flag that quietly does nothing is worse than no flag.

```python
from driver_core.driver import Driver
from driver_core.perception import CLI, Target
from driver_core.states import CLI_SCHEMA

driver = Driver()                       # budget, audit and executors injected
                                       # sources come from DRIVER_*, as declared
result = driver.step(Target("my-app", CLI), schema=CLI_SCHEMA)

if result.ok:
    print(result.execution.action, result.decision.confidence)
else:
    # A refusal is a normal, successful outcome -- the system working.
    print(result.reason, result.detail)
```

A default `Driver()` observes nothing, because no setting declares a source.
That is deliberate: it refuses with `no_capture` and tells you what *is*
configured, rather than reaching for pixels on your behalf. Pass
`sources=(...)` to register your own in Python instead — `()` means
"explicitly none".

## Configuration

Every setting reads from the `DRIVER_*` namespace and nothing else. This is
load-bearing, not cosmetic: driver-core runs beside other projects with work
in flight, and it must never read another project's keys or state.

| Variable | Default | Meaning |
|---|---|---|
| `DRIVER_JEV_API_KEY` | — | decision key; unkeyed means `unavailable`, never a guess |
| `DRIVER_JEV_MODEL` | `jev-latest` | |
| `DRIVER_QUORUM` | `2` | minimum slots that must *answer* |
| `DRIVER_MIN_AGREEMENT` | `1.0` | unanimity among those who answered |
| `DRIVER_CONFIDENCE_THRESHOLD` | `0.70` | below this, escalate rather than act |
| `DRIVER_STEP_CEILING_USD` | `0.50` | per-step pre-flight ceiling |
| `DRIVER_RUN_CEILING_USD` | `5.00` | per-run ceiling |
| `DRIVER_DRY_RUN` | `false` | rehearse without side effects |
| `DRIVER_ALLOW_WRITE` | `false` | register the executors that have side effects |
| `DRIVER_AUDIT_PATH` | OS state dir | the hash-chained log |
| `DRIVER_CLI_COMMAND` | — | declare the `cli` tier; tokenised, never shelled |
| `DRIVER_MCP_COMMAND` | — | declare the `mcp` tier (needs `DRIVER_MCP_TOOL` too) |
| `DRIVER_MCP_TOOL` | — | the tool to call; half a declaration enables nothing |
| `DRIVER_DOM_URL` | — | declare the `dom` tier |
| `DRIVER_SCREEN` | `false` | declare the vision tier; the only source that costs money |

The bottom five are the declared perception sources. Each is off unless set,
`health` reports which are live, and none of them is inferred from another's
presence. See [Declaring a source](#declaring-a-source).

## Integrating into a host project

The REST surface is the seam, and it is deliberately plain:

```
GET  /health      settings, key state, vocabulary, and which sources are live
GET  /vocabulary  the declared actions
GET  /schemas     the declared extraction schemas
GET  /verify      audit chain verdict + spend
POST /step        {"target": "...", "schema": "cli", "prefer": ["cli"],
                  "consent": {...}, "step_id": "..."}
```

`schema` is **required**. It binds the target to one declared class, and
anything absent, blank or unrecognised is a 400 naming the valid names. That
is not pedantry: the class is what makes "three of four targets never see
pixels" enforceable, and the old behaviour — defaulting to the screen schema
while leaving the class undeclared — meant the one request a caller made
without thinking was the one that could reach both the strongest structured
tier and pixels. There is no safe default, so the service asks.

`step_id` is optional and is used as given, so a host can join its own log to
the audit chain afterwards. Nothing else off a screen crosses the boundary in
either direction.

Three conventions a host can rely on:

- Every response carries `ok` and, when false, a `reason` from a **closed
  set** (`no_capture`, `insufficient_agreement`, `extraction_disagreement`,
  `confidence_below_threshold`, `state_not_stable`, `decision_not_usable`,
  `no_action_recommended`, `undeclared_action`, `execution_refused`).
- **A refusal is HTTP 200 with `ok: false`.** Only malformed requests are
  4xx. A caller retrying on 5xx must never be retrying a *decision*.
- No response echoes what was on a screen.

A `consent` object in a `POST /step` body is bound to exactly one action and
one exact parameter set:

```json
{"target": "my-app", "consent": {"granted": true, "action": "write_file",
                                 "params": {"path": "/abs/report.md",
                                            "content": "..."}}}
```

`action: "*"` is accepted and authorises nothing with consequences. Send the
action and the resolved parameters you mean.

`tools/interference_check.py` in this repository is a worked example of the
four-part check used to confirm this package has not disturbed a neighbouring
repository while it is being built.

## Security, honestly

The server binds loopback and requires a bearer token. **That is a floor, not
a security claim.** A local process can read your screen, and a driver holding
that capability deserves more scepticism than a token header provides. Before
pointing this at a real machine:

- start with `--dry-run` and read what it *would* do;
- run with a read-only vocabulary until you trust the extraction;
- leave `DRIVER_ALLOW_WRITE` off until you trust the consent model;
- keep the step ceiling low until you trust the cost;
- treat an irreversible action as requiring you, in the loop, every time.

## Development

```bash
python -m unittest discover -s tests -t .    # 283 hermetic tests
ruff check driver_core tests tools
python tools/tier_order_run.py               # the tier chain, live
python tools/live_action_run.py              # a declared action, live
python tools/vision_run.py                   # the vision tier (needs a key)
python tools/configure_run.py                # declared settings -> live sources
```

`tools/configure_run.py` drives the shipped CLI and the REST service over a
real socket with nothing but `DRIVER_*` variables set: a real child process
observed through `step --schema cli`, a real document fetched for `--schema
dom`, a `>` in a declared command that is *not* a redirection, and an absent
`schema` refused with a 400 rather than defaulted.

`tools/vision_run.py` uses `DRIVER_JEV_API_KEY` and the real endpoint when a
key is configured, and **reports that the live call was skipped when one is
not** — it never substitutes a provider, because a run that passes against a
stand-in is evidence of nothing.

The suite is hermetic: no network, no model, no screen, no disk. Fakes live in
`driver_core/ev.py` and are injected at the seams, which is only possible
because every collaborator is a constructor argument.

[jev]: https://docs.typesafe.ai/introduction/coding-agents.md
