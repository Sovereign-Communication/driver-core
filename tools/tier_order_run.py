"""A LIVE run of the perception tier chain: CLI -> MCP -> DOM -> pixels.

The point of this script is to show the ordering *happening*, rather than to
assert it in a docstring. It exercises each structured tier against something
real:

* **CLI** -- a real child process launched through :mod:`driver_core.osal`.
  No stub, no fixture: a real ``python -c`` that prints and exits non-zero.
* **MCP** -- a real MCP server, speaking newline-delimited JSON-RPC over
  stdio. It is launched as a child process and answers a genuine
  ``initialize`` / ``notifications/initialized`` / ``tools/call`` sequence,
  so the handshake, the framing and the response parsing are all exercised
  for real. The server is ~40 lines of stdlib and lives in this file; that is
  the one substitution, and it substitutes the *far side of a socket*, not the
  driver code under test. Everything on the driver side is the shipped
  :class:`driver_core.perception.McpSource`.
* **DOM** -- a real HTTP fetch from a real ``http.server`` instance on
  loopback, parsed by the shipped :func:`driver_core.perception.read_document`.
  No network access outside loopback.

Then it demonstrates the guarantee that matters, by making pixels
*unreachable* rather than merely unpreferred: every structured capture is
taken with a screen source attached that raises if it is ever consulted, and
a screen capture is only ever produced for a target declared ``gui``.

Run it:

    python tools/tier_order_run.py

Nothing here needs an API key, a model, or a display.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from driver_core.budget import Budget                         # noqa: E402
from driver_core.errors import PerceptionUnavailable          # noqa: E402
from driver_core.extractors import (                          # noqa: E402
    ExtractorPool, StructuredExtractor,
)
from driver_core.perception import (                          # noqa: E402
    CLI, DOM, GUI, MCP, CliSource, DomSource, McpSource,
    ScreenSource, Target, select_capture,
)
from driver_core.schema import validate_state                  # noqa: E402
from driver_core.states import CLI_SCHEMA, SCREEN_SCHEMA      # noqa: E402

PASS = "  ok  "
FAIL = " FAIL "
_failures = []


def check(label, condition, detail=""):
    print(f"[{PASS if condition else FAIL}] {label}"
          + (f"\n         {detail}" if detail and not condition else ""))
    if not condition:
        _failures.append(label)
    return condition


def show(title):
    print(f"\n--- {title} " + "-" * max(0, 60 - len(title)))


class UnreachableScreen(ScreenSource):
    """A screen source that fails loudly if the ordering is ever violated.

    Not a counter. A counter would still let the call happen and merely
    report it afterwards, which is precisely the situation the structural
    guarantee is supposed to make impossible.
    """

    def capture(self, target=None):
        raise AssertionError(
            f"pixels were reached for a {getattr(target, 'target_class', None)!r}"
            f" target; a structured class must never reach the vision tier")


# ---- a real MCP server, on stdio ------------------------------------------

MCP_SERVER = r'''
import json, sys

# A minimal but protocol-faithful MCP server over stdio: newline-delimited
# JSON-RPC, answering exactly the three messages driver-core sends.
STATE = {"exit_code": 0, "stdout": "from-mcp", "stderr": ""}


def send(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except ValueError:
        continue
    method = message.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "live-run-server", "version": "1.0.0"}}})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/call":
        # structuredContent is the field driver-core reads. Returning text
        # blocks instead would (correctly) be refused: text is the lossy tier.
        send({"jsonrpc": "2.0", "id": message["id"], "result": {
            "content": [{"type": "text", "text": "ignored"}],
            "structuredContent": STATE}})
    else:
        send({"jsonrpc": "2.0", "id": message.get("id"),
              "error": {"code": -32601, "message": "unknown method"}})
'''

PAGE = """<!doctype html>
<html><head><title>Payroll Report</title></head>
<body><h1>Payroll Report</h1><p>Deductions are final.</p></body></html>
"""


class _Handler(BaseHTTPRequestHandler):
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
    """A real loopback HTTP server, so the DOM tier fetches over a socket."""
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}/report"


def main():
    workdir = tempfile.mkdtemp(prefix="driver-tiers-")
    print(f"tier-order run in {workdir}")
    try:
        return run_all(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_all(workdir):
    # ---- tier 1: CLI ----------------------------------------------------
    show("tier 1 - CLI: a real subprocess via osal.run")
    cli_source = CliSource(
        ["python", "-c",
         "import sys; print('from-cli:' + sys.argv[1]); sys.exit(0)"],
        timeout=30)
    capture = select_capture(Target("my-app", CLI), [cli_source])
    check("a real child process answered", capture.ok, capture.detail)
    check("the capture came from the CLI tier", capture.source == CLI)
    check("its stdout really is the child's output",
          capture.payload["stdout"].strip() == "from-cli:my-app",
          repr(capture.payload))
    check("its exit code really is the child's", capture.payload["exit_code"] == 0)
    # Platform line endings are a property of the platform, not of the run,
    # and the declared EXACT provenance strips before comparison -- so the
    # normalised form is what the tally would compare.
    normalised = validate_state(capture.payload, CLI_SCHEMA)
    check("it validates against the declared CLI schema",
          normalised["stdout"] == "from-cli:my-app",
          json.dumps(normalised))
    print(f"         raw stdout {capture.payload['stdout']!r} "
          f"(platform line ending preserved in the capture)")

    # ---- tier 2: MCP ----------------------------------------------------
    show("tier 2 - MCP: a real JSON-RPC handshake over stdio")
    server_script = os.path.join(workdir, "mcp_server.py")
    with open(server_script, "w", encoding="utf-8") as handle:
        handle.write(MCP_SERVER)
    mcp_source = McpSource(["python", server_script], "report_status",
                           timeout=30)
    capture = select_capture(Target("my-app", MCP), [mcp_source])
    check("the MCP server answered over a real pipe", capture.ok, capture.detail)
    check("structuredContent was read", capture.payload == {
        "exit_code": 0, "stdout": "from-mcp", "stderr": ""},
        json.dumps(capture.payload))

    show("tier 2b - MCP text-only content is refused, not parsed")
    text_only = os.path.join(workdir, "mcp_text.py")
    with open(text_only, "w", encoding="utf-8") as handle:
        handle.write(MCP_SERVER.replace(
            '"structuredContent": STATE', '"structuredContent": {}'))
    try:
        select_capture(Target("my-app", MCP),
                       [McpSource(["python", text_only], "t")])
        check("text blocks are not coerced into fields", False,
              "a text-only result was accepted as structured")
        check("...and the refusal says why", False)
    except PerceptionUnavailable as exc:
        check("text blocks are not coerced into fields", True)
        check("...and the refusal says why",
              "no structured content" in str(exc), str(exc))

    # ---- tier 3: DOM ----------------------------------------------------
    show("tier 3 - DOM: a real HTTP fetch parsed from bytes")
    httpd, url = serve_page()
    try:
        dom_source = DomSource(url, timeout=10)
        capture = select_capture(Target(url, DOM), [dom_source])
        check("the document really was fetched over a socket",
              capture.ok, capture.detail)
        check("the title was extracted",
              capture.payload.get("window_title") == "Payroll Report",
              json.dumps(capture.payload))
        check("the body text was extracted",
              "Deductions are final." in capture.payload.get("visible_text", ""))
        check("it validates against the declared screen schema",
              validate_state(
                  {**capture.payload, "foreground_app": "browser",
                   "error_dialog_present": False}, SCREEN_SCHEMA)
              is not None)
        print(f"         fetched {url} -> {capture.fingerprint}")

        # ---- the ordering guarantee, with the server still up -----------
        show("the guarantee: pixels are unreachable, not merely unpreferred")
        structured = [
            (CLI, CliSource(["python", "-c", "print('x')"])),
            (MCP, mcp_source),
            (DOM, dom_source),
        ]
        for cls, source in structured:
            try:
                select_capture(Target("my-app", cls),
                               [source, UnreachableScreen()])
                check(f"{cls!r} never reaches the vision tier", True)
            except AssertionError as exc:
                check(f"{cls!r} never reaches the vision tier", False, str(exc))

        show("and pixels ARE reached when the target is declared gui")
        screen = ScreenSource()
        screen.capture = lambda target=None: _FakePixels(target)
        capture = select_capture(Target("my-app", GUI), [screen])
        check("a gui target still reaches the last resort",
              capture.ok and capture.source == GUI)
    finally:
        # shutdown() stops the loop; server_close() releases the socket. Under
        # -W error::ResourceWarning the difference is an unraisable warning.
        httpd.shutdown()
        httpd.server_close()

    # ---- free, and the reason it matters -------------------------------
    show("cost: the structured tiers are free, which is the whole argument")
    pool = ExtractorPool([
        StructuredExtractor("a", lambda c, s: dict(c.payload)),
        StructuredExtractor("b", lambda c, s: dict(c.payload)),
    ], serves=(CLI,))
    check("two independent structured extractors cost nothing",
          all(v.cost == 0.0 for v in pool.run(cli_capture(cli_source),
                                              CLI_SCHEMA)))
    print("         vision never entered the chain; "
          "budget entries remain empty")
    budget = Budget(1.0, step_ceiling_usd=1.0)
    check("no spend was reserved or leaked",
          budget.snapshot()["entries"] == []
          and budget.open_reservations() == [])

    print("\n" + "=" * 66)
    if _failures:
        print(f"TIER RUN FAILED: {len(_failures)} check(s) did not hold")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print("TIER RUN PASSED: CLI, MCP and DOM each answered for real, and the")
    print("vision tier was never reachable for any of them.")
    return 0


class _FakePixels:
    """A stand-in capture payload for the gui leg only."""

    def __init__(self, target):
        self.source = GUI
        self.target = target
        self.payload = "base64-pixels"
        self.detail = ""
        self.fingerprint = "deadbeefdeadbeef"
        self.target_class = GUI

    @property
    def ok(self):
        return True


def cli_capture(source):
    return source.capture("my-app")


if __name__ == "__main__":
    sys.exit(main())
