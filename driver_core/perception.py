"""The perception tier: what the driver is looking at.

The single most consequential decision in this module is that **structured
input is tried before pixels**, every time.

A CLI's stdout, an MCP tool's JSON result, and a DOM or accessibility tree
are all already the state a human would reason about, in a form a program can
read exactly. A screenshot of the same application is a lossy, unlabelled
rendering of it that costs real money to interpret and can be read two
different ways by two different extractors. When a structured source exists
it is strictly better on every axis that matters here -- determinism,
testability, cost, and accuracy.

So the ordering is not an optimisation, it is the design:

    CLI  ->  MCP  ->  DOM  ->  screen pixels

and pixels are reached only when the three structured sources cannot answer.
That single choice is what keeps most runs free, hermetic, and reproducible,
and it is why the vision adapter is a small, isolated, last-resort component
rather than the core of the system.

**The ordering is structural, not advisory.** An earlier version tried the
structured tiers in order and then fell back to a screen capture -- correct in
practice, but the guarantee lived in the shape of a ``for`` loop, so nothing
stopped a caller registering a screen source that answered a DOM target, and
nothing could *prove* the claim. Three of the four target classes can be
served without pixels at all, and that deserves to be a property of the
types rather than a promise in a docstring:

* a :class:`Target` carries the class it was declared to be (:data:`CLI`,
  :data:`MCP`, :data:`DOM`, or :data:`GUI`);
* every source declares which classes it can answer, in ``serves``;
* :func:`select_capture` builds its candidate set *first* and filters by
  class, so for a ``cli``, ``mcp`` or ``dom`` target the screen source is
  never a candidate and is therefore never called.

The consequence is that "this run never spent a vision token" is checkable
rather than aspirational: a source that would have violated the ordering
cannot be reached, and a test can assert it with a source that raises if
touched.

A bare string target is still accepted, and is deliberately *weaker*: an
undeclared class permits any source, with pixels last as before. That
preserves every existing caller while making the strong form available to
callers who want it, and it is the reason :class:`Target` exists rather than
the string having been changed outright.

Every capture is also **opaque and short-lived**. A path, a handle, a base64
blob -- whichever the source produces -- is passed forward and never copied
into an audit record, because a log of screen captures is a log of everything
the operator has looked at.
"""
import base64
import hashlib
import json

from . import osal
from .errors import PerceptionUnavailable

#: Preference order. Named, not incidental.
SOURCE_ORDER = ("cli", "mcp", "dom", "screen")

#: The four target classes the driver can be pointed at. Three are answerable
#: from structured input; only :data:`GUI` has no structured representation
#: this package can read without a browser engine.
CLI = "cli"
MCP = "mcp"
DOM = "dom"
GUI = "gui"

TARGET_CLASSES = (CLI, MCP, DOM, GUI)

#: The classes that never need pixels. Declared as data so the guarantee can
#: be asserted against the declaration rather than against a comment.
STRUCTURED_CLASSES = (CLI, MCP, DOM)

#: The one class that falls through to a screen capture.
VISION_CLASS = GUI


class Target:
    """What to observe, and the class it was declared to be.

    The class is the load-bearing half. Without it this is just a string,
    and the only thing the chain can do is try things in order until one
    answers -- which is a weaker property, because "tried in order" is a
    statement about the code path taken rather than about what was reachable.

    ``target_class=None`` means undeclared, and is accepted for compatibility
    with callers that pass a plain string. It permits any source to answer,
    pixels last. Prefer a declared class: it is the difference between
    "pixels were not needed" and "pixels were not reached".
    """

    __slots__ = ("ref", "target_class")

    def __init__(self, ref, target_class=None):
        if target_class is not None and target_class not in TARGET_CLASSES:
            raise PerceptionUnavailable(
                f"unknown target class {target_class!r}; declared classes are "
                f"{list(TARGET_CLASSES)}")
        self.ref = ref
        self.target_class = target_class

    @property
    def declared(self):
        return self.target_class is not None

    @property
    def structured(self):
        """True when this class is answerable without pixels."""
        return self.target_class in STRUCTURED_CLASSES

    def to_dict(self):
        return {"target": self.ref, "class": self.target_class}

    def __str__(self):
        return str(self.ref)

    def __repr__(self):
        return f"Target({self.ref!r}, {self.target_class!r})"


def as_target(value):
    """Accept either a :class:`Target` or a bare string, unchanged behaviour."""
    if isinstance(value, Target):
        return value
    return Target(value, None)


class Capture:
    """One observation of the target.

    ``fingerprint`` is a short digest of the bytes observed. It exists so two
    steps can be proven to have looked at the *same* screen without storing
    the screen -- which is how a re-plan after an extraction disagreement can
    tell "the window changed under us" from "the extractors just disagreed
    about an unchanged window".
    """

    __slots__ = ("source", "target", "target_class", "payload", "detail",
             "fingerprint")

    def __init__(self, source, target, payload=None, detail="", fingerprint=""):
        self.source = source
        # Coerced here rather than trusted from callers, because this value
        # is written straight into the audit log. A Target object reaching
        # json.dumps would raise at the moment a capture succeeded, which is
        # the worst possible time to discover it.
        self.target = target.ref if isinstance(target, Target) else target
        #: Carried alongside the capture so the extraction tier can select a
        #: pool without re-deriving the target the caller handed in.
        self.target_class = (target.target_class
                             if isinstance(target, Target) else None)
        self.payload = payload
        self.detail = detail
        self.fingerprint = fingerprint

    @property
    def ok(self):
        return self.payload is not None

    @property
    def cost_usd(self):
        """A capture's own cost. Zero for every structured tier."""
        return 0.0

    def summary(self):
        """A description safe to write to a log. Never carries the payload."""
        return {"source": self.source, "target": self.target,
                "ok": self.ok, "detail": self.detail,
                "fingerprint": self.fingerprint}

    def __repr__(self):
        return f"Capture({self.source!r}, ok={self.ok})"


def fingerprint(payload):
    """A short, stable digest of an observed payload."""
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        material = payload
    else:
        material = str(payload).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


class StructuredSource:
    """A source that already has the state in machine-readable form.

    ``reader`` is a callable ``(target) -> payload | None``, and ``target``
    is the plain ref -- never a :class:`Target`. The declared class governs
    which sources the chain will *ask*; it is not something a reader needs
    in order to read.

    ``serves`` declares which target classes this source can answer. It
    defaults to the source's own name, so ``StructuredSource("dom", ...)``
    serves ``dom`` and nothing else unless told otherwise.
    """

    def __init__(self, name, reader, *, serves=None, cost_usd=0.0):
        if name not in SOURCE_ORDER:
            raise PerceptionUnavailable(
                f"unknown structured source {name!r}; declared sources are "
                f"{list(SOURCE_ORDER)}")
        self.name = name
        self._reader = reader
        self.cost = float(cost_usd)
        self.serves = self._declared_serves(name, serves)

    @staticmethod
    def _declared_serves(name, serves):
        if serves is None:
            # A structured source serves its own class. Declaring it here
            # rather than defaulting to "everything" is what makes the
            # ordering enforceable instead of merely intended.
            return (name,) if name in TARGET_CLASSES else ()
        unknown = sorted(set(serves) - set(TARGET_CLASSES))
        if unknown:
            raise PerceptionUnavailable(
                f"source {name!r} serves unknown target class(es) {unknown}; "
                f"declared classes are {list(TARGET_CLASSES)}")
        return tuple(serves)

    def can_serve(self, target):
        """Whether this source is even a candidate for ``target``.

        An undeclared target class permits anything -- that is the
        compatibility path, and it is why a bare string is weaker. A declared
        class is a commitment: a source that does not serve it is not asked,
        so it cannot be reached, paid for, or accidentally preferred.
        """
        if not self.serves:
            return False
        if not target.declared:
            return True
        return target.target_class in self.serves

    def capture(self, target):
        # The reader is handed the *ref*, not the Target. A reader wants the
        # thing being observed; the declared class is bookkeeping that
        # concerns the chain, not the source. Passing the Target through
        # would break every reader that does anything real with the value --
        # open(path), argv.append(target), urljoin(base, target) -- with a
        # TypeError from somewhere deep inside a caller's code.
        ref = target.ref if isinstance(target, Target) else target
        try:
            payload = self._reader(ref)
        except Exception as exc:
            return Capture(self.name, target, None,
                           detail=f"reader failed: {exc}")
        if payload is None:
            return Capture(self.name, target, None, detail="source had nothing")
        return Capture(self.name, target, payload,
                       fingerprint=fingerprint(payload))


class ScreenSource:
    """The last resort: pixels.

    Serves :data:`GUI` and only :data:`GUI`. That single declaration is what
    keeps the vision tier out of reach for the three structured classes: a
    ``dom`` target cannot reach this object, so it cannot be called, billed,
    or quietly preferred.

    Registration is explicit and failure is named. A driver that cannot
    capture returns a reason, and the caller routes to a structured source or
    stops -- it never hands a vision model a path that does not exist.
    """

    serves = (GUI,)

    def __init__(self, *, encoder=None):
        self.name = "screen"
        self._encoder = encoder or _default_encoder

    def can_serve(self, target):
        # Same rule as StructuredSource, but it cannot be overridden: this is
        # the one source whose reachability is the ordering guarantee.
        if not target.declared:
            return True
        return target.target_class == GUI

    def capture(self, target=None):
        path, detail = osal.capture_screen()
        if not path:
            return Capture("screen", target, None, detail=detail)
        try:
            payload = self._encoder(path)
        except Exception as exc:
            return Capture("screen", target, None,
                           detail=f"could not read the capture: {exc}")
        if payload is None:
            return Capture("screen", target, None,
                           detail="capture produced no readable bytes")
        return Capture("screen", target, payload, fingerprint=fingerprint(payload))


class CliSource:
    """Tier 1: a real subprocess, read through :mod:`driver_core.osal`.

    A command either produced output or it did not. There is no model, no
    pixels and no interpretation in this tier, which is why it is first and
    why three of the four target classes can avoid the vision extractor
    entirely.

    The target is appended as its **own argv element** rather than
    substituted into the command. There is no shell here, so this is not a
    shell-injection surface -- but splicing a caller-supplied string into the
    middle of a command *is* argument injection if the target happens to look
    like a flag. Appending it means a hostile target can only ever be one
    argument, which is a much smaller thing to reason about.
    """

    name = CLI
    serves = (CLI,)

    def __init__(self, command, *, cwd=None, timeout=None, extra_env=None):
        if isinstance(command, str):
            raise PerceptionUnavailable(
                "CliSource takes an argv list, not a string; a string would "
                "be handed to the platform shell")
        argv = list(command)
        if not argv:
            raise PerceptionUnavailable("CliSource needs a non-empty command")
        self.command = argv
        self.cwd = cwd
        self.timeout = timeout
        self.extra_env = dict(extra_env or {})

    def can_serve(self, target):
        if not target.declared:
            return True
        return target.target_class == CLI

    def capture(self, target):
        argv = list(self.command) + [str(target)]
        kwargs = {}
        if self.cwd is not None:
            kwargs["cwd"] = self.cwd
        if self.timeout is not None:
            kwargs["timeout"] = self.timeout
        if self.extra_env:
            kwargs["env"] = self.extra_env
        result = osal.run(argv, **kwargs)
        if not result.ok and result.reason:
            # "not found" and "timed out" are failures to ask, not answers.
            return Capture(self.name, target, None,
                           detail=f"command did not run: {result.error}")
        return Capture(
            self.name, target,
            {"exit_code": result.returncode,
             "stdout": result.stdout,
             "stderr": result.stderr},
            fingerprint=fingerprint({"exit_code": result.returncode,
                                     "stdout": result.stdout,
                                     "stderr": result.stderr}))


class McpSource:
    """Tier 2: a real MCP tool call over stdio JSON-RPC.

    Speaks newline-delimited JSON-RPC to a declared child process through
    :func:`driver_core.osal.run`: ``initialize``, then
    ``notifications/initialized``, then ``tools/call``. The handshake is not
    ceremony -- a compliant server is entitled to refuse a ``tools/call``
    that was never initialised, and this package would then report an empty
    result as though the tool had answered.

    Zero dependencies is the whole reason this is spelled out rather than
    delegated: an MCP client is the sort of thing that arrives as a package,
    and a driver that acts on a machine is the last place to add one.
    """

    name = MCP
    serves = (MCP,)

    def __init__(self, command, tool, *, timeout=30, extra_env=None,
                 client_name="driver-core"):
        if isinstance(command, str):
            raise PerceptionUnavailable(
                "McpSource takes an argv list, not a string")
        argv = list(command)
        if not argv:
            raise PerceptionUnavailable("McpSource needs a non-empty command")
        self.command = argv
        self.tool = tool
        self.timeout = timeout
        self.extra_env = dict(extra_env or {})
        self.client_name = client_name

    def can_serve(self, target):
        if not target.declared:
            return True
        return target.target_class == MCP

    def _envelope(self, payload):
        """Newline-delimited JSON-RPC, which is the MCP stdio framing."""
        return json.dumps(payload) + "\n"

    def capture(self, target):
        request = "".join((
            self._envelope({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05",
                           "capabilities": {},
                           "clientInfo": {"name": self.client_name,
                                          "version": "1.0.0"}}}),
            self._envelope({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            self._envelope({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": self.tool,
                           "arguments": {"target": str(target)}}}),
        ))
        kwargs = {"timeout": self.timeout, "input_text": request}
        if self.extra_env:
            kwargs["env"] = self.extra_env
        result = osal.run(self.command, **kwargs)
        if not result.ok and result.reason:
            return Capture(self.name, target, None,
                           detail=f"mcp server did not run: {result.error}")
        reply = _last_reply(result.stdout)
        if reply is None:
            return Capture(self.name, target, None,
                           detail="mcp server returned no parseable response")
        if "error" in reply:
            return Capture(self.name, target, None,
                           detail=f"mcp error: {reply['error']}")
        payload = _tool_payload(reply.get("result"))
        if payload is None:
            return Capture(self.name, target, None,
                           detail="mcp tool returned no structured content")
        return Capture(self.name, target, payload, fingerprint=fingerprint(payload))


def _last_reply(stdout):
    """The final JSON-RPC response on a server's stdout.

    A server is free to print banners and progress lines, so the stream is
    scanned line by line and the last well-formed response wins. Parsing the
    whole stream as one document -- the obvious implementation -- breaks on
    the first server that prints a ready-marker, which is most of them.
    """
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("jsonrpc"):
            return payload
    return None


def _tool_payload(result):
    """The structured content of a ``tools/call`` result, or ``None``.

    Text content blocks are deliberately *not* coerced. They are the MCP
    equivalent of reading pixels -- a lossy rendering a program has to parse
    by guessing -- and parsing one into fields here would reintroduce the
    unreliable tier at the end of a chain whose whole argument is that the
    structured tiers are not guesses.
    """
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and structured:
        return structured
    return None


class DomSource:
    """Tier 3: the document tree, read without a browser engine.

    Fetches a declared URL and extracts the document title and visible text
    with the standard library's own HTML parser -- no engine, no headless
    browser, no dependency. That is enough to answer a declared schema for a
    DOM target, which is the point: if a DOM target can be answered here,
    it never reaches pixels.

    The target is **not** substituted into the URL. Splicing a caller string
    into a URL is how a driver ends up fetching whatever it was told to, and
    a declared URL is both safer and more auditable. The target is carried as
    a label on the capture, which is all it is needed for.
    """

    name = DOM
    serves = (DOM,)

    def __init__(self, url, *, timeout=20, opener=None, title_tag="title"):
        self.url = url
        self.timeout = timeout
        self._opener = opener
        self.title_tag = title_tag

    def can_serve(self, target):
        if not target.declared:
            return True
        return target.target_class == DOM

    def capture(self, target):
        opener = self._opener or osal.http_get
        try:
            status, body = opener(self.url, timeout=self.timeout)
        except Exception as exc:
            return Capture(self.name, target, None,
                           detail=f"document could not be fetched: {exc}")
        if status != 200:
            return Capture(self.name, target, None,
                           detail=f"document returned HTTP {status}")
        try:
            payload = read_document(body, title_tag=self.title_tag)
        except Exception as exc:
            return Capture(self.name, target, None,
                           detail=f"document could not be read: {exc}")
        if not payload:
            return Capture(self.name, target, None,
                           detail="document had no readable text")
        return Capture(self.name, target, payload, fingerprint=fingerprint(payload))


def read_document(body, *, title_tag="title"):
    """Extract the declared fields from an HTML document.

    Returns ``{"window_title": ..., "visible_text": ...}``. Script and style
    bodies are dropped: they are the parts of a page most likely to contain
    the word "error" or "Save" in a string literal, and a vision model does
    not read them either -- leaving them in would make this tier disagree
    with the pixel tier for no good reason.
    """
    from html.parser import HTMLParser

    class _Reader(HTMLParser):
        def __init__(self, title_tag):
            super().__init__(convert_charrefs=True)
            self.title_tag = title_tag
            self.title = []
            self.text = []
            self._skip = 0
            self._in_title = False

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self._skip += 1
            if tag == self.title_tag:
                self._in_title = True

        def handle_endtag(self, tag):
            if tag in ("script", "style") and self._skip:
                self._skip -= 1
            if tag == self.title_tag:
                self._in_title = False

        def handle_data(self, data):
            if self._skip:
                return
            text = " ".join(data.split())
            if not text:
                return
            if self._in_title:
                self.title.append(text)
            else:
                self.text.append(text)

    reader = _Reader(title_tag)
    reader.feed(body or "")
    reader.close()
    payload = {}
    title = " ".join(reader.title).strip()
    if title:
        payload["window_title"] = title
    visible = " ".join(reader.text).strip()
    if visible:
        payload["visible_text"] = visible
    return payload


def _default_encoder(path):
    with open(path, "rb") as handle:
        return base64.b64encode(handle.read()).decode("ascii")


def select_capture(target, sources, *, prefer=()):
    """Return the first usable capture, structured sources first.

    ``prefer`` reorders *within* the structured set only. It cannot promote
    pixels past a structured source, because that would reintroduce exactly
    the cost and unreliability the ordering exists to avoid -- and it cannot
    promote anything at all past a class it does not serve.

    When nothing answers, the refusal names what was tried and, crucially,
    whether the screen tier was *excluded by class* or merely declined. Those
    are different operational problems: the first means the caller declared
    the wrong class, and the second means the machine genuinely had nothing.
    A declared class that nothing serves is a third fact again -- *nothing was
    tried* -- and an operator who configured a CLI source and asked for
    pixels has a wiring mistake rather than an observation failure, so the
    message says which of the three happened.
    """
    target = as_target(target)
    candidates = [s for s in sources if s.can_serve(target)]
    excluded = [s.name for s in sources if not s.can_serve(target)]

    structured_order = [s for s in SOURCE_ORDER if s != "screen"]
    structured_order.sort(
        key=lambda name: prefer.index(name) if name in prefer else len(prefer))
    tried = []

    for name in structured_order:
        for source in candidates:
            if source.name != name:
                continue
            capture = source.capture(target)
            tried.append((source.name, capture.detail or "declined"))
            if capture.ok:
                return capture

    for source in candidates:
        if source.name != "screen":
            continue
        capture = source.capture(target)
        tried.append((source.name, capture.detail or "declined"))
        if capture.ok:
            return capture

    attempted = ", ".join(f"{name}: {detail}" for name, detail in tried) or "none"
    declared_on = sorted({s.name for s in sources}) or ["none"]

    if target.declared and not candidates:
        # Nothing was even tried, because every configured source serves some
        # other class. "Every source declined" would be a lie about what
        # happened and would send an operator to inspect the wrong tier; the
        # fix is to configure the class they actually asked for.
        note = ""
        if excluded:
            note += (f"; excluded because they serve another class: "
                     f"{sorted(set(excluded))}")
        if "screen" in excluded:
            note += (f" The screen tier serves {VISION_CLASS!r} only, so it was "
                     f"therefore not reached.")
        raise PerceptionUnavailable(
            f"target {target.ref!r} is declared {target.target_class!r} and no "
            f"configured source serves that class; nothing was tried. "
            f"Configured sources: {declared_on}{note}.")

    if target.declared and target.structured and "screen" in excluded:
        # The loud, correct version: pixels were not merely unhelpful here,
        # they were unreachable. Saying so is what turns a confusing stop
        # into an obvious fix (declare the real class, or register a source
        # that serves it).
        raise PerceptionUnavailable(
            f"target {target.ref!r} is declared {target.target_class!r}, which "
            f"is answerable without pixels; the screen tier was therefore not "
            f"reached. Tried: {attempted}. Configured sources: {declared_on}.")

    raise PerceptionUnavailable(
        f"no source could observe the target {target.ref!r}; every source "
        f"either declined or failed. Tried: {attempted}. Configured "
        f"sources: {declared_on}")
