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
7. **Execute** with a model-free executor, under consent, with parameters
   that came from the caller and not from the model.
8. **Escalate** if any gate said no.

The provenance of the *parameters* is as load-bearing as the provenance of
the decision. The model is allowed to name an action out of a closed
vocabulary; it is never allowed to supply a path, a URL or a string to type,
because those are the things a model will confidently invent. They arrive
from the caller -- the same place consent arrives from -- which is what lets
an operator be shown the action *and* its parameters as a single pair and
agree to exactly that.

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
from .executor_registry import build_driver_registry
from .extractors import ExtractorPool
from .jev_client import JevClient
from .perception import select_capture
from .states import SCHEMAS_BY_TARGET
from .wiring import configured_pools, configured_sources


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
                 executor=None, sources=None, screen=None, pools=None):
        self.settings = settings or load_settings()
        self.budget = budget or Budget(self.settings.run_ceiling_usd,
                                       step_ceiling_usd=self.settings.step_ceiling_usd)
        self.audit = audit or AuditLog(
            self.settings.audit_path or default_audit_path())
        self.vocabulary = vocabulary

        # ``None`` means "use what the settings declare"; ``()`` means
        # "explicitly none". Both spellings are load-bearing: a library caller
        # that wants a driver which cannot observe anything should not have to
        # unset five settings to get one, and a caller that declares a CLI
        # command should not have to re-derive the source in Python.
        if sources is None or screen is None or pools is None:
            declared_sources = configured_sources(self.settings)
            declared_pools = configured_pools(self.settings,
                                              budget=self.budget,
                                              audit=self.audit)
        else:
            declared_sources, declared_pools = [], {}

        #: The fallback pool, used for a target whose class is undeclared.
        self.pool = pool or ExtractorPool([])
        #: Pools keyed by declared target class. This is where "three of the
        #: four classes never reach the vision tier" is enforced on the
        #: extraction side: a class is only ever handed the pool that
        #: declared it, so a vision pool is not a candidate for a ``dom``
        #: target rather than merely being discouraged.
        self.pools = {**declared_pools, **(pools or {})}
        self.sources = list(declared_sources if sources is None else sources)
        if screen is None:
            # The declared set already carries a screen source when the
            # operator asked for the vision tier, so this picks out *that*
            # object rather than building a second one. Two screen sources
            # would mean two captures of the same pixels on every step, and
            # ``/health`` would report the tier twice.
            screen = next((s for s in self.sources if s.name == "screen"), None)
        self.screen = screen

        self.jev = jev or JevClient(self.settings, budget=self.budget,
                                    audit=self.audit)
        # Executors with a side effect are registered only when the operator
        # has declared this machine may be written to. A caller that builds
        # a Driver and passes no settings gets an observer.
        self.executor = executor or Executor(
            vocabulary=vocabulary,
            registry=build_driver_registry(
                allow_write=self.settings.allow_write),
            dry_run=self.settings.dry_run,
            audit=self.audit)

    def register_executor(self, name, handler):
        """Add one executor to this driver's closed registry."""
        self.executor.registry.register(name, handler)
        return self

    # -- the pipeline ----------------------------------------------------

    def step(self, target, *, schema=None, consent=None, step_id=None,
             prefer=(), require_stable=True, params=None):
        """Run one full step. Returns a :class:`StepResult`; never raises for
        a blocked or degraded condition.

        ``params`` are the action's parameters, and they come from the
        *caller* -- never from the decision tier. That provenance is the
        point: a model naming an action is safe because the vocabulary is
        closed, but a model supplying a path or a URL is the exact failure
        this design exists to prevent. Keeping the parameters on the
        caller's side of the line is also what lets them be shown to an
        operator and consented to as a single ``(action, params)`` pair.
        """
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
        votes = self._pool_for(capture).run(capture, schema)
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
            execution = self.executor.execute(
                action.name, _params_for(action, params),
                consent=consent, step_id=step_id)
        except Exception as exc:
            return self._stop(step_id, "execution", "execution_refused", str(exc),
                              capture=capture, agreement=agreement,
                              decision=decision, receipt=receipt,
                              cost=decision.cost)

        # An executor that refused did not do the thing. The pipeline
        # reaching the end is not the same as the action having happened,
        # and a host that branches on ``ok`` must never be told a click
        # occurred when no backend was registered to perform it.
        #
        # This is the same refusal, and the same already-declared reason, as
        # an executor that raised -- just reported at the layer the caller
        # actually reads. The execution record still travels with the result
        # so the failure is inspectable rather than merely announced.
        if not execution.ok:
            return self._stop(step_id, "execution", "execution_refused",
                              execution.detail, capture=capture,
                              agreement=agreement, decision=decision,
                              execution=execution, receipt=receipt,
                              cost=decision.cost)

        return StepResult(step_id, ok=True, stopped_at="executed",
                          capture=capture, agreement=agreement,
                          decision=decision, execution=execution,
                          receipt=receipt,
                          cost=agreement.cost + decision.cost)

    def observation_sources(self):
        """Every source this driver can consult, in tier order.

        ``self.sources`` and ``self.screen`` are two handles onto one ordered
        set, and this is the single place they are merged. Merging them
        twice is how a step ends up capturing the same screen twice and
        ``/health`` ends up reporting the vision tier twice, so every caller
        that needs the full chain reads it from here.
        """
        sources = list(self.sources)
        if self.screen is not None and not any(s is self.screen
                                               for s in sources):
            sources.append(self.screen)
        return sources

    def _capture(self, target, prefer):
        """One capture, through the ordered tier chain.

        ``sources`` and ``screen`` are one list on purpose. They used to be
        two arguments, with the screen as a special case bolted on beside the
        structured sources, and that shape is what let a screen capture be
        treated as a peer of a CLI probe rather than as the last resort it
        is. One ordered list, filtered by target class, is the guarantee.
        """
        return select_capture(target, self.observation_sources(),
                              prefer=prefer)

    def _pool_for(self, capture):
        """The extractor pool for this capture's target class.

        Falls back to the undeclared-class pool. Refusing here rather than
        downstream means the "no vision for a structured class" rule holds
        even when a caller wires an unusually generous pool map.
        """
        target_class = getattr(capture, "target_class", None)
        if target_class is not None and target_class in self.pools:
            return self.pools[target_class]
        return self.pool

    def _stop(self, step_id, stopped_at, reason, detail, **kwargs):
        self.audit.append("refusal", step_id=step_id, stopped_at=stopped_at,
                          reason=reason, detail=detail)
        return StepResult(step_id, ok=False, stopped_at=stopped_at,
                          reason=reason, detail=detail, **kwargs)


def _params_for(action, supplied):
    """The parameters for one action, taken from the caller and never the model.

    The decision tier names an action; it does not supply arguments, because
    a model inventing a path or a URL is precisely the failure this design
    exists to prevent. They arrive from the caller instead -- the same place
    the consent arrives from, which is what makes them one thing to be shown
    and agreed to rather than two that can drift apart.

    An action that declares no parameters and is handed some refuses rather
    than ignoring them. A parameter nobody declared is the same class of
    problem as an undeclared action: something outside this contract is
    trying to influence execution, and quietly discarding it would hide that.
    """
    supplied = dict(supplied or {})
    if not action.params:
        if supplied:
            raise VocabularyError(
                f"action {action.name!r} takes no parameters, but "
                f"{sorted(supplied)} were supplied")
        return {}
    return action.check_params(supplied)


__all__ = ["Driver", "StepResult", "Settings", "load_settings", "Budget",
           "AuditLog"]
