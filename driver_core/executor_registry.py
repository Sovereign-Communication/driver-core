"""The closed registry of action executors.

An executor is the code that actually does a thing. The registry is
deliberately closed: a name that is not registered has no handler, and
:meth:`ExecutorRegistry.get` returns ``None`` rather than a default. That
makes "this action has no implementation" an explicit, visible condition at
the point of use instead of a silent no-op that looks exactly like success.

The read-only executors are built in because observing is always safe and
should never require a deployment step to turn on. Everything that touches
the outside world is registered by the embedding application, which is the
right place for it: the choice of what this machine is allowed to touch is a
policy decision, not a library default.

OS contact lives in exactly one module for the same reason the sibling
project enforces it: a place that reaches for ``subprocess`` in one file and
``os`` in another grows a different idea of what a subprocess is on each
platform, and the divergence only shows up on the platform you did not test
on. :mod:`driver_core.osal` is that module, and the boundary test holds
everything else out of it.
"""
from .errors import ExecutorError


class ExecutorRegistry:
    """A closed name -> handler map."""

    def __init__(self, handlers=None):
        self._handlers = dict(handlers or {})

    def register(self, name, handler):
        if not callable(handler):
            raise ExecutorError(f"executor {name!r} is not callable")
        if name in self._handlers:
            raise ExecutorError(f"executor {name!r} is already registered")
        self._handlers[name] = handler
        return self

    def get(self, name):
        """The handler, or ``None``. There is intentionally no default."""
        return self._handlers.get(name)

    def names(self):
        return tuple(sorted(self._handlers))

    def __contains__(self, name):
        return name in self._handlers

    def __len__(self):
        return len(self._handlers)

    def clone(self):
        return ExecutorRegistry(self._handlers)


def _noop(action, params):
    """`no_action`: the declared way to say "nothing is required here".

    Returning a real result rather than doing nothing is intentional. "The
    driver decided no action was needed" is an outcome worth recording, and
    it is distinguishable in the log from the driver never having run.
    """
    return {"action": action.name, "note": "no action required"}


def _read_value(action, params, state=None):
    """`read_value`: pull one field out of the current verified state."""
    if state is None:
        raise ExecutorError(
            "read_value needs a verified state; it cannot read from nothing")
    field = params.get("field")
    if field not in state:
        raise ExecutorError(f"field {field!r} is not in the current state")
    return {"field": field, "value": state[field]}


def _observe(action, params, state=None):
    """`observe`: report that a capture happened and what it yielded."""
    return {
        "action": action.name,
        "fields": sorted(state or {}),
        "observed": state is not None,
    }


def build_read_only_registry(state_provider=None):
    """The registry containing only the safe built-ins.

    ``state_provider`` is a zero-argument callable returning the current
    verified state, which is how the built-in readers see the extraction
    without the executor tier holding a copy of it.
    """
    def _state():
        return state_provider() if state_provider else None

    registry = ExecutorRegistry({
        "no_action": _noop,
        "read_value": lambda a, p: _read_value(a, p, _state()),
        "observe": lambda a, p: _observe(a, p, _state()),
    })
    return registry
