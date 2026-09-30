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

Every capture is also **opaque and short-lived**. A path, a handle, a base64
blob -- whichever the source produces -- is passed forward and never copied
into an audit record, because a log of screen captures is a log of everything
the operator has looked at.
"""
import base64
import hashlib

from . import osal
from .errors import PerceptionUnavailable

#: Preference order. Named, not incidental.
SOURCE_ORDER = ("cli", "mcp", "dom", "screen")


class Capture:
    """One observation of the target.

    ``fingerprint`` is a short digest of the bytes observed. It exists so two
    steps can be proven to have looked at the *same* screen without storing
    the screen -- which is how a re-plan after an extraction disagreement can
    tell "the window changed under us" from "the extractors just disagreed
    about an unchanged window".
    """

    __slots__ = ("source", "target", "payload", "detail", "fingerprint")

    def __init__(self, source, target, payload=None, detail="", fingerprint=""):
        self.source = source
        self.target = target
        self.payload = payload
        self.detail = detail
        self.fingerprint = fingerprint

    @property
    def ok(self):
        return self.payload is not None

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

    ``reader`` is a callable ``(target) -> payload | None``. It is injected,
    so the same tier can serve a live subprocess, a fixture in a test, or a
    stub standing in for a real MCP client, with no branching here.
    """

    def __init__(self, name, reader, *, cost_usd=0.0):
        if name not in SOURCE_ORDER:
            raise PerceptionUnavailable(
                f"unknown structured source {name!r}; declared sources are "
                f"{list(SOURCE_ORDER)}")
        self.name = name
        self._reader = reader
        self.cost = float(cost_usd)

    def capture(self, target):
        try:
            payload = self._reader(target)
        except Exception as exc:
            return Capture(self.name, target, None,
                           detail=f"reader failed: {exc}")
        if payload is None:
            return Capture(self.name, target, None, detail="source had nothing")
        return Capture(self.name, target, payload,
                       fingerprint=fingerprint(payload))


class ScreenSource:
    """The last resort: pixels.

    Registration is explicit and failure is named. A driver that cannot
    capture returns a reason, and the caller routes to a structured source or
    stops -- it never hands a vision model a path that does not exist.
    """

    def __init__(self, *, encoder=None):
        self.name = "screen"
        self._encoder = encoder or _default_encoder

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


def _default_encoder(path):
    with open(path, "rb") as handle:
        return base64.b64encode(handle.read()).decode("ascii")


def select_capture(target, sources, *, prefer=()):
    """Return the first usable capture, structured sources first.

    ``prefer`` reorders *within* the structured set only. It cannot promote
    pixels past a structured source, because that would reintroduce exactly
    the cost and unreliability the ordering exists to avoid.
    """
    ordered = [s for s in SOURCE_ORDER if s != "screen"]
    ordered.sort(key=lambda name: prefer.index(name) if name in prefer else
                 len(prefer))
    for name in ordered:
        for source in sources:
            if source.name != name:
                continue
            capture = source.capture(target)
            if capture.ok:
                return capture
    for source in sources:
        if source.name == "screen":
            capture = source.capture(target)
            if capture.ok:
                return capture
    raise PerceptionUnavailable(
        f"no source could observe the target {target!r}; every source either "
        f"declined or failed")
