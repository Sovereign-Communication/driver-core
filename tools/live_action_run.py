"""A LIVE end-to-end run: really executes a declared action, for real.

The hermetic suite cannot demonstrate the thing that matters most about this
package. Every test injects a fake decision client, a fake perception source
and a fake transport, which is exactly what makes them fast -- and it is also
what makes them blind to the tier where a mistake turns into a consequence.
A test that asserts ``execution.ok`` proves the plumbing connected. It does
not prove a file was written, that it was written atomically, that the write
was backed up, that the audit chain that records it verifies afterwards, or
that the spend is accounted for.

So this script runs the real pipeline against a real filesystem and a real
audit log, with one substitution declared openly below.

**The one fake, and why.** The decision tier is
:class:`driver_core.ev.FakeJev`, because running it for real needs
``DRIVER_JEV_API_KEY`` and spends money. Everything else is real: a real
subprocess capture through :mod:`driver_core.osal`, two independent
extractors, a real deterministic tally, the real gate chain, the real
executor, real files written to a real directory, a real fsync'd
hash-chained audit log on disk, and a real budget with real accounting. The
substitution is confined to the one tier that returns a *name* from a closed
vocabulary -- the tier that, by construction, cannot introduce a path, a
string or a URL. Nothing in the action tier is stubbed.

Run it:

    python tools/live_action_run.py

It is expected to *perform* a write and a delete. Both are confined to a
fresh temporary directory it creates and removes.
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from driver_core import osal                                  # noqa: E402
from driver_core.audit import AuditLog                        # noqa: E402
from driver_core.budget import Budget                         # noqa: E402
from driver_core.config import load_settings                  # noqa: E402
from driver_core.driver import Driver                         # noqa: E402
from driver_core.ev import FakeJev, action_answer             # noqa: E402
from driver_core.executor import Consent                      # noqa: E402
from driver_core.extractors import (                          # noqa: E402
    ExtractorPool, StructuredExtractor,
)
from driver_core.perception import StructuredSource            # noqa: E402
from driver_core.states import CLI_SCHEMA                     # noqa: E402

PASS = "  ok  "
FAIL = " FAIL "
_failures = []


def check(label, condition, detail=""):
    """Record a verdict. A live run that cannot fail is not a test."""
    print(f"[{PASS if condition else FAIL}] {label}"
          + (f"\n         {detail}" if detail and not condition else ""))
    if not condition:
        _failures.append(label)
    return condition


def cli_capture(target):
    """A REAL structured capture: runs a real child process via osal.

    This is the CLI tier of ``CLI -> MCP -> DOM -> pixels``, and it is the
    tier that means no pixels and no model are needed for three of the four
    target classes. If it works, the whole structured path is live.
    """
    result = osal.run(["python", "-c",
                       "import json,sys; "
                       "print(json.dumps({'argv': sys.argv[1:]}))", target])
    if not result.ok:
        return None
    return {"exit_code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip()}


def _reader(capture, schema):
    """An extractor over a real capture: the raw state, unmodified.

    Two of these are the two independent observers the consensus tier needs.
    They are deterministic, so they will agree -- which is the honest
    outcome here. A genuine disagreement between two deterministic readers is
    a bug in the reader, not a difference of opinion, and the rest of the
    suite is where that gets exercised.
    """
    return {"exit_code": capture.payload["exit_code"],
            "stdout": capture.payload["stdout"],
            "stderr": capture.payload["stderr"]}


def build_driver(workdir, *, dry_run=False, jev=None):
    settings = load_settings(
        env={},                       # never the real environment
        quorum=2, min_agreement=1.0, confidence_threshold=0.7,
        run_ceiling_usd=1.0, step_ceiling_usd=1.0,
        dry_run=dry_run,
        allow_write=True,             # the capability gate, deliberately on
    )
    audit_path = os.path.join(workdir, "audit.jsonl")
    return Driver(
        settings=settings,
        budget=Budget(1.0, step_ceiling_usd=1.0),
        audit=AuditLog(audit_path),
        pool=ExtractorPool([StructuredExtractor(f"cli-{i}", _reader)
                            for i in range(2)]),
        jev=jev or FakeJev(action_answer("write_file", confidence=0.99)),
        sources=[StructuredSource("cli", cli_capture)],
    ), audit_path


def show(title):
    print(f"\n--- {title} " + "-" * max(0, 62 - len(title)))


def main():
    workdir = tempfile.mkdtemp(prefix="driver-live-")
    target_file = os.path.join(workdir, "report.md")
    scratch = os.path.join(workdir, "scratch.txt")
    print(f"live run in {workdir}")
    try:
        return run_all(workdir, target_file, scratch)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_all(workdir, target_file, scratch):
    # ---- 1. a real structured capture, no pixels, no model -------------
    show("1. capture (CLI tier: a real subprocess, no screen)")
    driver, audit_path = build_driver(workdir)
    capture = driver._capture("my-app", ())
    check("capture came from the CLI tier", capture.source == "cli",
          f"got {capture.source}")
    check("capture produced a fingerprint", bool(capture.fingerprint))
    check("capture summary carries no payload",
          "stdout" not in str(capture.summary()))

    # ---- 2. a real MUTATING action actually executes -------------------
    show("2. execute a declared mutating action (write_file)")
    params = {"path": target_file, "content": "# Report\n\nwritten live.\n"}
    # Consent is bound to the RESOLVED pair, exactly as the CLI resolves it
    # before showing it to an operator.
    consent = Consent(True, "write_file",
                      params={"path": osal.resolve_path(params["path"]),
                              "content": params["content"]},
                      by="live-run")
    result = driver.step("my-app", schema=CLI_SCHEMA, params=params,
                         consent=consent, step_id="live-01")
    check("step succeeded", result.ok, result.detail)
    check("executed the decision's action",
          result.execution.action == "write_file")
    check("not a dry run", result.execution.dry_run is False)
    check("file really exists on disk", os.path.isfile(target_file))
    with open(target_file, encoding="utf-8") as handle:
        written = handle.read()
    check("file content is exactly what was asked for",
          written == params["content"], repr(written))
    check("no temporary file was left behind",
          not [n for n in os.listdir(workdir) if n.endswith(".tmp")])

    # ---- 3. the audit chain verifies on disk ---------------------------
    show("3. hash-chained audit log, verified by re-hashing")
    verdict = driver.audit.verify()
    check("chain verified", verdict.ok, verdict.detail)
    kinds = [r["kind"] for r in driver.audit.read_all()]
    for expected in ("capture", "extraction", "action"):
        check(f"chain contains a {expected!r} record", expected in kinds)
    # NOT asserted: the 'decision' record. It is written by the real
    # JevClient, which this script replaces with a fake -- so its absence is
    # an artefact of the substitution, and asserting it either way would be
    # asserting something about the fake rather than about the system.
    print("         no 'decision' record: expected, the real JevClient "
          "writes that one and it is faked here")
    action_record = [r for r in driver.audit.read_all()
                     if r["kind"] == "action"][-1]
    check("the action record carries the consent it ran under",
          action_record["consent"]["action"] == "write_file")
    check("the action record carries the resolved path, not the raw one",
          action_record["consent"]["params"]["path"] == target_file,
          action_record["consent"]["params"]["path"])

    # ---- 4. the backup policy, live ------------------------------------
    show("4. overwrite backs up first (why MUTATING is the honest class)")
    params2 = {"path": target_file, "content": "# Report\n\nrewritten.\n"}
    consent2 = Consent(True, "write_file",
                       params={"path": target_file,
                               "content": params2["content"]}, by="live-run")
    driver.jev = FakeJev(action_answer("write_file", confidence=0.99))
    result = driver.step("my-app", schema=CLI_SCHEMA, params=params2,
                         consent=consent2, step_id="live-02")
    check("second write succeeded", result.ok, result.detail)
    check("the first version was backed up",
          os.path.isfile(target_file + osal.BACKUP_SUFFIX))
    with open(target_file + osal.BACKUP_SUFFIX, encoding="utf-8") as handle:
        check("the backup holds the ORIGINAL content",
              handle.read() == params["content"])

    # ---- 5. refusals, live ---------------------------------------------
    show("5. the refusals, on the same live driver")
    driver.jev = FakeJev(action_answer("delete_file", confidence=0.99))
    with open(scratch, "w", encoding="utf-8") as handle:
        handle.write("delete me")

    # 5a. a blanket grant authorises nothing.
    wildcard = driver.step("my-app", schema=CLI_SCHEMA,
                           params={"path": scratch},
                           consent=Consent(True, "*"), step_id="live-03")
    check("a wildcard grant is refused", not wildcard.ok, wildcard.detail)
    check("...and says why, in terms of the wildcard",
          "no wildcard grant" in wildcard.detail, wildcard.detail)
    check("...and the file is untouched", os.path.isfile(scratch))

    # 5b. exact consent for the exact pair works.
    exact = Consent(True, "delete_file", {"path": scratch}, by="live-run")
    deleted = driver.step("my-app", schema=CLI_SCHEMA, params={"path": scratch},
                          consent=exact, step_id="live-04")
    check("an exactly-bound consent performs the delete", deleted.ok,
          deleted.detail)
    check("the file really is gone", not os.path.exists(scratch))

    # 5c. ...and is spent. A second delete needs a second person.
    with open(scratch, "w", encoding="utf-8") as handle:
        handle.write("recreated")
    reused = driver.step("my-app", schema=CLI_SCHEMA, params={"path": scratch},
                         consent=exact, step_id="live-05")
    check("the same consent cannot delete a second time", not reused.ok)
    check("...and asks for re-confirmation",
          "re-confirmation" in reused.detail, reused.detail)
    check("...and the file survives the refused delete",
          os.path.exists(scratch))

    # 5d. an action needing a backend that nobody registered.
    driver.jev = FakeJev(action_answer("click", confidence=0.99))
    clicked = driver.step("my-app", schema=CLI_SCHEMA,
                          params={"target": "OK"},
                          consent=Consent(True, "click", {"target": "OK"},
                                          by="live-run"),
                          step_id="live-06")
    check("click refuses: no backend is registered",
          not clicked.ok and "no registered backend" in clicked.detail,
          clicked.detail)
    check("driver-core registers no platform input code at all",
          osal.input_backends() == {})

    # ---- 6. spend, recorded --------------------------------------------
    show("6. spend")
    snapshot = driver.budget.snapshot()
    check("no reservation was leaked",
          driver.budget.open_reservations() == [])
    # The extraction cost is genuinely, correctly $0.000000: two structured
    # extractors over a subprocess capture cost nothing. That is the whole
    # argument for the CLI -> MCP -> DOM -> pixels ordering, and it is worth
    # showing rather than asserting.
    check("structured extraction really was free",
          snapshot["spent_usd"] == 0.0, json.dumps(snapshot["entries"]))
    # NOT demonstrated here, and the gap is named rather than papered over:
    # reserve -> dispatch -> settle lives in the real JevClient, which this
    # script fakes. The budget therefore records no entries in this run.
    # That tier needs DRIVER_JEV_API_KEY and real spend; see
    # tests/test_budget_audit.py for the accounting, and run this script
    # with a key for the live figure.
    print(f"         spent      ${snapshot['spent_usd']:.9f} "
          f"of ${snapshot['ceiling_usd']:.2f}")
    print(f"         remaining  ${snapshot['remaining_usd']:.9f}")
    print(f"         budget entries {json.dumps(snapshot['entries'])}")
    print(f"         step cost  ${result.cost:.9f} "
          f"(extraction $0 + decision, decided by the fake)")

    # ---- 7. the chain still verifies after all of that ------------------
    show("7. final chain verdict")
    final = driver.audit.verify()
    check("chain still verifies after every step", final.ok, final.detail)
    print(f"         {final.records} records, all chained")
    print(f"         {audit_path}")

    print("\n" + "=" * 68)
    if _failures:
        print(f"LIVE RUN FAILED: {len(_failures)} check(s) did not hold")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print("LIVE RUN PASSED: a declared action really executed, and every")
    print("refusal above is a refusal the design depends on.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
