# Close `DRV-1`: a live protocol conformance check against a real driver-core service

*Written against driver-core `79b2658` (v3.4.0). Every figure and every status
code below was returned by a live service, not read from a document.*

---

## The goal

Your adapter is done. `harness/perception_client.py` (361 lines) plus
`harness/docs/driver.md` are, by your own roadmap, "the Harness half of the
driver-core integration and deliberately the whole of it". The hermetic suite
is green with no network, screen, model or driver process in the path. What
is missing is the last clause of your own acceptance criteria, which asks for:

> a **live protocol conformance check against a real driver-core service**
> (not a mock) showing route names, auth, refusal-as-200, and 4xx/5xx →
> `PerceptionUnavailable`.

That is the whole first landing. It is a check, not a feature.

**Do not close `DRV-2`.** Its row says so in as many words: real backends
behind driver-core's declared actions are "a driver-core roadmap item, not a
Harness one… not a Harness PR's to close." The same applies to the vision
extractor. If the check makes `DRV-2` look closeable, that is a finding to
report, not a gap to fill.

---

## Getting a service to point at

**You do not install driver-core into Harness, and you must not import it.**
`perception_client.py` contains zero references to `driver_core` — it speaks
HTTP and nothing else. This integration is a port, not a dependency, and
keeping it that way is a property worth protecting rather than a limitation
to work around.

So the only thing you need is a runnable service, and both of these give you
one:

```bash
pip install driver-core    # zero dependencies, stdlib only; installs a `driver-core` command
# or, from a checkout, with no install at all:
python -m driver_core --help
```

After an install the entry point is `driver-core serve`; from a checkout it is
`python -m driver_core serve`. Both are the same code.

---

## What to build

A repeatable command that takes a **reachable driver-core service** and
asserts the protocol contract against it, exiting `0` when the contract holds
and `1` with the specific divergence named when it does not. It should print
what it checked, because that output is the evidence that closes the
acceptance criterion.

It must run with **no screen, no model key, and no outbound network** —
every assertion below is reachable without them, and the check should prove
that rather than assume it.

### Starting a service yourself, exactly

If your check launches the service rather than expecting one to be running,
this is the sequence that works. It was run end to end; the three failure
modes below are the ones that actually bite.

```python
import json, os, re, subprocess, sys, tempfile, time, urllib.request

work = tempfile.mkdtemp(prefix="drv-conf-")
env = dict(os.environ)
env.update({
    "PYTHONUNBUFFERED": "1",                  # see below — this is load-bearing
    "DRIVER_TOKEN": "conformance-probe-token-1234",   # >= 16 characters
    "DRIVER_PORT": "0",                       # let the OS pick a free port
    "DRIVER_AUDIT_PATH": os.path.join(work, "audit.jsonl"),  # throwaway chain
})
for name in ("DRIVER_CLI_COMMAND", "DRIVER_DOM_URL", "DRIVER_MCP_COMMAND",
             "DRIVER_MCP_TOOL", "DRIVER_SCREEN", "DRIVER_HOST"):
    env.pop(name, None)                       # no tiers declared; see assertion 4

proc = subprocess.Popen(
    [sys.executable, "-m", "driver_core", "serve"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, bufsize=1, env=env, cwd=work)

base = None
deadline = time.time() + 20
while time.time() < deadline:
    line = proc.stdout.readline()
    if not line:
        break
    if "listening on" not in line:
        # Anything else on this stream is a startup refusal -- see below.
        print(line.strip())
        break
    base = re.search(r"listening on (http://[0-9.]+:\d+)", line).group(1)
    break
```

**The bound port appears on one line of stdout, and only if stdout is
unbuffered.** With `PYTHONUNBUFFERED=1` (or passing `-u` to the interpreter)
the first line is:

```
listening on http://127.0.0.1:55651
```

Without one, the service **binds, runs, and prints nothing at all** — the
line sits in a block buffer and never arrives, because the process is still
running and has no reason to flush. Verified: buffered launch gives zero
lines of output and a live process; unbuffered gives the line. If your check
concludes the announce does not exist, it is looking at a buffered pipe.

**Set `DRIVER_TOKEN` yourself.** With no token set, the service starts
normally, announces, and generates a token it **never prints** — so the check
comes up and cannot authenticate. `MIN_TOKEN_LENGTH` is 16.

**A short token fails loudly, on the same stream.** `DRIVER_TOKEN=short`
produces

```
[error] ConfigError: DRIVER_TOKEN is shorter than the 16-character minimum for a bearer token
```

and the process exits. So read the first line and branch on whether it says
`listening on` or `[error]`, or a misconfigured check will simply hang until
its own timeout and report a network failure.

**Clean up.** Terminate the process, and use a throwaway
`DRIVER_AUDIT_PATH` as above. A check that only probes and never steps
creates no chain file at all, so there is nothing to clean even then.

### Where to put it — your call, with one fact you need

`tests/test_osal_boundary.py` walks `PACKAGE = <repo>/harness` and nothing
else. `tests/` is **not** scanned, so a live checker there is legal, and
`tools/` or a standalone script is equally legal. What is not legal is
`harness/`, whose OS contact belongs to `harness/osal.py`.

That leaves a tension for you to resolve rather than one to be handed: a live
check in `tests/` runs in CI, which is where you probably want it — but
`DRV-1`'s value currently rests on that suite being hermetic, with "no
network, screen, model, or driver process in the path." A checker that starts
a driver process and runs by default would spend that property. Whether to
make it opt-in (skipped unless a variable is set), put it in a separate lane
of the suite, or house it outside `tests/` entirely is a call about your own
suite's shape, so make it, and say which you chose and why in the `DRV-1`
row.

### Version drift: report it, do not fail on it

`GET /health` returns `version`. **Print the value your check ran against and
carry on.** Do not fail the run on a mismatch.

The reason: the check asserts the *protocol*, not a build. The route names,
the closed reason set, the refusal-as-200 convention and the 4xx boundary
are the contract, and they are what this milestone closes. A patch bump that
keeps all of those intact has broken nothing, and a check that hard-fails on
a version string is a check its owner will be asked to silence within a month
— at which point it stops being run at all, which is worse than the version
drift it was guarding. If a version *does* coincide with a failing assertion,
the printed version is the evidence; if it does not, the version is a footnote.

---

## The contract to assert

| Route | Shape actually returned |
|---|---|
| `GET /health` | `{keyed, ok, settings, sources, status, version, vocabulary}` — `status: "up"`, `sources: []` when none are declared |
| `GET /vocabulary` | `{ok, vocabulary}`, `vocabulary.actions` a list of 14 declared actions |
| `GET /schemas` | `{ok, schemas}`, 3 declared schemas |
| `GET /verify` | `{ok, audit: {detail, ok, records}, budget: {ceiling_usd, entries, remaining_usd, reserved_usd, spent_usd}}` |
| `POST /step` | 11 keys: `step_id, ok, stopped_at, reason, detail, capture, agreement, decision, execution, receipt, cost_usd` |

Note the plural — `/schemas`, not `/schema`.

Seven assertions, each of which has been exercised against a live service:

1. **Route names.** All five answer; an unknown route returns 404.
2. **Auth.** No token → **401**. A token under 16 characters → **401**.
   A valid token → 200 on all five routes. (The short-token case is about
   *sending* a weak credential; the 16-character minimum is separately about
   *starting* with one.)
3. **Refusal-as-200.** `POST /step` with no model key configured returns
   **HTTP 200** with `ok: false` and `reason: "decision_not_usable"`. The
   adapter must **return** that envelope, not raise. This is the distinction
   the whole adapter exists to hold, and it is the one most worth proving
   against a real process rather than a fixture.

   This one needs a **declared source**, or it will read as
   `no_capture` instead and look like the check is wrong. Add
   `DRIVER_CLI_COMMAND="python -c print(1)"` to the launch environment above
   and drop it from the loop that clears the tiers. Capture then succeeds and
   the step stops at the decision, which is the case under test.
4. **A second reason, from the same closed set, with no key.** With no source
   declared — the launch environment exactly as written above —
   `POST /step {"target": "...", "schema": "cli"}` returns 200 with
   `reason: "no_capture"`. Two distinct reasons reachable with no key and no
   screen is enough to show the set is being honoured rather than one value
   being special-cased.
5. **4xx → `PerceptionUnavailable`.** `POST /step` with `schema` absent
   returns **400** with
   `{"error": "schema is required; declare one of ['cli', 'dom', 'gui', 'mcp', 'screen']", "ok": false}`.
   There is no safe default: the class is what makes "three of four targets
   never see pixels" enforceable, so the service asks rather than guesses.
   The adapter must raise here, and must not raise on (3).
6. **The reason set is byte-identical.** driver-core's `STOP_REASONS` and the
   adapter's are the same 9 names in the same order — verified today:
   `no_capture, insufficient_agreement, extraction_disagreement,
   confidence_below_threshold, state_not_stable, decision_not_usable,
   no_action_recommended, undeclared_action, execution_refused`.
   **Your `_check_contract` raises on an undeclared reason**, so any future
   addition on the driver-core side turns every real refusal into a
   `PerceptionUnavailable` at the boundary. Make the check compare the two
   sets and fail loudly on divergence — that is the cheapest insurance in
   this whole integration, and it is invisible until it is not.
7. **`step_id` round-trips as given.** Send one; the echo in the response is
   the same string. This is what lets Harness join its own log to the audit
   chain afterwards, and it is the only sanctioned join.

Optionally, and only if the service is configured for it: assert that
`/verify`'s `audit.ok` is `true` and that `records` is a number you do not
assert a value for — **record count grows every time anything runs**, so pin
nothing about it. If the check posts a `step_id` and can then read the audit
log, showing the two lines up is the strongest single piece of evidence this
integration has, because it is the join the whole design rests on.

---

## Constraints that must hold

- **Invent no capabilities.** Synthetic input (`click`, `type`, `key`) is
  **declared and refused** — there is no backend, and driver-core refuses
  rather than silently doing nothing and reporting success. A driver that
  claimed otherwise would be worse than one that refuses. Vision is declared
  but has no live provider. A first landing can observe and can be refused;
  it cannot act. Do not write a check that expects an action to execute.
- **The audit log is required on anything that acts.** driver-core refuses to
  construct a `Driver` or an `Executor` without a chain, and refuses an
  executor handed to a driver that does not write to that driver's own chain.
  Do not build a Harness path that runs an action outside it. If your lane
  ever registers a backend for a declared action, that backend runs *inside*
  driver-core's executor and is recorded — that is the design, not an
  inconvenience to route around.
- **The recorded chain is a compatibility surface.** A log written by any
  earlier version must still verify. So: do not rewrite, truncate, delete or
  re-point a chain; do not point a check at a developer's real
  `%LOCALAPPDATA%\driver-core\audit.jsonl` by default; give the check its own
  `DRIVER_AUDIT_PATH` or its own temporary directory, and say which.
- **Namespaces stay separable in both directions.** driver-core reads only
  `DRIVER_*` and never `HARNESS_*`, `OPENROUTER_*` or `FREEBUFF_*`; it has a
  test asserting so. Your side is asserted to read no namespace but its own
  either. Do not widen this in either direction to make configuration easier.
- **Do not change the wire contract to suit the check.** The 11 response keys,
  the 9 reasons, and the request field set are fixed. A check that "needs" a
  different shape is reporting a real divergence — report it, do not
  negotiate it.
- **No provider brand strings** in new modules, and the driver-core side owns
  every ladder and extractor policy. Your lane owns none of it.

---

## What done looks like

1. A command that, against a reachable real service, checks all seven
   assertions, prints each one with its result and the `version` it ran
   against, and exits non-zero naming the first divergence.
2. It runs with **no screen, no model key, and no outbound network** — every
   assertion above is reachable without them, and the check should prove
   that rather than assume it.
3. Its output is pasted onto the `DRV-1` row in `docs/jev-roadmap.md` as the
   evidence that closes the acceptance clause, together with where you put
   the check and why. That is the whole milestone.
4. **`harness/perception_client.py` is unchanged**, unless the check found a
   real divergence — in which case the divergence is the deliverable and the
   fix is a separate, argued change. Do not "improve" the adapter to make a
   check pass.
5. If the two `STOP_REASONS` sets have diverged, or a route name does not
   match, that is a finding for the driver-core lane and a genuine reason to
   pause. Say so rather than working around it.

**Explicitly not part of this landing:** registering backends for declared
actions, wiring vision, changing the adapter's shape, adding a
driver-core-side capability, or relaxing the 4xx/5xx distinction. All of
those belong to `DRV-2` or to the driver-core repository.
