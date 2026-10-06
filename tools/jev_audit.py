"""Run the comprehensive Jev audit: every symbol, every dimension, in full.

`driver_core.jev_audit` holds the audit -- the declared dimensions, the
mechanically computed dossier each symbol is judged against, the context
supplier, the aggregation. This is the operator's handle on it: it decides the
budget, streams each symbol to a checkpoint as it is paid for, and writes the
Markdown report.

Run it:

    DRIVER_JEV_API_KEY=... python tools/jev_audit.py --estimate
    DRIVER_JEV_API_KEY=... python tools/jev_audit.py --budget-usd 0.25

**It is resumable, and that is not a nicety.** A full pass is several hundred
sequential calls against a rate-limited endpoint, which is exactly the kind of
run that gets killed. Every record is appended to the checkpoint the moment it
is produced, and a killed run restarts from what it already bought -- so a
partial audit costs a partial budget rather than all of it.

**It refuses to pretend.** Without a key it exits non-zero and says why,
instead of producing an empty report that reads like a clean audit. With a
budget it stops when the budget is spent and writes the words "budget
exhausted" into the report, instead of trailing off.

Three arguments have no sane default and are therefore explicit: the gate, the
need floor, and the budget. The gate here is *not* the decision gate. A
decision the system acts on has to clear 0.99; an audit score is information,
and information that is 0.72 confident is worth exactly 0.72 and is worth
recording as such. The report prints every mean confidence so a reader can
apply their own bar.
"""
import argparse
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from driver_core import jev_audit                                  # noqa: E402
from driver_core.config import load_settings                       # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(REPO_ROOT),
                        help="the tree to audit (default: the repository, so "
                             "the tests bucket can find the tests); use "
                             "--prefix to narrow what is judged")
    parser.add_argument("--tests-root", default=str(REPO_ROOT / "tests"),
                        help="where the tests live, for the guardedness "
                             "dossier (default: tests)")
    parser.add_argument("--checkpoint",
                        default=str(REPO_ROOT / "build" / "jev_audit"
                                    / "symbols.jsonl"))
    parser.add_argument("--report",
                        default=str(REPO_ROOT / "build" / "jev_audit"
                                    / "report.md"))
    parser.add_argument("--summary",
                        default=str(REPO_ROOT / "build" / "jev_audit"
                                    / "summary.json"))
    parser.add_argument("--gate", type=float, default=0.9,
                        help="confidence a verdict must reach to settle a "
                             "round (default: 0.9, deliberately not the "
                             "decision gate)")
    parser.add_argument("--need-floor", type=float, default=3.0,
                        help="outstanding-need score at or below which the "
                             "model is treated as needing no more context")
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--budget-usd", type=float, default=0.25)
    parser.add_argument("--limit", type=int, default=None,
                        help="audit only the first N symbols")
    parser.add_argument("--module", action="append", default=None,
                        help="restrict to one dotted module path; repeatable")
    parser.add_argument("--prefix", default=None,
                        help="restrict to modules under a package, e.g. "
                             "driver_core -- the whole tree stays readable, "
                             "so the tests bucket still finds its tests")
    parser.add_argument("--resume", action="store_true",
                        help="skip symbols already in the checkpoint")
    parser.add_argument("--report-only", action="store_true",
                        help="re-aggregate an existing checkpoint and write "
                             "the report; makes no calls and needs no key")
    parser.add_argument("--estimate", action="store_true",
                        help="print what a pass would cover, and stop")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def chosen_symbols(root, tests_root, modules, limit, prefix=None):
    """Every symbol a pass would cover, in the order the pass covers it.

    ``prefix`` narrows which symbols are *audited* without narrowing what the
    tree *reads*: the tests bucket still has to find the tests, and the
    alternatives bucket still has to see the siblings, so the root is never
    reduced to do this.
    """
    wanted = [symbol for symbol in jev_audit.symbols(root,
                                                     tests_root=tests_root)
              if (not modules or symbol.module in modules)
              and (prefix is None or symbol.module == prefix
                   or symbol.module.startswith(prefix + "."))]
    return wanted if not limit else wanted[:limit]


def main(argv=None):
    args = parse_args(argv)
    chosen = chosen_symbols(args.root, args.tests_root, args.module,
                            args.limit, args.prefix)
    if args.estimate:
        bucket = {}
        for symbol in chosen:
            bucket[symbol.module] = bucket.get(symbol.module, 0) + 1
        print(f"{len(chosen)} symbols would be audited under {args.root}:")
        for module, count in sorted(bucket.items()):
            print(f"  {module:24} {count}")
        print(f"\n{len(jev_audit.DIMENSIONS) + 4} questions per symbol, "
              f"gate {args.gate}, up to {args.max_rounds} rounds each.")
        print(f"Budget ceiling: ${args.budget_usd:.4f} -- the run stops there "
              f"and says so.")
        return 0

    # Reporting reads a checkpoint that was already paid for, so it neither
    # needs a key nor may refuse for the want of one. Auditing does need one,
    # and refusing loudly is the difference between "not run" and "ran clean".
    if not args.report_only:
        settings = load_settings(env=os.environ)
        if not settings.keyed:
            print("refusing to run: no Jev key. Set DRIVER_JEV_API_KEY in the "
                  "environment (never in a file in this tree) and try again.",
                  file=sys.stderr)
            return 2
    else:
        settings = load_settings(env=os.environ)

    checkpoint_path = pathlib.Path(args.checkpoint)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    already = jev_audit.load_checkpoint(checkpoint_path)
    done = ({record.get("key") for record in already}
            if (args.resume or args.report_only) else set())
    if done and not args.quiet:
        print(f"resuming: {len(done)} symbols already in the checkpoint")

    handle = checkpoint_path.open("a", encoding="utf-8", newline="\n")

    def checkpoint(record):
        handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    def progress(index, total, symbol, spent):
        if args.quiet:
            return
        print(f"[{index:>4}/{total}] ${spent:.5f}  {symbol.key}",
              file=sys.stderr)

    started = time.time()
    final = None
    try:
        if not args.report_only:
            for record in jev_audit.audit(
                    args.root, settings=settings, tests_root=args.tests_root,
                    gate=args.gate, need_floor=args.need_floor,
                    max_rounds=args.max_rounds, budget_usd=args.budget_usd,
                    limit=args.limit, modules=args.module, checkpoint=checkpoint,
                    progress=progress, done=done):
                if "stopped" in record:
                    final = record
    finally:
        handle.close()

    records = jev_audit.load_checkpoint(checkpoint_path)
    summary = jev_audit.aggregate(records)
    summary["stopped"] = bool(final and final.get("stopped"))
    summary["stop_reason"] = (", ".join(f"{k}={v}" for k, v in
                                        sorted((final or {}).items())))
    summary["gate"] = args.gate
    summary["need_floor"] = args.need_floor
    summary["max_rounds"] = args.max_rounds
    summary["root"] = args.root
    summary["model"] = records[-1].get("grounding", {}).get("model") \
        if records else None

    report = jev_audit.render_report(summary, root=args.root,
                                     model=summary["model"], gate=args.gate)
    pathlib.Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.report).write_text(report + "\n", encoding="utf-8")
    pathlib.Path(args.summary).write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8")

    print(f"\n{summary['symbols_audited']} symbols audited in "
          f"{time.time() - started:.1f}s for ${summary['cost_usd']:.6f} "
          f"({summary['input_tokens']:,} input tokens).")
    if summary["stopped"]:
        print(f"STOPPED EARLY: {summary['stop_reason']}", file=sys.stderr)
    print(f"report:  {args.report}")
    print(f"summary: {args.summary}")
    print(f"raw:     {args.checkpoint}")
    return 1 if summary["stopped"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
