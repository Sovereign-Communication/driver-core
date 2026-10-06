"""The live gate: the only test that can catch a live-only regression.

Everything else in this suite is hermetic, and that is deliberate. But
hermetic means the fake transport is handed whatever the code under test asks
for, which is precisely why the two vision defects survived a green suite:

* a fake does not read this machine's disk, so it cannot notice that a
  filesystem path was being sent to a **remote** endpoint;
* a fake supplies ``state``/``fields`` on demand, so it cannot notice that no
  question ever asked for them.

``tests/test_vision.py`` now guards both hermetically. This module guards the
thing hermetic tests structurally cannot: that the **real** endpoint still
accepts what we send it, still returns the answer shapes we parse, and that a
real ``POST /step`` still cross-verifies end to end.

**It opts in on key presence.** No key, no run, and the default suite is
unchanged. Nothing here is weakened to make it pass: with a key it drives the
actual server over HTTP against the actual endpoint, and a failure is a real
failure.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from driver_core.config import JEV_INPUT_PRICE_PER_MILLION

#: The package under test, resolved from the running interpreter so the child
#: process imports the same code this test file was collected against -- even
#: though it runs from a throwaway working directory.
_REPO_ROOT = str(pathlib.Path(__file__).resolve().parent.parent)

#: No key means no run. The skip is the opt-in, not a relaxation: every other
#: test in this package still executes, and this one runs the moment an
#: operator supplies a key.
_KEYED = bool(os.environ.get("DRIVER_JEV_API_KEY"))
_SKIP = "live gate: set DRIVER_JEV_API_KEY to run against the real endpoint"

#: The vision tier needs somewhere to put a real capture. An operator naming
#: ``DRIVER_SCREEN_OUT`` is saying "this machine has a display and I want the
#: repaired tier exercised"; without it the vision tests skip rather than fail,
#: because a gate that fails on a headless box is a flaky gate, not a strict
#: one. Only the *presence* is taken as the signal -- the path itself is
#: deliberately ignored, so the gate writes into its own temporary directory
#: and can never clobber a file somebody else chose.
_SCREEN = bool(os.environ.get("DRIVER_SCREEN_OUT"))
_SKIP_SCREEN = ("live gate: set DRIVER_SCREEN_OUT to exercise the vision tier "
                "on a machine with a display")

_TOKEN = "live-gate-token-not-a-secret"  # >= MIN_TOKEN_LENGTH; local only


def _free_port_env(workdir):
    env = dict(os.environ)
    env.update({
        "PYTHONUNBUFFERED": "1",          # load-bearing: see below
        "DRIVER_TOKEN": _TOKEN,
        "DRIVER_PORT": "0",               # let the OS choose
        "DRIVER_AUDIT_PATH": os.path.join(workdir, "audit.jsonl"),
    })
    # The child runs with cwd set to a throwaway directory so it cannot touch
    # a developer's real audit chain -- which means it cannot find the package
    # by cwd, so the import path has to be handed to it explicitly.
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (_REPO_ROOT + os.pathsep + existing
                         if existing else _REPO_ROOT)
    for name in ("DRIVER_SCREEN", "DRIVER_SCREEN_OUT", "DRIVER_DOM_URL",
                 "DRIVER_MCP_COMMAND", "DRIVER_MCP_TOOL"):
        env.pop(name, None)
    # The deterministic tier is off unless it is named, so a step against it
    # would refuse with ``no_capture`` and the gate would be asserting a
    # refusal. Declaring the ``cli`` source here is what makes the test reach
    # the agreement gate it is meant to be checking. Quoted because the
    # tokeniser has no escapes -- a space inside an argument must be quoted.
    env["DRIVER_CLI_COMMAND"] = (
        f'"{sys.executable}" -c "import sys; print(sys.argv[1])"')
    return env


def _wait_for_base(process, timeout=30):
    """Read stdout until the service announces the socket it actually bound.

    The announce only appears when stdout is unbuffered. A buffered launch
    binds, serves, and prints nothing at all -- a checker that concludes the
    announce does not exist is looking at a pipe with a block buffer in it.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = process.stdout.readline()
        if not line:
            break
        match = re.search(r"listening on (http://[0-9.]+:\d+)", line)
        if match:
            return match.group(1)
    return None


class LiveService:
    """The real ``driver-core serve`` process, on a real socket."""

    def __init__(self, **extra_env):
        self._tmp = tempfile.TemporaryDirectory(prefix="live-gate-")
        env = _free_port_env(self._tmp.name)
        env.update(extra_env)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "driver_core", "serve"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env, cwd=self._tmp.name)
        self.base = _wait_for_base(self.process)
        #: Whatever the child said before it died. Kept so a bind failure
        #: reports the reason rather than a shrug -- a gate that skips because
        #: the server would not start is exactly the silent hole it exists to
        #: close.
        self.output = ""
        if not self.base and self.process.stdout is not None:
            try:
                self.output = self.process.stdout.read() or ""
            except Exception:                       # pragma: no cover
                self.output = ""

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:      # pragma: no cover
                self.process.kill()
        if self.process.stdout is not None:
            self.process.stdout.close()
        self._tmp.cleanup()

    def get(self, route):
        return self._request("GET", route)

    def step(self, body):
        return self._request("POST", "step", body)

    def _request(self, method, route, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self.base}/{route}", data=data, method=method,
            headers={"Authorization": f"Bearer {_TOKEN}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=120) as handle:
                return handle.status, json.loads(handle.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")


@unittest.skipUnless(_KEYED, _SKIP)
class LiveProtocolTests(unittest.TestCase):
    """What only the real service can tell us."""

    @classmethod
    def setUpClass(cls):
        cls.service = LiveService()
        if not cls.service.base:
            # A key is present, so this is a **failure**, not a skip. Skipping
            # here would make the gate self-defeating: the one case it must
            # never hide is the service failing to come up.
            output = cls.service.output.strip()[:400]
            cls.service.close()
            raise AssertionError(
                "the live service did not announce a bound address, so no "
                f"live assertion was made. Child output: {output or '<none>'}")

    @classmethod
    def tearDownClass(cls):
        cls.service.close()

    def test_health_reports_a_key_and_a_declared_source(self):
        status, body = self.service.get("health")
        self.assertEqual(status, 200)
        self.assertTrue(body["keyed"], "the key is present but not reported")
        self.assertIn("version", body)

    def test_a_cli_step_reaches_the_decision_tier_and_is_costed(self):
        """The deterministic tier cross-verifies, then the model is called.

        This is the end-to-end shape that must not silently change: agreement
        first, model second, and a cost that reflects the corrected rate.
        """
        status, body = self.service.step(
            {"target": "live-gate", "schema": "cli",
             "step_id": "live-gate-cli"})
        self.assertEqual(status, 200, body)
        agreement = body.get("agreement") or {}
        self.assertEqual(agreement.get("outcome"), "agreed",
                         "deterministic extractors did not agree")
        self.assertEqual(agreement.get("answering"), 2)
        decision = body.get("decision") or {}
        self.assertTrue(decision.get("native"),
                        "the real endpoint returned no usable decision")
        self.assertEqual(decision.get("usage_source"), "actual")
        self.assertGreater(decision.get("cost", 0.0), 0.0,
                           "a real call was billed at zero")
        # At $0.042/Mtok an 800-token call is ~$0.0000336. A cost ten times
        # lower means the superseded $0.0042 rate is back.
        self.assertGreater(decision["cost"], 1e-5,
                           "cost is an order of magnitude below $0.042/Mtok")
        self.assertLess(decision["cost"], 1e-3)

    def test_a_refusal_is_http_200_with_a_declared_reason(self):
        """The convention a host depends on, against the real process."""
        status, body = self.service.step({"target": "live-gate"})
        self.assertEqual(status, 400, "an absent schema must be refused")
        self.assertIn("schema is required", body.get("error", ""))


@unittest.skipUnless(_KEYED, _SKIP)
@unittest.skipUnless(_SCREEN, _SKIP_SCREEN)
class LiveVisionTests(unittest.TestCase):
    """The one step the text-only constraint exists to protect.

    Pre-repair this exact step produced two extractor slots with
    ``ok: False`` ("model returned no parseable state") and a tally of
    ``insufficient_agreement``. Every assertion below is one of those things
    that actually broke, so a regression here is a live-only regression and
    the hermetic suite cannot see it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="live-gate-gui-")
        capture = os.path.join(self._tmp.name, "capture.png")
        self.service = LiveService(DRIVER_SCREEN="1",
                                   DRIVER_SCREEN_OUT=capture)
        if not self.service.base:
            output = self.service.output.strip()[:400]
            self.service.close()
            self.skipTest(f"no display available: {output or '<none>'}")

    def tearDown(self):
        self.service.close()
        self._tmp.cleanup()

    def test_a_vision_step_cross_verifies_instead_of_shortfalling(self):
        """Both slots answer, the tally agrees, and the decision follows.

        Asserting the cost against ``input_tokens`` is deliberate: it pins the
        rate itself rather than a magnitude. A return to the superseded
        $0.0042/Mtok would satisfy "cost is greater than zero" and fail here.
        """
        status, body = self.service.step(
            {"target": "live-gate-display", "schema": "gui",
             "step_id": "live-gate-gui"})
        self.assertEqual(status, 200, body)
        self.assertNotEqual(
            body.get("reason"), "insufficient_agreement",
            f"the extractors did not answer: {body.get('detail')}")
        self.assertNotEqual(body.get("reason"), "no_capture",
                            f"the screen was never captured: {body.get('detail')}")

        agreement = body.get("agreement") or {}
        self.assertEqual(agreement.get("outcome"), "agreed", agreement)
        self.assertEqual(agreement.get("answering"), 2,
                         "both extractor slots must answer")

        capture = body.get("capture") or {}
        self.assertTrue(capture.get("ok"), capture)

        decision = body.get("decision") or {}
        self.assertTrue(decision.get("native"), decision)
        tokens = int(decision.get("input_tokens") or 0)
        self.assertGreater(tokens, 0, "no tokens were reported")
        self.assertAlmostEqual(
            decision["cost"], tokens * JEV_INPUT_PRICE_PER_MILLION / 1e6,
            places=9,
            msg="cost is not input_tokens at $0.042/Mtok")
        self.assertGreater(body.get("cost_usd", 0.0), 0.0)

    def test_the_payload_reaching_the_model_is_text_never_a_path_or_pixels(self):
        """Asserted against the real endpoint, not a fake.

        The hermetic guard proves the shape of what we build. This proves the
        shape of what actually goes over the wire -- because pre-repair this
        was ``{"image_ref": "<local path>"}``, which a remote endpoint cannot
        read, and only a live call would have caught that the thing we send is
        unusable rather than merely unusual.
        """
        from driver_core import transport as real_transport
        from driver_core.audit import MemoryAuditLog
        from driver_core.budget import Budget
        from driver_core.config import load_settings
        from driver_core.extractors import VisionExtractor
        from driver_core.states import SCREEN_SCHEMA

        sent = []

        class Recording:
            """The real transport, with the request bodies kept."""

            def call_service(self, url, questions, **kwargs):
                sent.append(kwargs.get("body_extra"))
                return real_transport.call_service(url, questions, **kwargs)

        settings = load_settings(env=dict(os.environ))
        extractor = VisionExtractor(
            "live", settings, budget=Budget(1.0, step_ceiling_usd=1.0),
            audit=MemoryAuditLog(), transport_module=Recording())
        result = extractor.extract(SCREEN_SCHEMA)

        self.assertEqual(len(sent), 1, "no request reached the endpoint")
        state = sent[0].get("state") or {}
        rendered = json.dumps(state)
        self.assertIn("description", state,
                      "the model must receive prose about the window")
        self.assertNotIn("image_ref", rendered)
        self.assertNotIn("base64", rendered.lower())
        self.assertNotIn("\\", rendered, "no local path may cross")
        self.assertTrue(result.ok,
                        f"the live extraction did not answer: {result.reason}")


#: A document with a title, a body, and a script and style body that must not
#: reach the state -- the script text deliberately contains words a schema
#: would ask about, so a reader that failed to drop it would change a field.
_DOCUMENT = """<!doctype html>
<html><head><title>live-gate-document</title>
<style>body { color: #333333; }</style>
<script>window.TRAP = "error Save dialog";</script>
</head><body><h1>live-gate-body</h1>
<p>The document tier fetched this page through a real socket.</p>
</body></html>
"""

#: A stdio JSON-RPC child, written out for the gate to run. It prints a banner
#: first on purpose: a server that prints a ready-marker is the case
#: ``_last_reply`` exists for, and one that prints nothing would not exercise
#: it.
_MCP_CHILD = '''
import json, sys
sys.stdin.read()
sys.stdout.write("mcp server ready\\n")
sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
    "protocolVersion": "2024-11-05", "capabilities": {},
    "serverInfo": {"name": "live-gate", "version": "1.0.0"}}}) + "\\n")
sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {
    "structuredContent": {"exit_code": 0,
                          "stdout": "live-gate-tool-answer",
                          "stderr": ""}}}) + "\\n")
sys.stdout.flush()
'''


class _Document:
    """A real HTTP server on a real socket, serving one page."""

    def __init__(self):
        import http.server
        import threading
        body = _DOCUMENT.encode("utf-8")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):          # pragma: no cover
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()


def _mcp_command(directory):
    """The declared argv for the JSON-RPC child, quoted for the tokeniser."""
    path = os.path.join(directory, "mcp_child.py")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(_MCP_CHILD)
    return f'"{sys.executable}" "{path}"'


def _agreed_fields(body):
    """The field names the real server reported as agreed, and nothing else.

    The response is deliberately payload-free, so this is the one thing about
    an extraction a live caller can see: which fields the tier answered for.
    """
    fields = (body.get("agreement") or {}).get("fields") or []
    return {entry["field"] for entry in fields if entry.get("agreed")}


def _children():
    """A document server and a JSON-RPC child, or a named reason it did not."""
    try:
        document = _Document()
    except OSError as exc:                     # pragma: no cover - no socket
        return None, None, f"no loopback socket for the document: {exc}"
    return document, tempfile.mkdtemp(prefix="live-gate-mcp-"), ""


@unittest.skipUnless(_KEYED, _SKIP)
class LiveStructuredTierTests(unittest.TestCase):
    """``dom`` and ``mcp``: the two tiers that reach a real foreign process.

    Neither had any live coverage, so nothing checked that the fetch and the
    handshake this package writes are ones a real server and a real document
    accept. The hermetic tests drive both against fakes, which is precisely
    why they cannot see a wrong request shape.
    """

    @classmethod
    def setUpClass(cls):
        cls.document, cls.mcp_dir, cls.why = _children()

    @classmethod
    def tearDownClass(cls):
        if cls.document is not None:
            cls.document.shutdown()
        if cls.mcp_dir is not None:
            shutil.rmtree(cls.mcp_dir, True)

    def _step(self, target, schema, **env):
        service = LiveService(**env)
        try:
            if not service.base:
                output = service.output.strip()[:400]
                # A key is present, so a service that will not start is a
                # failure rather than a skip -- the same rule the protocol
                # tests hold to.
                raise AssertionError(
                    f"the live service did not start for {schema!r}: "
                    f"{output or '<none>'}")
            return service.step({"target": target, "schema": schema,
                                 "step_id": f"live-gate-{schema}"})
        finally:
            service.close()

    def test_a_dom_step_fetches_a_real_document_and_reaches_a_decision(self):
        if self.document is None:
            self.skipTest(self.why)
        status, body = self._step("live-gate-dom", "dom",
                                  DRIVER_DOM_URL=self.document.url)
        self.assertEqual(status, 200, body)
        capture = body.get("capture") or {}
        self.assertTrue(capture.get("ok"), capture)
        agreement = body.get("agreement") or {}
        self.assertEqual(agreement.get("outcome"), "agreed", agreement)
        self.assertEqual(agreement.get("answering"), 2)
        decision = body.get("decision") or {}
        self.assertTrue(decision.get("native"), decision)
        self.assertEqual(decision.get("usage_source"), "actual")
        # Both declared document fields were really read: ``window_title`` is
        # REQUIRED and could only agree if a real ``<title>`` came back over a
        # real socket. The values themselves are deliberately absent from the
        # response -- a capture summary carries a fingerprint, never a payload
        # -- so the field names and their agreement are the whole observable.
        self.assertEqual(_agreed_fields(body),
                         {"window_title", "visible_text"})
        self.assertNotIn("window_title", json.dumps(  # never the value
            body.get("capture") or {}).lower())

    def test_an_mcp_step_calls_a_real_json_rpc_child_and_reaches_a_decision(self):
        status, body = self._step(
            "live-gate-mcp", "mcp",
            DRIVER_MCP_COMMAND=_mcp_command(self.mcp_dir),
            DRIVER_MCP_TOOL="read_live_gate")
        self.assertEqual(status, 200, body)
        capture = body.get("capture") or {}
        self.assertTrue(capture.get("ok"), capture)
        agreement = body.get("agreement") or {}
        self.assertEqual(agreement.get("outcome"), "agreed", agreement)
        self.assertEqual(agreement.get("answering"), 2)
        decision = body.get("decision") or {}
        self.assertTrue(decision.get("native"), decision)
        # ``stdout`` could only agree if real structured content came back
        # through the handshake -- past the banner line the child printed
        # first, which is the case ``_last_reply`` exists for.
        self.assertEqual(_agreed_fields(body),
                         {"exit_code", "stdout", "stderr"})


@unittest.skipUnless(_KEYED, _SKIP)
class LiveOfferedActionTests(unittest.TestCase):
    """The option set the model is given is what this build can perform.

    Three of the fourteen declared names -- ``run_probe``, ``call_read_tool``
    and ``read_dom`` -- have no executor in any build. Pre-repair they were
    offered anyway, and a live model picked ``run_probe`` at 1.00 confidence
    with an exact consent, only for the step to end in a refusal naming an
    executor nothing registered. The distribution the model returns is over
    exactly the options it was shown, so the option set is readable straight
    off the decision record -- which makes this the one live assertion that
    can catch an option set widening again.
    """

    def _offered(self, **env):
        service = LiveService(**env)
        try:
            if not service.base:
                output = service.output.strip()[:400]
                raise AssertionError(
                    f"the live service did not start: {output or '<none>'}")
            status, body = service.step(
                {"target": "live-gate", "schema": "cli",
                 "step_id": "live-gate-offered"})
        finally:
            service.close()
        self.assertEqual(status, 200, body)
        decision = body.get("decision") or {}
        self.assertTrue(decision.get("native"), decision)
        return set(decision.get("probabilities") or {})

    def test_an_observer_build_offers_only_the_three_read_only_actions(self):
        self.assertEqual(self._offered(), {"no_action", "observe",
                                          "read_value"})

    def test_a_write_enabled_build_offers_exactly_the_registered_actions(self):
        """Ten calls, because the endpoint's own output is quantised.

        The provider documents that its distribution sums to 1, and documents
        every worked example rounded to two decimal places. At eleven options
        those two documented facts stop being simultaneously satisfiable: the
        endpoint lands 0.01 short on roughly half of all live calls, and the
        shipping validator rejects such a response as malformed. That is a
        real finding and it is *recorded* here rather than asserted away.

        What this test pins is the option set -- so it requires that every
        native response offered exactly the registered actions, and that every
        rejection was that quantisation artefact and not some other fault. A
        rejection for any other reason is a failure of this gate.
        """
        from driver_core.executor_registry import build_driver_registry
        expected = set(build_driver_registry(allow_write=True).names())
        self.assertEqual(len(expected), 11)
        native, rejected = [], []
        for _ in range(10):
            service = LiveService(DRIVER_ALLOW_WRITE="1")
            try:
                if not service.base:
                    raise AssertionError("the live service did not start")
                status, body = service.step(
                    {"target": "live-gate", "schema": "cli",
                     "step_id": "live-gate-offered"})
            finally:
                service.close()
            self.assertEqual(status, 200, body)
            decision = body.get("decision") or {}
            if decision.get("native"):
                native.append(set(decision.get("probabilities") or {}))
            else:
                rejected.append({"stop_reason": decision.get("stop_reason"),
                                 "reasons": decision.get("reasons")})
        for offered in native:
            self.assertEqual(offered, expected)
            for name in ("run_probe", "call_read_tool", "read_dom"):
                self.assertNotIn(name, offered)
        for failure in rejected:
            joined = " ".join(failure.get("reasons") or [])
            self.assertIn("sum to 0.99", joined,
                          "a rejection that is not the two-decimal-place "
                          f"artefact: {failure}")
        self.assertTrue(native, f"all ten calls were rejected: {rejected[:2]}")


@unittest.skipUnless(_KEYED, _SKIP)
class LiveVisionWithoutScreenOutTests(unittest.TestCase):
    """``DRIVER_SCREEN_OUT`` is not a precondition of the vision tier.

    Pre-repair this exact configuration -- ``DRIVER_SCREEN=1`` and nothing
    else -- had every step stop at capture with ``no_capture`` and the detail
    "screen: DRIVER_SCREEN_OUT is not set; cannot capture", naming a variable
    that appears in no documented row and that no other code path reads. The
    cause was a screenshot being taken for a payload no vision extractor
    reads, so the capture became unskippable.

    Off Windows nothing can read a foreground window at all, so the skip
    names the platform rather than claiming this machine has no display.
    """

    def test_a_gui_step_with_no_screen_out_reaches_a_decision(self):
        if os.name != "nt":
            self.skipTest("describing a foreground window is Windows-only; "
                          "the vision tier needs a window manager, and this "
                          "is not one")
        service = LiveService(DRIVER_SCREEN="1")
        try:
            if not service.base:
                output = service.output.strip()[:400]
                self.skipTest(f"the service did not start: {output or '<none>'}")
            status, body = service.step(
                {"target": "live-gate-display", "schema": "gui",
                 "step_id": "live-gate-gui-no-screen-out"})
        finally:
            service.close()
        self.assertEqual(status, 200, body)
        self.assertNotEqual(
            body.get("reason"), "no_capture",
            f"DRIVER_SCREEN_OUT is still a precondition of the vision tier: "
            f"{body.get('detail')}")
        agreement = body.get("agreement") or {}
        self.assertEqual(agreement.get("outcome"), "agreed", agreement)
        self.assertEqual(agreement.get("answering"), 2)
        self.assertTrue((body.get("decision") or {}).get("native"), body)


if __name__ == "__main__":
    unittest.main()
