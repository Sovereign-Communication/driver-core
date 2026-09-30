"""Command-line entry point.

Small on purpose. The interesting behaviour is in the library, and a CLI
that grew its own opinions would become a second implementation of the same
policy -- which is the single thing this project is structured to prevent.

Every subcommand that can spend money or touch a machine reports what it did
and refuses honestly when it could not. ``--json`` is available on all of
them so a script can branch on ``ok`` and ``reason`` rather than parsing
prose, and the exit code reflects whether the step actually executed.
"""
import argparse
import json
import sys

from .config import load_settings
from .driver import Driver
from .executor import Consent
from .server import Service, serve
from .states import SCREEN_SCHEMA

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ERROR = 2


def _print(payload, as_json):
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def cmd_health(args, driver):
    payload = Service(driver).health()
    if not args.json:
        settings = driver.settings
        print(f"driver-core: ok (keyed={settings.keyed})")
        print(f"  vocabulary : {driver.vocabulary.identity()} "
              f"({len(driver.vocabulary)} actions)")
        print(f"  budget     : ${driver.budget.remaining:.6f} remaining of "
              f"${driver.budget.ceiling:.6f}")
        print(f"  audit      : {driver.audit.count} records")
    _print(payload, args.json)
    return EXIT_OK


def cmd_vocabulary(args, driver):
    payload = Service(driver).vocabulary()
    if not args.json:
        for action in driver.vocabulary.to_dict()["actions"]:
            print(f"  {action['name']:<26} {action['class']:<12} "
                  f"target={action['target']}")
    _print(payload, args.json)
    return EXIT_OK


def cmd_schema(args, driver):
    payload = Service(driver).schemas()
    if not args.json:
        for schema in payload["schemas"]:
            print(f"{schema['id']}@{schema['version']}")
            for field in schema["fields"]:
                flag = "" if field["presence"] == "required" else " (optional)"
                print(f"  {field['name']:<26} {field['type']}{flag}")
    _print(payload, args.json)
    return EXIT_OK


def cmd_step(args, driver):
    consent = None
    if args.grant_write:
        consent = Consent(True, "*", by="cli")
    elif args.grant:
        consent = Consent(True, args.grant, params={"path": args.grant_path},
                          by="cli")
    result = driver.step(args.target, schema=SCREEN_SCHEMA, consent=consent,
                         prefer=tuple(args.prefer or ()),
                         require_stable=not args.allow_unstable)
    payload = result.to_dict()
    if not args.json:
        if payload["ok"]:
            execution = payload.get("execution") or {}
            print(f"[executed] {execution.get('action')} "
                  f"(confidence {payload['decision']['confidence']})")
        else:
            print(f"[refused] {payload['reason']}: {payload['detail']}")
    _print(payload, args.json)
    return EXIT_OK if payload["ok"] else EXIT_REFUSED


def cmd_verify(args, driver):
    payload = Service(driver).verify()
    if not args.json:
        audit = payload["audit"]
        print(f"audit: {'VERIFIED' if audit['ok'] else 'BROKEN'} "
              f"({audit['records']} records) -- {audit['detail']}")
        budget = payload["budget"]
        print(f"spend: ${budget['spent_usd']:.6f} of "
              f"${budget['ceiling_usd']:.6f} "
              f"(${budget['remaining_usd']:.6f} remaining)")
    _print(payload, args.json)
    return EXIT_OK if payload["audit"]["ok"] else EXIT_REFUSED


def cmd_serve(args, driver):
    _, service = serve(args.host, args.port, service=Service(driver),
                       block=not args.print_token)
    if args.print_token:
        print(f"listening on http://{args.host}:{args.port}")
        print(f"token: {service.token}")
        return EXIT_OK
    return EXIT_OK


COMMANDS = {
    "health": cmd_health,
    "vocabulary": cmd_vocabulary,
    "schema": cmd_schema,
    "step": cmd_step,
    "verify": cmd_verify,
    "serve": cmd_serve,
}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="driver-core",
        description="Verified extraction -> Jev decision -> deterministic action")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON only")
    parser.add_argument("--dry-run", action="store_true",
                        help="perform no side effects; report what would happen")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="show settings, budget and audit state")
    sub.add_parser("vocabulary", help="list the declared action vocabulary")
    sub.add_parser("schema", help="list the declared extraction schemas")
    sub.add_parser("verify", help="verify the audit chain and report spend")

    step = sub.add_parser("step", help="run one full pipeline step")
    step.add_argument("target", help="what to observe")
    step.add_argument("--prefer", action="append",
                      help="prefer a structured source (cli, mcp, dom)")
    step.add_argument("--grant", help="consent for one named action")
    step.add_argument("--grant-path", default="",
                      help="the path an --grant was given for")
    step.add_argument("--grant-write", action="store_true",
                      help="consent for all mutating actions")
    step.add_argument("--allow-unstable", action="store_true",
                      help="do not require the stability guard")

    srv = sub.add_parser("serve", help="run the loopback REST service")
    srv.add_argument("--host", default=None)
    srv.add_argument("--port", type=int, default=None)
    srv.add_argument("--print-token", action="store_true",
                     help="bind, print the token, and exit")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    # Only override when the flag is actually set: passing False would
    # clobber DRIVER_DRY_RUN from the environment, which is a quieter bug
    # than it looks -- the flag would appear to do nothing.
    settings = load_settings(**({"dry_run": True} if args.dry_run else {}))
    driver = Driver(settings=settings)
    try:
        return COMMANDS[args.command](args, driver)
    except Exception as exc:
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
