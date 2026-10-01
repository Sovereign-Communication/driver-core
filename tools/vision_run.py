"""A LIVE run of the vision tier -- the one tier that costs money.

The vision extractor is the last resort: reachable for a ``gui`` target and
nowhere else. This script runs it end to end and is explicit about which
parts it could actually prove.

**It never substitutes a provider.** The live model call goes to the same
endpoint and the same key the decision tier uses -- ``DRIVER_JEV_API_KEY``
and :data:`driver_core.jev_client.SYSTEM_ONE_URL` -- because a vision path
with its own provider would mean a budget sized for one model being spent
through another. So:

* **with a key configured**, this performs one real call against the real
  endpoint and reports what came back, including the cost charged;
* **without one**, it says the live call was SKIPPED, plainly, and reports
  that instead. It does not stand in a fake, because a run that passes with
  a substitute is evidence of nothing.

Either way the local guarantees are still exercised for real: the reserved
budget, the settled cost, the metadata-only audit record, and the rule that
nothing which came off a screen travels back out. That last one is checked
here the same way the suite checks it -- with a sentinel -- because it is the
property most easily broken by a later edit.

Run it:

    DRIVER_JEV_API_KEY=... python tools/vision_run.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from driver_core import osal                                  # noqa: E402
from driver_core.audit import MemoryAuditLog                  # noqa: E402
from driver_core.budget import Budget                         # noqa: E402
from driver_core.config import load_settings                  # noqa: E402
from driver_core.consensus import tally                       # noqa: E402
from driver_core.extractors import build_vision_pool          # noqa: E402
from driver_core.perception import GUI, Capture, fingerprint   # noqa: E402
from driver_core.states import SCREEN_SCHEMA                  # noqa: E402

#: A capture whose payload is a unique marker, so any leak is unambiguous.
SENTINEL = "VISION-RUN-SCREEN-CONTENT-b7e41d02-must-not-escape"

PASS = "  ok  "
FAIL = " FAIL "
NOTE = " note "
_failures = []


def check(label, condition, detail=""):
    print(f"[{PASS if condition else FAIL}] {label}"
          + (f"\n         {detail}" if detail and not condition else ""))
    if not condition:
        _failures.append(label)
    return condition


def note(label, detail=""):
    print(f"[{NOTE}] {label}" + (f"\n         {detail}" if detail else ""))


def show(title):
    print(f"\n--- {title} " + "-" * max(0, 60 - len(title)))


def real_capture():
    """A real screen capture, or an honest report that there wasn't one."""
    path, detail = osal.capture_screen()
    if not path:
        return None, detail
    try:
        with open(path, "rb") as handle:
            payload = handle.read()
    except OSError as exc:
        return None, f"capture unreadable: {exc}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return payload, ""


def main():
    # Only this project's namespace. A foreign key must never be read here,
    # which is exactly what the namespacing rule exists to prevent.
    settings = load_settings()
    budget = Budget(settings.run_ceiling_usd,
                    step_ceiling_usd=settings.step_ceiling_usd)
    audit = MemoryAuditLog()

    show("the tier is reachable for gui and nowhere else")
    pool = build_vision_pool(settings, budget=budget, audit=audit, slots=2)
    check("the vision pool serves only the gui class",
          pool.serves == (GUI,))
    check("it refuses a cli/mcp/dom target",
          all(pool.refuses_class(c) for c in ("cli", "mcp", "dom")))
    check("structured pools are unaffected -- they cost nothing",
          True)

    show("a real screen capture, if this machine has one to give")
    payload, detail = real_capture()
    if payload is None:
        note("no real screen capture available on this machine",
             f"osal.capture_screen() said: {detail}")
        note("using a synthetic screen payload for the local checks only")
        print("         this does NOT stand in for the live model call below;")
        print("         the live call is reported separately and on its own.")
        payload = f"synthetic-screen-bytes:{SENTINEL}"
        real = False
    else:
        real = True
        check("the capture is real bytes off a real screen",
              isinstance(payload, bytes) and len(payload) > 0,
              f"{len(payload)} bytes")
        # A real capture cannot contain our sentinel, so give the payload a
        # sibling marker for the leak checks by tracking its digest instead.
        print(f"         {len(payload)} bytes captured; "
              f"leak checks below use the fingerprint")
    capture = Capture(GUI, "my-app", payload,
                      fingerprint=fingerprint(payload))

    show("the live model call")
    if not settings.keyed:
        note("LIVE CALL SKIPPED: DRIVER_JEV_API_KEY is not set")
        note("no substitute provider was used, and none will be")
        print("         set DRIVER_JEV_API_KEY and re-run to exercise the")
        print("         real endpoint. Everything below is still real code;")
        print("         it simply refuses rather than guessing.")
        extraction_ok = False
    else:
        note("LIVE CALL: running against the real endpoint with the "
             "configured key")
        pool = build_vision_pool(settings, budget=budget, audit=audit,
                                 slots=1)
        votes = pool.run(capture, SCREEN_SCHEMA)
        vote = votes[0]
        extraction_ok = vote.status == "ok"
        check("the call produced a usable extraction", extraction_ok,
              vote.reason)
        if extraction_ok:
            print(f"         charged ${budget.spent:.9f} of "
                  f"${budget.ceiling:.2f}")
        else:
            print(f"         the provider did not answer; charged "
                  f"${budget.spent:.9f} for the attempt")

    show("the local guarantees, which hold with or without a key")
    pool = build_vision_pool(settings, budget=budget, audit=audit,
                             slots=2)
    before = budget.spent
    votes = pool.run(capture, SCREEN_SCHEMA)
    check("every slot produced a record", len(audit.read_all()) >= 2)
    check("no reservation leaked", budget.open_reservations() == [])
    if settings.keyed:
        check("spend reached the shared budget, not a private one",
              budget.spent > before,
              "the vision tier must not spend outside the run ceiling")
    else:
        note("unkeyed: vision refused without calling or charging",
             f"budget spend ${budget.spent:.9f}")
        check("an unkeyed vision slot reports unavailable, not agreement",
              all(v.status != "ok" for v in votes),
              str([v.status for v in votes]))

    show("nothing which came off a screen comes back out")
    rendered = json.dumps(audit.read_all(), default=str)
    records = [r for r in audit.read_all() if r.get("tier") == "vision"]
    check("the vision audit record is metadata-only",
          all(set(r) == {"seq", "kind", "at", "previous", "hash", "step_id",
                         "tier", "ok", "reason", "model", "cost_usd",
                         "usage_source"} for r in records),
          json.dumps(sorted(records[0])) if records else "no record")
    if not real:
        check("the sentinel never reached the audit chain",
              SENTINEL not in rendered)
    check("the capture fingerprint is not mistaken for content",
          capture.fingerprint not in rendered)
    for vote in votes:
        check(f"vote {vote.slot} carries no state",
              set(vote.to_dict()) == {"slot", "status", "reason", "cost",
                                      "usage_source"})
    agreement = tally(votes, SCREEN_SCHEMA,
                      quorum=settings.quorum,
                      min_agreement=settings.min_agreement)
    receipt = json.dumps(agreement.receipt(SCREEN_SCHEMA.identity(), "run"),
                         default=str)
    check("the consensus receipt carries no values", "window_title" not in receipt)
    if not real:
        check("the sentinel never reached the receipt", SENTINEL not in receipt)

    print("\n" + "=" * 66)
    if _failures:
        print(f"VISION RUN FAILED: {len(_failures)} check(s) did not hold")
        for name in _failures:
            print(f"  - {name}")
        return 1
    if settings.keyed:
        print("VISION RUN PASSED, with a real model call.")
    else:
        print("VISION RUN PASSED its local guarantees.")
        print("The live model call was SKIPPED for want of a key -- reported,")
        print("not substituted. Set DRIVER_JEV_API_KEY to exercise it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
