"""A LIVE run of declared configuration becoming live perception sources.

The perception tiers were built, ordered, and provable -- and unreachable. A
default ``Driver()`` carried no sources, so ``driver-core step`` answered
``Tried: none`` and every ``POST /step`` ended in ``no_capture``. The chain
worked only for a caller writing Python by hand, which makes it a library
feature rather than a product one.

This script drives the *shipped surfaces* -- the CLI's ``main()`` and the
REST service over a real socket -- with nothing but ``DRIVER_*`` variables in
the environment, and reports what each one really did. There is no fixture and
no hand-built source anywhere in it: every capture below comes from the
configuration wiring calling a real child process, a real loopback HTTP
server, or nothing at all.

What it proves, in order:

* **nothing configured observes nothing** -- and says so honestly rather than
  falling back to pixels;
* **one setting enables exactly one tier**, and the vision tier is never
  enabled by the presence of another setting;
* **a declared command really runs**, end to end through capture, extraction,
  tally, gates and a read-only execution;
* **a declared command is tokenised, never shelled** -- ``>`` in a
  ``DRIVER_CLI_COMMAND`` is an argv element, not a redirection, and the proof
  is a file that was *not* created;
* **an absent or unknown ``schema`` is refused over the wire** -- 400, not a
  silent default -- while a recognised one is honoured;
* **pixels are not reachable by default**: ``schema: "gui"`` with no
  ``DRIVER_SCREEN`` is a refusal that says no screen tier is configured, not
  a capture.

The only substitution is the decision tier, which is a stand-in when no API
key is present -- the far side of a network call, exactly as in
``tools/vision_run.py``. The capture tiers under test are all real.

Run it:

    python tools/configure_run.py

Nothing here needs an API key, a model, a network, or a display.
"""
import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from driver_core import cli                                    # noqa: E402
from driver_core.audit import AuditLog, MemoryAuditLog           # noqa: E402
from driver_core.config import load_settings                   # noqa: E402
from driver_core.driver import Driver                          # noqa: E402
from driver_core.errors import PerceptionUnavailable           # noqa: E402
from driver_core.ev import FakeJev, action_answer              # noqa: E402
from driver_core.perception import GUI, Target                 # noqa: E402
from driver_core.server import STOP_REASONS, Service, serve    # noqa: E402
from driver_core.states import CLI_SCHEMA, DOM_SCHEMA          # noqa: E402
from driver_core.wiring import configured_pools, configured_sources  # noqa: E402,E501

PASS = "  ok  "
FAIL = " FAIL "
_failures = []

#: A command that prints its own argument, so a capture can be shown to carry
#: the *real* child's output rather than a fixture's.
PRINTER = "python -c \"import sys; print('from-cli:' + sys.argv[1])\""

#: A sentinel string planted in the child's output. The audit log is searched
#: for it afterwards: nothing off a capture belongs in the chain.
SENTINEL = "from-cli"

PAGE = """<!doctype html>
<html><head><title>Payroll Report</title></head>
<body><h1>Payroll Report</h1><p>Deductions are final.</p></body></html>
"""


def check(label, condition, detail=""):
    print(f"[{PASS if condition else FAIL}] {label}"
          + (f"\n         {detail}" if detail and not condition else ""))
    if not condition:
        _failures.append(label)
    return condition


def show(title):
    print(f"\n--- {title} " + "-" * max(0, 60 - len(title)))


class _Page(BaseHTTPRequestHandler):
    """A real document on loopback, so the DOM tier fetches over a socket."""

    def do_GET(self):
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def serve_page():
    httpd = HTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}/report"


def free_port():
    """An unused loopback port, chosen by the OS and then released.

    ``serve()`` falls back to the configured port when handed ``0``, so the
    run asks for a concrete one rather than assuming an ephemeral bind.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def post(port, token, route, body):
    """A real HTTP request. Returns ``(status, parsed_body)``.

    The token check is exercised here too: an endpoint that can act on a
    machine must not answer an unauthenticated caller.
    """
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/{route}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        # An HTTPError is a file object, and a 4xx is the *expected* answer
        # for most of the checks below. It is read and closed here; leaving it
        # to the garbage collector is a ResourceWarning under the flag this
        # project runs its suite with.
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        finally:
            exc.close()


def get(port, token, route):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/{route}",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read() or b"{}")


def env_for(workdir, **extra):
    """A ``DRIVER_*``-only environment. Nothing else is set or inherited."""
    env = {"DRIVER_AUDIT_PATH": os.path.join(workdir, "audit.jsonl"),
           "DRIVER_DRY_RUN": "1"}
    env.update(extra)
    return env


def main():
    workdir = tempfile.mkdtemp(prefix="driver-configure-")
    print(f"configure run in {workdir}")
    try:
        return run_all(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_all(workdir):
    # ---- 1. the default observes nothing, and says so -------------------
    show("nothing configured: the driver observes nothing")
    settings = load_settings(env={}, audit_path=os.path.join(workdir, "a.jsonl"))
    check("no setting declares a source", settings.declared_sources() == [])
    driver = Driver(settings=settings, audit=MemoryAuditLog(),
                    jev=FakeJev(action_answer("observe", confidence=0.95)))
    check("...and the wiring builds none", driver.sources == [])
    check("...and there is no screen source", driver.screen is None)
    result = driver.step(Target("my-app", GUI), schema=CLI_SCHEMA)
    check("a step refuses rather than falling back to pixels",
          not result.ok and result.reason == "no_capture",
          f"{result.reason}: {result.detail}")
    check("the refusal names the empty configuration",
          "Configured sources: ['none']" in result.detail, result.detail)
    print(f"         {result.detail}")

    # ---- 2. one setting, one tier ---------------------------------------
    show("DRIVER_CLI_COMMAND enables the CLI tier and nothing else")
    env = env_for(workdir, DRIVER_CLI_COMMAND=PRINTER)
    settings = load_settings(env=env)
    check("the declared source is reported by name",
          settings.declared_sources() == ["cli"],
          str(settings.declared_sources()))
    check("and it becomes a live source object",
          [s.name for s in configured_sources(settings)] == ["cli"])
    pools = configured_pools(settings, budget=None, audit=None)
    check("structured pools are free and keyed by class",
          sorted(pools) == ["cli", "dom", "mcp"], str(sorted(pools)))
    check("the vision pool is absent -- no DRIVER_SCREEN",
          GUI not in pools)
    check("and /health reports the capability, not a guess",
          Service(driver=Driver(settings=settings, audit=MemoryAuditLog(),
                                jev=FakeJev(action_answer("observe"))),
                  token="t").health()["sources"] == ["cli"])

    # ---- 2b. the vision tier, declared once and only once ---------------
    show("DRIVER_SCREEN declares the vision tier, exactly once")
    vision = Driver(
        settings=load_settings(env=env_for(workdir, DRIVER_SCREEN="1")),
        audit=MemoryAuditLog(), jev=FakeJev(action_answer("observe")))
    check("the screen source is live",
          [s.name for s in vision.observation_sources()] == ["screen"],
          str([s.name for s in vision.observation_sources()]))
    check("...and is not consulted twice",
          vision.screen is vision.sources[0])
    check("...and /health reports it once",
          Service(driver=vision, token="t").health()["sources"] == ["screen"],
          str(Service(driver=vision, token="t").health()["sources"]))
    check("...and the vision pool exists for the gui class alone",
          GUI in vision.pools
          and all(vision.pools[GUI].refuses_class(c) for c in ("cli", "mcp",
                                                                "dom")))
    result = vision.step(Target("my-app", GUI))
    check("a gui step with no declared screen output refuses honestly",
          not result.ok and result.reason == "no_capture",
          f"{result.reason}: {result.detail}")
    check("...naming the tier it tried rather than claiming a capture",
          "screen:" in result.detail, result.detail)
    print(f"         {result.detail}")

    # ---- 3. a real subprocess, end to end -------------------------------
    show("a real step: capture -> extract -> tally -> gate -> execute")
    driver = Driver(settings=load_settings(env=env), audit=MemoryAuditLog(),
                    jev=FakeJev(action_answer("observe", confidence=0.95)))
    result = driver.step(Target("my-app", "cli"), schema=CLI_SCHEMA)
    check("the step executed", result.ok,
          f"{result.reason}: {result.detail}")
    check("the capture came from the configured CLI tier",
          result.capture.source == "cli")
    check("it is the child's own output, carrying the target",
          result.capture.payload["stdout"].strip() == f"{SENTINEL}:my-app",
          repr(result.capture.payload))
    check("the two free extractors were unanimous",
          result.agreement.is_usable and result.agreement.answering == 2,
          str(result.agreement))
    check("execution was the read-only action",
          result.execution.action == "observe")
    print(f"         {result.capture.summary()}")

    # ---- 4. tokenised, never shelled ------------------------------------
    show("a declared command is tokenised, never handed to a shell")
    trap = os.path.join(workdir, "driver-should-not-exist.txt")
    hostile = PRINTER + " > " + trap
    driver = Driver(
        settings=load_settings(env=env_for(workdir, DRIVER_CLI_COMMAND=hostile)),
        audit=MemoryAuditLog(), jev=FakeJev(action_answer("observe")))
    result = driver.step(Target("my-app", "cli"), schema=CLI_SCHEMA)
    check("the command still ran", result.ok, result.detail)
    check("'>' was an argument, so no redirection happened",
          not os.path.exists(trap))
    print(f"         declared: {hostile}")

    # ---- 5. DOM, with the schema a document can actually satisfy --------
    show("DRIVER_DOM_URL reaches a real document over a real socket")
    httpd, url = serve_page()
    try:
        dom = Driver(
            settings=load_settings(env=env_for(workdir, DRIVER_DOM_URL=url)),
            audit=MemoryAuditLog(), jev=FakeJev(action_answer("observe")))
        check("the dom tier is live", [s.name for s in dom.sources] == ["dom"])
        result = dom.step(Target(url, "dom"), schema=DOM_SCHEMA)
        check("the document was fetched and validated against DOM_SCHEMA",
              result.ok, f"{result.reason}: {result.detail}")
        if result.capture is not None:
            check("its title came out of the real bytes",
                  result.capture.payload.get("window_title") == "Payroll Report",
                  str(result.capture.payload))
            print(f"         {result.capture.summary()}")
    finally:
        httpd.shutdown()
        httpd.server_close()

    # ---- 6. the real CLI, environment only ------------------------------
    show("the shipped CLI, driven by DRIVER_* only")
    previous = dict(os.environ)
    try:
        # Only DRIVER_* is touched. The rest of the environment is left alone:
        # driver-core reads no other namespace, and clearing the whole
        # environment on Windows takes PATH and SystemRoot with it -- which
        # would make this a test of subprocess creation, not of the wiring.
        for name in list(os.environ):
            if name.startswith("DRIVER_"):
                del os.environ[name]
        os.environ.update(env)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(["health"])
        text = out.getvalue()
        check("`driver-core health` exits 0", code == 0, text)
        check("...and reports the source it can observe",
              "sources    : cli" in text, text)

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(["--json", "step", "--schema", "cli", "my-app"])
        payload = json.loads(out.getvalue())
        check("`driver-core --json step --schema cli` runs the real chain",
              payload["capture"]["source"] == "cli", out.getvalue())
        check("...and stops honestly at the decision tier (no key here)",
              payload["ok"] is False
              and payload["reason"] == "decision_not_usable",
              json.dumps(payload.get("reason")))
        check("...and the reason is from the closed set",
              payload["reason"] in STOP_REASONS)
        print(f"         {payload['reason']}: {payload['detail']}")

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(["--json", "step", "--schema", "gui", "my-app"])
        payload = json.loads(out.getvalue())
        check("`step --schema gui` with no DRIVER_SCREEN refuses",
              payload["ok"] is False and payload["reason"] == "no_capture",
              json.dumps(payload.get("reason")))
        check("...and says no screen tier is configured",
              "no configured source serves" in payload["detail"],
              payload["detail"])
        check("...and the refusal is non-zero exit", code == 1, str(code))
    finally:
        for name in list(os.environ):
            if name.startswith("DRIVER_"):
                del os.environ[name]
        os.environ.update(previous)

    # ---- 7. the REST surface over a real socket -------------------------
    show("POST /step over a real socket: fail closed on an absent schema")
    # A real hash-chained audit log, not a memory double: the checks at the
    # end of this run read the file back off disk.
    driver = Driver(settings=settings, audit=AuditLog(settings.audit_path))
    # ``block=False`` binds and returns without accepting, so the accept loop
    # is started here. Everything below is a real request over a real socket
    # to the shipped handler.
    httpd, service = serve("127.0.0.1", free_port(), service=Service(driver),
                           block=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    print(f"         listening on 127.0.0.1:{port}")
    try:
        status, body = post(port, service.token, "step", {"target": "t"})
        check("an absent schema is a 400, not a default",
              status == 400, f"{status} {body}")
        check("...naming the schemas the caller may declare",
              "schema is required" in body.get("error", "")
              and all(name in body.get("error", "")
                      for name in ("cli", "dom", "gui", "mcp", "screen")),
              body.get("error"))

        status, body = post(port, service.token, "step",
                            {"target": "t", "schema": "banana"})
        check("an unknown schema is a 400 too",
              status == 400 and "unknown schema" in body.get("error", ""),
              f"{status} {body}")

        status, body = post(port, service.token, "step",
                            {"target": "t", "schema": "   "})
        check("a blank schema is a 400 as well",
              status == 400 and "schema is required" in body.get("error", ""),
              f"{status} {body}")

        status, body = post(port, service.token, "step",
                            {"target": "t", "schema": "gui"})
        check("a declared gui target with no DRIVER_SCREEN is a 200 refusal",
              status == 200 and body.get("reason") == "no_capture",
              f"{status} {body}")
        check("...not a screen capture: pixels are not reachable by default",
              body.get("capture") is None, str(body.get("capture")))
        check("...and the refusal names the missing tier",
              "no configured source serves" in body.get("detail", ""),
              body.get("detail"))
        print(f"         {body['detail']}")

        status, body = post(port, service.token, "step",
                            {"target": "my-app", "schema": "cli",
                             "step_id": "host-0001"})
        check("a declared cli target really captures",
              status == 200 and (body.get("capture") or {}).get("source") == "cli",
              f"{status} {body}")
        check("...and the host's own step_id is the one recorded",
              body.get("step_id") == "host-0001", str(body.get("step_id")))
        check("...stopping at the decision tier for want of a key",
              body.get("reason") == "decision_not_usable",
              str(body.get("reason")))

        status, body = post(port, service.token, "step",
                            {"target": "t", "schema": "mcp"})
        check("an unconfigured mcp target refuses rather than guessing",
              status == 200 and body.get("reason") == "no_capture",
              f"{status} {body}")

        status, body = get(port, service.token, "health")
        check("GET /health reports the sources it can observe",
              status == 200 and body.get("sources") == ["cli"],
              str(body.get("sources")))
        check("...and never renders a key",
              "jev_api_key" not in json.dumps(body))

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/health", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            try:
                status = exc.code
            finally:
                exc.close()
        check("an unauthenticated caller is refused",
              status == 401, str(status))
    finally:
        # shutdown() stops the loop; server_close() releases the socket. Under
        # -W error::ResourceWarning the difference is an unraisable warning.
        httpd.shutdown()
        httpd.server_close()

    # ---- 8. the chain, and what it must not contain ---------------------
    show("the audit chain, and what may never enter it")
    check("the chain verifies", driver.audit.verify().ok)
    with open(driver.audit.path, encoding="utf-8") as handle:
        log = handle.read()
    check("the capture's own content never reaches the audit chain",
          SENTINEL not in log,
          "screen content was found in the audit log")
    check("the chain records the step the host named",
          "host-0001" in log)

    show("a refusal that says what to do about it")
    try:
        Driver(settings=load_settings(env=env), audit=MemoryAuditLog()
               )._capture(Target("t", "dom"), ())
        check("a dom target with only a cli source configured refuses", False)
    except PerceptionUnavailable as exc:
        message = str(exc)
        check("a dom target with only a cli source configured refuses", True)
        check("...naming the class that was asked for",
              "declared 'dom'" in message, message)
        check("...naming what is actually configured",
              "Configured sources: ['cli']" in message, message)
        check("...and blaming the wrong class, not a failed probe",
              "nothing was tried" in message, message)
        print(f"         {message}")

    print("\n" + "=" * 66)
    if _failures:
        print(f"CONFIGURE RUN FAILED: {len(_failures)} check(s) did not hold")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print("CONFIGURE RUN PASSED: declared settings became live sources, a real")
    print("child process was observed through the real CLI and the real REST")
    print("surface, and an absent schema was refused rather than defaulted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
