"""The action tier: deterministic executors, and no model anywhere in it.

Everything above this module decides. This module does. That separation is
the whole reason a System One model is safe to put in a loop that touches a
real machine: the model names an action from a closed vocabulary, and the
code that carries it out contains no model, no interpretation, and no
judgement about what the parameters should have been.

Three properties make that safe enough to run unattended:

**Executors are named, declared, and closed.** ``observe``, ``no_action`` and
``read_value`` are built in. Anything touching the outside world --
clicking, typing, writing, deleting -- must be registered explicitly, and an
unregistered executor name is a refusal, not a fallback.

**Consent is checked per action class, and re-checked.** A mutating action
needs current consent. An irreversible action needs a *fresh* human
confirmation, every time, and is never eligible to ride along inside a batch
with anything else -- otherwise a single "yes" launders three consequences
past a person who was only asked about one.

**Dry run is the default shape, not a flag to remember.** Every execution
returns a record of what it *would* have done, and a run configured for dry
mode performs no side effect while still producing the identical record, so
the dry run is a real rehearsal rather than a different code path that
happens to skip the interesting part.
"""
from .actions import IRREVERSIBLE, READ_ONLY
from .errors import ConsentError, ExecutorError


class ExecutionResult:
    """The record of one action, produced whether or not it had an effect."""

    __slots__ = ("action", "action_class", "params", "ok", "detail", "output",
                 "dry_run", "cost")

    def __init__(self, action, action_class, params, ok, detail="", output=None,
                 dry_run=False, cost=0.0):
        self.action = action
        self.action_class = action_class
        self.params = params
        self.ok = ok
        self.detail = detail
        self.output = output
        self.dry_run = dry_run
        self.cost = float(cost or 0.0)

    def to_dict(self):
        return {
            "action": self.action,
            "class": self.action_class,
            "params": self.params,
            "ok": self.ok,
            "detail": self.detail,
            "output": self.output,
            "dry_run": self.dry_run,
            "cost_usd": round(self.cost, 9),
        }

    def __repr__(self):
        return f"ExecutionResult({self.action!r}, ok={self.ok})"


class Consent:
    """A record that a human agreed to a specific, named thing.

    Consent is bound to what was actually proposed, not to a general
    permission. A confirmation captured for ``click`` does not authorise
    ``delete_file``; one captured for ``delete_file`` on one path does not
    authorise it on another. :attr:`params` is the parameter set the person
    was shown, and an irreversible action whose current parameters do not
    match those requires a fresh confirmation rather than proceeding on the
    strength of a different one.
    """

    __slots__ = ("granted", "action", "params", "by")

    def __init__(self, granted, action="*", params=None, by="operator"):
        self.granted = bool(granted)
        self.action = action
        self.params = dict(params or {})
        self.by = by

    def covers(self, action_name, action_class, params):
        if action_class == READ_ONLY:
            return True
        if not self.granted:
            return False
        # A blanket grant is honoured for mutating work (one "go ahead" for
        # a run of clicks) but never for anything irreversible.
        if action_class == IRREVERSIBLE:
            if self.action not in ("*", action_name):
                return False
            return self.params == dict(params or {})
        return True

    def to_dict(self):
        return {"granted": self.granted, "action": self.action,
                "params": self.params, "by": self.by}

    def __repr__(self):
        return f"Consent(granted={self.granted}, action={self.action!r})"


#: Consent for read-only work; used as the default so a caller that only
#: observes never has to construct one.
NO_CONSENT_NEEDED = Consent(True, "*", by="system")


class Executor:
    """The model-free action tier."""

    def __init__(self, *, vocabulary, registry, dry_run=False, audit=None,
                 clock=None):
        self.vocabulary = vocabulary
        self.registry = registry
        self.dry_run = bool(dry_run)
        self.audit = audit
        self._clock = clock

    def resolve(self, action_name, params):
        """Validate an action name and its parameters against the vocabulary.

        Both halves raise rather than default. This is the last point at
        which an undeclared name can be caught, and a name that reaches an
        executor unvalidated is a name the rest of the system has no reason
        to distrust.
        """
        action = self.vocabulary.resolve(action_name)
        checked = action.check_params(params or {})
        return action, checked

    def check_consent(self, action, params, consent):
        if action.action_class == READ_ONLY:
            return True
        if consent is None:
            raise ConsentError(
                f"action {action.name!r} is {action.action_class} and no "
                f"consent was supplied")
        if not consent.covers(action.name, action.action_class, params):
            raise ConsentError(
                f"consent (granted for {consent.action!r} "
                f"{consent.params}) does not authorise {action.name!r} with "
                f"params {params}; re-confirmation is required")
        return True

    def execute(self, action_name, params=None, *, consent=NO_CONSENT_NEEDED,
                step_id="step"):
        """Perform one declared action, or explain why it was not performed."""
        action, checked = self.resolve(action_name, params or {})
        self.check_consent(action, checked, consent)

        handler = self.registry.get(action.executor)
        if handler is None:
            # An action that declares an executor nobody registered is a
            # wiring fault. It is refused, loudly, rather than skipped --
            # a silent skip would look like a successful no-op.
            raise ExecutorError(
                f"action {action.name!r} names executor {action.executor!r}, "
                f"which is not registered; registered: "
                f"{sorted(self.registry.names())}")

        if self.dry_run:
            result = ExecutionResult(
                action.name, action.action_class, checked, True,
                detail="dry run: no side effect was performed", output=None,
                dry_run=True)
        else:
            result = self._invoke(handler, action, checked)

        if self.audit is not None:
            self.audit.append(
                "action", step_id=step_id, action=action.name,
                action_class=action.action_class, ok=result.ok,
                detail=result.detail, dry_run=result.dry_run,
                consent=consent.to_dict() if consent else None)
        return result

    def _invoke(self, handler, action, params):
        try:
            output = handler(action, params)
        except Exception as exc:
            return ExecutionResult(action.name, action.action_class, params,
                                   False, detail=f"executor failed: {exc}")
        return ExecutionResult(action.name, action.action_class, params, True,
                               output=output)
