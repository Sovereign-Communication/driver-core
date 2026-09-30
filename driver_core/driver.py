"""The driver: one owner for the whole sequence.

This is the seam. Everything above it is a decision about *how* a step
proceeds; everything below it is a capability that knows how to do one
thing. :class:`Driver` is the only class that knows the order, and it is the
object a host project talks to -- which is what makes the integration
contract a single method with a plain-data result rather than a negotiation
between five subsystems.

The order is the design, so it is worth stating plainly:

1. **Capture** a structured source if one can answer; pixels only as a
   last resort.
2. **Extract** through N independent extractors.
3. **Tally** deterministically, and refuse anything that is not unanimous
   among those who answered.
4. **Gate** on the tally, before any state reaches a model.
5. **Decide** with Jev, over the declared action vocabulary only.
6. **Gate** on confidence and on the guards.
7. **Execute** with a model-free executor, under consent.
8. **Escalate** if any gate said no.

Each step's verdict is carried forward rather than recomputed, so a blocked
extraction can never be followed by a decision that assumed it succeeded --
the pipeline returns at the first refusal instead of continuing with a
half-built state.

A blocked step is a **normal, successful outcome**. ``StepResult.ok`` is
false and the result explains why; that is the system working, not failing,
and the audit log records it as a first-class event.
"""
import uuid

from . import policy
from .actions import DEFAULT_VOCABULARY
from .audit import AuditLog
from .budget import Budget
from .consensus import tally
from .config import Settings, default_audit_path, load_settings
from .errors import PerceptionUnavailable, VocabularyError
from .executor import Executor
from .executor_registry import build_read_only_registry
from .extractors import ExtractorPool
from .jev_client import JevClient
from .perception import select_capture
from .states import SCHEMAS_BY_TARGET


class StepResult:
    """Everything one step produced, in a form a host project can consume.

    Deliberately plain data. A caller on the far side of a process boundary
    needs to branch on *why* a step stopped, and the six reasons are distinct
    enough to matter: a shortfall and a disagreement and a low confidence all
    call for different responses from the code that owns the screen.
    """

    __slots__ = ("step_id", "ok", "stopped_at", "reason", "detail", "capture",
                 "agreement", "decision", "execution", "receipt", "cost")

    def __init__(self, step_id, *, ok, stopped_at, reason=None, detail="",
                 capture=None, agreement=None, decision=None, execution=None,
                 receipt=None, cost=0.0):
        self.step_id = step_id
        self.ok = ok
        self.stopped_at = stopped_at
        self.reason = reason
        self.detail = detail
        self.capture = capture
        self.agreement = agreement
        self.decision = decision
        self.execution = execution
        self.receipt = receipt
        self.cost = float(cost or 0.0)

    def to_dict(self):
        """The wire form -- the contract a host adapter serialises."""
        return {
            "step_id": self.step_id,
            "ok": self.ok,
            "stopped_at": self.stopped_at,
            "reason": self.reason,
            "detail": self.detail,
            "capture": self.capture.summary() if self.capture else None,
            "agreement": self.agreement.to_dict() if self.agreement else None,
            "decision": self.decision.to_dict() if self.decision else None,
            "execution": self.execution.to_dict() if self.execution else None,
            "receipt": self.receipt,
            "cost_usd": round(self.cost, 9),
        }

    def __repr__(self):
        return f"StepResult({self.step_id!r}, ok={self.ok}, at={self.stopped_at})"


class Driver:
    """The one owner of the pipeline.

    Collaborators are injected rather than constructed, which is what keeps
    the host integration honest: a caller supplies its own budget, its own
    audit log and its own executors, and cannot accidentally end up with two
    of any of them.
    """

    def __init__(self, *, settings=None, budget=None, audit=None,
                 vocabulary=DEFAULT_VOCABULARY, pool=None, jev=None,
                 executor=None, sources=(), screen=None):
        self.settings = settings or load_settings()
        self.budget = budget or Budget(self.settings.run_ceiling_usd,
                                       step_ceiling_usd=self.settings.step_ceiling_usd)
        self.audit = audit or AuditLog(
            self.settings.audit_path or default_audit_path())
        self.vocabulary = vocabulary
        self.pool = pool or ExtractorPool([])
        self.sources = list(sources)
        self.screen = screen

        self.jev = jev or JevClient(self.settings, budget=self.budget,
                                    audit=self.audit)
        self.executor = executor or Executor(
            vocabulary=vocabulary,
            registry=build_read_only_registry(),
            dry_run=self.settings.dry_run,
            audit=self.audit)

    def register_executor(self, name, handler):
        """Add one executor to this driver's closed registry."""
        self.executor.registry.register(name, handler)
        return self

    # -- the pipeline ----------------------------------------------------

    def step(self, target, *, schema=None, consent=None, step_id=None,
             prefer=(), require_stable=True):
        """Run one full step. Returns a :class:`StepResult`; never raises for
        a blocked or degraded condition."""
        step_id = step_id or uuid.uuid4().hex[:12]
        schema = schema or SCHEMAS_BY_TARGET.get("screen")
        if schema is None:
            raise PerceptionUnavailable("no schema was supplied and none could "
                                        "be inferred for this target")

        # 1. capture
        try:
            capture = self._capture(target, prefer)
        except PerceptionUnavailable as exc:
            return self._stop(step_id, "capture", "no_capture", str(exc))
        self.audit.append("capture", step_id=step_id, **capture.summary())

        # 2 + 3. extract and tally
        votes = self.pool.run(capture, schema)
        agreement = tally(votes, schema, quorum=self.settings.quorum,
                          min_agreement=self.settings.min_agreement)
        receipt = agreement.receipt(schema.identity(), step_id)
        self.audit.append("extraction", step_id=step_id, **receipt)

        # 4. gate before any model sees the state
        gate = policy.check_agreement(
            agreement, quorum=self.settings.quorum,
            min_agreement=self.settings.min_agreement)
        if not gate.passed:
            return self._stop(step_id, "agreement", gate.reason, gate.detail,
                              capture=capture, agreement=agreement,
                              receipt=receipt, cost=agreement.cost)

        # 5. decide
        decision = self.jev.decide(agreement.state, self.vocabulary,
                                   receipt=receipt, step_id=step_id)

        # 6. gate on the decision
        decision_gate = policy.check_decision(
            decision, threshold=self.settings.confidence_threshold,
            require_stable=require_stable)
        if not decision_gate.passed:
            self.audit.append("escalation", step_id=step_id,
                              reason=decision_gate.reason,
                              detail=decision_gate.detail)
            return self._stop(step_id, "decision", decision_gate.reason,
                              decision_gate.detail, capture=capture,
                              agreement=agreement, decision=decision,
                              receipt=receipt, cost=decision.cost)

        # 7. execute -- no model past this line
        #    A model naming an action the vocabulary does not declare is a
        #    refusal, not a crash. It is reachable in principle (a caller
        #    injecting a hand-built envelope, a validation rule drifting),
        #    and the correct response is the same one as any other blocked
        #    step: stop, name it, log it, hand it back.
        try:
            action = self.vocabulary.resolve(decision.recommended_action)
        except VocabularyError as exc:
            return self._stop(step_id, "execution", "undeclared_action", str(exc),
                              capture=capture, agreement=agreement,
                              decision=decision, receipt=receipt,
                              cost=decision.cost)
        try:
            execution = self.executor.execute(action.name, _params_for(action),
                                              consent=consent, step_id=step_id)
        except Exception as exc:
            return self._stop(step_id, "execution", "execution_refused", str(exc),
                              capture=capture, agreement=agreement,
                              decision=decision, receipt=receipt,
                              cost=decision.cost)

        return StepResult(step_id, ok=True, stopped_at="executed",
                          capture=capture, agreement=agreement,
                          decision=decision, execution=execution,
                          receipt=receipt,
                          cost=agreement.cost + decision.cost)

    def _capture(self, target, prefer):
        sources = list(self.sources)
        if self.screen is not None:
            sources.append(self.screen)
        return select_capture(target, sources, prefer=prefer)

    def _stop(self, step_id, stopped_at, reason, detail, **kwargs):
        self.audit.append("refusal", step_id=step_id, stopped_at=stopped_at,
                          reason=reason, detail=detail)
        return StepResult(step_id, ok=False, stopped_at=stopped_at,
                          reason=reason, detail=detail, **kwargs)


def _params_for(action):
    """Default parameters for a no-argument action.

    The decision tier names an action; it does not supply arguments, because
    a model inventing a path or a URL is precisely the failure this design
    exists to prevent. Actions that genuinely need arguments are executed by
    a host-supplied executor that knows its own parameters.
    """
    return {}


__all__ = ["Driver", "StepResult", "Settings", "load_settings", "Budget",
           "AuditLog"]
