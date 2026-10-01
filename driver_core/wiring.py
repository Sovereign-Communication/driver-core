"""Turning declared configuration into a live driver.

This module exists because of a specific failure: the perception tiers were
built, wired to each other, and provable -- and unreachable. A default
``Driver()`` carried no sources, so ``driver-core step`` answered ``Tried:
none`` and every ``POST /step`` ended in ``no_capture``. The chain worked
only for a caller writing Python by hand, which makes it a library feature
rather than a product one.

So this is the one place that reads the declared sources out of
:class:`~driver_core.config.Settings` and builds the live objects. Keeping it
here rather than in :mod:`driver_core.driver` is deliberate on two counts:
:class:`~driver_core.config.Settings` stays a pure declaration with no
perception imports, and a reader looking for "what can this driver observe"
finds it in one file instead of inferring it from a constructor.

Two rules the wiring holds to, both of which are the reason it exists:

* **A source is off unless it was named.** Nothing is inferred from another
  setting's presence. An operator who sets a CLI command gets the CLI tier
  and not the screen tier, and the vision tier is never enabled by anything
  other than its own explicit switch.
* **Commands are tokenised, never executed through a shell.**
  :func:`parse_argv` uses :mod:`shlex` for splitting only -- no globbing, no
  variable expansion, no redirection. A setting that reached a shell would be
  the first place in this package where a configuration string could become
  code, and the one rule :mod:`driver_core.osal` exists to prevent.
"""
import shlex

from .config import ConfigError
from .extractors import ExtractorPool, StructuredExtractor, build_vision_pool
from .perception import (
    CLI, DOM, GUI, MCP, CliSource, DomSource, McpSource, ScreenSource,
)
from .states import SCHEMA_BY_CLASS


def parse_argv(text):
    """Split a declared command into an argv list. Tokenisation only.

    ``shlex`` here does what a shell's word-splitting does and nothing else
    it does: no glob expansion, no ``$VAR`` interpolation, no ``|`` or ``&&``.
    Those are the features that turn a configuration string into code, and
    :func:`driver_core.osal.run` takes an argv list precisely so that no
    string ever reaches a shell.
    """
    if not text or not text.strip():
        raise ConfigError("a declared command is empty")
    try:
        argv = shlex.split(text)
    except ValueError as exc:
        raise ConfigError(
            f"declared command {text!r} could not be tokenised: {exc}") from None
    if not argv:
        raise ConfigError(f"declared command {text!r} has no executable")
    return argv


def configured_sources(settings):
    """The live perception sources this configuration enables.

    Returned in the declared tier order so ``/health`` and the refusal
    messages describe the chain the way it actually runs. An unparseable
    command raises rather than being skipped: a source the operator asked
    for and cannot run should be a loud configuration fault, not a silent
    absence that looks identical to "not configured".
    """
    sources = []
    if settings.cli_command:
        sources.append(CliSource(parse_argv(settings.cli_command)))
    if settings.mcp_command and settings.mcp_tool:
        sources.append(McpSource(parse_argv(settings.mcp_command),
                                 settings.mcp_tool))
    if settings.dom_url:
        sources.append(DomSource(settings.dom_url))
    if settings.screen_enabled:
        # The vision tier. Separate from the structured tiers because it is
        # the only one that spends money and the only one that cannot be
        # re-derived from state that was already available exactly.
        sources.append(ScreenSource())
    return sources


def configured_pools(settings, *, budget, audit, slots=2):
    """Extractor pools keyed by the target class each one serves.

    Structured pools are deterministic and free, so two slots of them give
    the consensus tier the two independent observers it needs at no cost.
    That is the whole practical payoff of the tier ordering: the classes that
    can be read exactly get two opinions for nothing, and only the class that
    cannot gets a bill.
    """
    pools = {}
    for cls in (CLI, MCP, DOM):
        schema = SCHEMA_BY_CLASS[cls]
        pools[cls] = ExtractorPool(
            [StructuredExtractor(f"{cls}-{i}", _declared_reader(schema))
             for i in range(max(1, int(slots)))],
            serves=(cls,))
    if settings.screen_enabled:
        pools[GUI] = build_vision_pool(settings, budget=budget, audit=audit,
                                       slots=max(1, int(slots)))
    return pools


def _declared_reader(schema):
    """A reader that reports only what the schema declares and the payload
    actually contains.

    It does not default a missing field and it does not carry an undeclared
    one through. Both are the same mistake in opposite directions: inventing
    a value the source never saw, and passing along something outside the
    contract. What the source genuinely cannot supply stays absent, so the
    tally reports a shortfall rather than agreement nobody observed.
    """
    declared = frozenset(schema.field_names())

    def read(capture, _schema):
        payload = capture.payload
        if not isinstance(payload, dict):
            return None
        return {k: v for k, v in payload.items() if k in declared}

    return read
