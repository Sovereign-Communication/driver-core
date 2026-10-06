"""The extraction tier: N independent extractors over one capture.

Independence is the only thing this module is for. The consensus tally can
only say "two or more independent observers saw the same thing" if the
observers really are independent, so the rules below are structural rather
than advisory:

* **An extractor never sees another's answer.** Each is handed the same
  capture and the same schema and nothing else. There is no shared
  conversation, no seed answer, no "the previous extractor said" hint. A
  second model told what the first one said is a rubber stamp, not a second
  observation.
* **Failures are isolated.** One extractor dying must not affect the others;
  a pool that stops at the first error is not a pool.
* **Cost is recorded per slot**, including for slots that failed after the
  provider billed them.

Two kinds of extractor exist, and the distinction is the single biggest
lever on cost and reliability:

:class:`StructuredExtractor` is deterministic and free. It handles CLI
output, DOM/accessibility trees and MCP results, where the "state" is
already structured. Three of the four target classes need no vision at all,
and this is the class that makes them work.

:class:`VisionExtractor` spends money and can be wrong. It exists for native
GUI pixels, where there is no structured source to read. It reaches the
model through the same client primitives the decision tier uses -- one
endpoint, one key, one budget, one price list -- and it is the only extractor
whose spend reaches the run ceiling.
"""
import json

from . import osal
from . import transport
from .audit import KIND_EXTRACTION
from .budget import UNAVAILABLE, estimate_call_cost
from .config import JEV_INPUT_PRICE_PER_MILLION
from .consensus import Vote
from .errors import SchemaError
from .jev_client import SYSTEM_ONE_URL
from .perception import VISION_CLASS
from .schema import OPTIONAL, validate_state




class Extraction:
    """What one extractor observed."""

    __slots__ = ("state", "reason", "cost", "usage_source", "model")

    def __init__(self, state=None, reason="", cost=0.0,
                 usage_source=UNAVAILABLE, model=None):
        self.state = state
        self.reason = reason
        self.cost = float(cost or 0.0)
        self.usage_source = usage_source
        self.model = model

    @property
    def ok(self):
        return self.state is not None

    def __repr__(self):
        return f"Extraction(ok={self.ok}, reason={self.reason!r})"


class StructuredExtractor:
    """Deterministic extraction from an already-structured capture.

    No model, no network, no cost, no variance. Given the same capture it
    always produces the same observation, which means a disagreement between
    two structured extractors is a bug in the reader rather than a genuine
    difference of opinion -- and is worth surfacing loudly.

    This is the class that makes the three structured target classes work at
    all. It is free, so a run that never leaves this tier never spends
    anything, which is the practical payoff of the CLI -> MCP -> DOM ordering
    rather than an aesthetic argument about it.
    """

    deterministic = True

    #: Takes the capture; paired with ``VisionExtractor.uses_capture``.
    uses_capture = True

    def __init__(self, slot, reader):
        self.slot = slot
        self._reader = reader

    def extract(self, capture, schema):
        try:
            raw = self._reader(capture, schema)
        except Exception as exc:
            return Extraction(reason=f"reader failed: {exc}")
        if not isinstance(raw, dict):
            return Extraction(reason="reader did not return a mapping")
        # Validate here rather than deferring: a structured reader that emits
        # an undeclared key is misconfigured, and letting that through would
        # turn a wiring mistake into a consensus disagreement.
        try:
            state = validate_state(raw, schema)
        except SchemaError as exc:
            return Extraction(reason=str(exc))
        return Extraction(state=state, usage_source="actual", cost=0.0)


class VisionExtractor:
    """Vision extraction: one model, one opinion, charged honestly.

    The last resort, and the only extractor here that costs money or can be
    wrong in a way code cannot check. Three of the four target classes never
    reach it, and that is enforced by which pool a target selects rather than
    by this class refusing to help -- see :meth:`ExtractorPool.for_target`.

    **It goes through the same client primitives as the decision tier.** The
    same :class:`~driver_core.config.Settings`, the same
    :data:`~driver_core.jev_client.SYSTEM_ONE_URL`, the same
    :class:`~driver_core.budget.Budget`, the same
    :class:`~driver_core.audit.AuditLog`, the same
    :mod:`driver_core.transport`, and the same
    :data:`~driver_core.config.JEV_INPUT_PRICE_PER_MILLION`. There is no
    second provider, no second key, and no second price list.

    That consolidation is the point, not tidiness. The previous version took
    its own ``endpoint`` and ``api_key``, which meant extraction could be
    pointed at a different model than the decision tier, and it charged
    nothing at all -- so a vision pool could spend straight past a run
    ceiling that had been sized for the decision tier alone. Extraction is a
    pool, so that was N unaccounted calls per step.

    Runs in its own try/except so a provider that hangs, rate-limits, or
    returns a refusal becomes one failed slot rather than a failed round --
    and still settles its reservation, because a failed call is possibly a
    billed one.
    """

    deterministic = False

    #: Reads the window through ``osal``, so it has no use for a capture and
    #: does not accept one -- a capture is not evidence it can use.
    uses_capture = False

    #: The target class this extractor can answer. Declared rather than
    #: inferred, so a pool can be asked which class it serves without
    #: inspecting the objects in it.
    serves = (VISION_CLASS,)

    def __init__(self, slot, settings, *, budget, audit,
                 transport_module=None, timeout=90, describer=None):
        self.slot = slot
        self.settings = settings
        self.budget = budget
        self.audit = audit
        self._transport = transport_module or transport
        self.timeout = timeout
        # The same injectable-seam rule the rest of the package uses: a test
        # supplies a description so the suite never reads a real foreground
        # window, and two slots stay independent of whatever is on screen.
        self._describer = describer or osal.describe_screen

    def extract(self, schema):
        """Read the screen, judge it, and return the agreed-typed state."""
        if not self.settings.keyed:
            # No call, no reservation, no charge. An unkeyed vision slot is
            # "unavailable", never "answered nothing".
            return self._record(Extraction(reason="no key for vision "
                                                   "extraction"))

        # The screen is described before anything is reserved, so a capture
        # that cannot be described is a refusal rather than a billed call.
        described, why = self._describer()
        if not described:
            return self._record(Extraction(
                reason=f"screen description unavailable: {why}"))

        questions = _extraction_questions(schema)
        estimate = _estimate_capture_tokens(described, questions)
        reserve_usd = estimate_call_cost(
            estimate, price_per_million=JEV_INPUT_PRICE_PER_MILLION)

        try:
            reservation = self.budget.reserve(reserve_usd, label=self.slot)
        except Exception as exc:
            # Refused before dispatch, so nothing was spent.
            return self._record(Extraction(reason=f"budget refused: {exc}"))

        try:
            response = self._transport.call_service(
                SYSTEM_ONE_URL, questions,
                headers={"Authorization":
                         f"Bearer {self.settings.jev_api_key}"},
                timeout=self.timeout,
                body_extra={"state": _capture_state(described),
                            "model": self.settings.jev_model})
        except Exception as exc:
            charged = reservation.settle(None)
            return self._record(Extraction(
                reason=f"transport raised: {exc}", cost=charged,
                model=self.settings.jev_model))

        usage = response.usage() or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        # A failed call is still possibly a billed call. Settling at zero here
        # is how a vision loop quietly runs up a bill nobody can account for.
        if response.ok and usage and input_tokens:
            charged = reservation.settle(
                estimate_call_cost(
                    input_tokens, price_per_million=JEV_INPUT_PRICE_PER_MILLION),
                source="actual")
            usage_source = "actual"
        elif response.ok and usage:
            charged = reservation.settle(
                reserve_usd, source="estimated")
            usage_source = "estimated"
        else:
            charged = reservation.settle(None)
            usage_source = UNAVAILABLE

        if not response.ok:
            return self._record(Extraction(
                reason=f"{response.outcome}: {response.detail}", cost=charged,
                usage_source=usage_source, model=self.settings.jev_model))

        state = _observed_state(response.payload, schema, described)
        if state is None:
            return self._record(Extraction(
                reason="model returned no parseable state", cost=charged,
                usage_source=usage_source, model=self.settings.jev_model))
        return self._record(Extraction(
            state=state, cost=charged, usage_source=usage_source,
            model=self.settings.jev_model))

    def _record(self, extraction):
        """Write one metadata-only audit record. Never the state, never the
        image.

        The fields below are an explicit allowlist rather than a filtered
        copy of something else, so a field added to :class:`Extraction`
        later cannot leak into the log by default. The pixels went in; the
        validated fields came out; nothing in between is written down.
        """
        if self.audit is not None:
            self.audit.append(
                KIND_EXTRACTION,
                step_id=self.slot,
                tier="vision",
                ok=extraction.ok,
                reason=extraction.reason,
                model=extraction.model,
                cost_usd=round(extraction.cost, 9),
                usage_source=extraction.usage_source,
            )
        return extraction


#: Schema types a decision model can answer in a closed vocabulary. A
#: ``choice`` returns one of the options *code* declared, so it can express a
#: yes/no and a closed set of labels, and nothing else. Everything else in a
#: schema is a value the platform already holds exactly, and is read by code.
JUDGMENT_TYPES = ("boolean", "enum")

#: The label an enum question answers with when the field is simply not on
#: screen. Absent is a real observation and has to be nameable, or the model
#: is forced to invent a label to say "nothing".
ABSENT = "<absent>"


def _extraction_questions(schema):
    """One typed question per field the model can actually answer.

    The previous pack asked a single ``observed``/``unreadable`` question while
    the parser looked for ``state``/``fields`` that **no question asked for**,
    so the parse could not succeed even against a perfectly readable capture.
    Questions are now generated from the same schema the state is validated
    against: every question has an answer the parser knows how to read, and a
    field added to a schema is asked for automatically.

    Free-text fields are deliberately absent. A ``choice`` cannot return a
    window title, and a model asked to invent one would be fabricating a
    fact the platform already knows exactly -- so those are read by code and
    only the bounded judgments are asked of the model.
    """
    asked = [f for f in schema.fields if f.type in JUDGMENT_TYPES]
    listed = "\n".join(
        f"- {f.name} ({f.type}"
        + (f"/{f.presence}" if f.presence != "required" else "")
        + f"): {f.description}" for f in asked)
    questions = {
        "observation": transport.choice(
            "You are given a plain-text description of the screen observed "
            "on this machine, read from the window manager. Judge that "
            "description only. Do not guess at anything it does not state.\n"
            "ASKED FIELDS:\n" + (listed or "- (none)"),
            {"observed": "The description is a real observation of a screen.",
             "unreadable": "The description is empty, absent, or not a "
                           "description of an observed screen."}),
    }
    for field in asked:
        if field.type == "boolean":
            questions[field.name] = transport.noul(
                f"According to the description, and only according to it: "
                f"{field.description}",
                true="The description supports this being true.",
                false="The description does not support this being true.")
        else:
            options = {ABSENT: "The description does not say. Leave it out."}
            options.update({str(v): str(v) for v in field.values})
            questions[field.name] = transport.choice(
                f"According to the description, and only according to it: "
                f"{field.description}",
                options)
    return questions


def _observed_state(payload, schema, described):
    """Read a state out of a model response plus code-read facts.

    ``described`` carries the values the platform reported exactly. They are
    merged in *after* the model's bounded judgments rather than being asked
    for, because the model cannot be the source of a fact it would have to
    invent. A response that does not declare the capture observed is believed
    and yields nothing; a state that fails validation is a malformed slot, not
    one to be repaired.
    """
    if not isinstance(payload, dict):
        return None
    answers = payload.get("answers") or {}
    choice = answers.get("observation") or {}
    if choice.get("choice") != "observed":
        return None

    state = {name: value for name, value in (described or {}).items()
             if name in schema.field_names()}
    for field in schema.fields:
        if field.type not in JUDGMENT_TYPES:
            continue
        answer = answers.get(field.name)
        if not isinstance(answer, dict):
            continue
        if field.type == "boolean":
            try:
                value = float(answer.get("noul"))
            except (TypeError, ValueError):
                continue
            if not 0.0 <= value <= 1.0:
                continue
            # A noul is a probability, and an optional field whose
            # probability sits on the fence is an absent field rather than a
            # coin flip nobody observed.
            if field.presence == OPTIONAL and 0.4 <= value <= 0.6:
                continue
            state[field.name] = value >= 0.5
        else:
            picked = answer.get("choice")
            if picked in (None, ABSENT) or picked not in field.values:
                continue
            state[field.name] = picked

    try:
        return validate_state(state, schema)
    except SchemaError:
        return None


def _estimate_capture_tokens(described, questions):
    """A pre-flight upper bound, using the decision tier's own approximation.

    Serialised size over four, erring high on purpose: a reservation that is
    too large costs a refusal, while one that is too small under-bills.
    """
    material = len(json.dumps(_capture_state(described), default=str).encode(
        "utf-8"))
    material += len(json.dumps(questions, default=str).encode("utf-8"))
    return max(1, material // 4 + 1)


def build_vision_pool(settings, *, budget, audit, slots=1,
                      transport_module=None, timeout=90, describer=None):
    """A pool of independent vision slots, for the ``gui`` target class.

    Built here rather than by the caller so that the vision pool is
    constructed the same way every time: same endpoint, same budget, same
    audit log as the decision tier. A caller who wants two independent
    opinions asks for two slots; they do not assemble their own clients.
    """
    names = (f"vision-{i}" for i in range(max(1, int(slots))))
    return ExtractorPool(
        [VisionExtractor(name, settings, budget=budget, audit=audit,
                         transport_module=transport_module, timeout=timeout,
                         describer=describer)
         for name in names],
        serves=(VISION_CLASS,))





def _capture_state(described):
    """The payload the model is shown: words, never a path.

    The endpoint is text-only -- a base64 image arrives as literal characters,
    not as pixels -- so the screen is described by the window manager instead,
    which holds the title exactly rather than as a transcription of a picture.

    **Whatever :func:`driver_core.osal.describe_screen` returns is what this
    carries.** There is deliberately no field list here. An earlier version
    named the keys it expected, making this and ``osal`` two owners of one
    fact: a key added to the reader alone was dropped, silently.
    """
    lines = [f"{key.replace('_', ' ')}: {value}"
             for key, value in (described or {}).items() if value]
    return {"description": "; ".join(lines) if lines else "no window described"}





class ExtractorPool:
    """Runs every extractor against one capture and collects votes.

    Sequential rather than parallel on purpose for the first version: the
    number of extractors is small, the cost is already the dominant cost, and
    a serial pool produces a deterministic ordering of records for the audit
    log, which is worth more here than the wall-clock saving.

    A pool declares the target classes it serves, and
    :meth:`refuses_class` is the structural half of "three of the four target
    classes never reach the vision tier": a vision pool handed a ``dom``
    capture returns no votes rather than billing for an opinion nobody asked
    for.
    """

    def __init__(self, extractors, *, serves=()):
        self.extractors = list(extractors)
        self.serves = tuple(serves)

    @property
    def size(self):
        return len(self.extractors)

    def refuses_class(self, target_class):
        """Whether this pool declines to answer ``target_class``.

        ``None`` -- an undeclared target -- is always answered. A pool that
        declares nothing is a pool that serves everything, which is the
        historical behaviour and the reason the declaration is optional.
        """
        if not self.serves:
            return False
        return target_class is not None and target_class not in self.serves

    def for_target(self, target):
        """This pool, or ``None`` if it does not serve the target's class."""
        target_class = getattr(target, "target_class", None)
        return None if self.refuses_class(target_class) else self

    @property
    def reads_captures(self):
        """Whether anything in this pool will read a capture payload.

        A vision pool answers no: its extractors read the window through
        the operating system and declare ``uses_capture = False``. That
        declaration is what lets a screen capture be skipped instead of
        performed, written to disk, base64-encoded into memory and
        fingerprinted for a payload nothing looks at -- which also removes
        the unrelated environment variable that used to be a precondition
        of the vision tier working at all.

        An empty pool answers yes. It has no extractors to read anything,
        but it is the fallback for an undeclared target class, and reading
        its silence as "pixels are not needed" would quietly change what
        an existing caller observes.
        """
        if not self.extractors:
            return True
        return any(getattr(e, "uses_capture", True)
                   for e in self.extractors)

    def run(self, capture, schema):
        """Return one Vote per extractor. Never raises for a slot failure."""
        votes = []
        for extractor in self.extractors:
            slot = getattr(extractor, "slot", f"slot-{len(votes)}")
            try:
                result = (extractor.extract(capture, schema)
                          if extractor.uses_capture
                          else extractor.extract(schema))
            except Exception as exc:  # isolation is the point
                votes.append(Vote.error(slot, f"extractor raised: {exc}"))
                continue
            if result.ok:
                votes.append(Vote.ok(slot, result.state, cost=result.cost,
                                     usage_source=result.usage_source))
            else:
                # A model that answered but produced nothing usable is
                # malformed, not merely unavailable: it occupied a slot and
                # spent money, and the tally must reflect that.
                votes.append(Vote.malformed(slot, result.reason or "no state",
                                            cost=result.cost,
                                            usage_source=result.usage_source))
        return votes
