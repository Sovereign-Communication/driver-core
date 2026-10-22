"""Configuration for driver-core, namespaced entirely under ``DRIVER_*``.

The namespace is not cosmetic. driver-core runs in a workspace that sits
beside a separate, actively-developed project, and the operator has several
of those in flight at once. If this package read a shared or conventionally
named config file, a stray run here could read -- or worse, be handed
credentials belonging to work it has nothing to do with.

So the rule is enforced rather than merely stated: :data:`ENV_PREFIX` is
``DRIVER_``, every accessor is a pure function of the supplied environment,
and :func:`assert_no_foreign_reads` exists so a test can prove that no
Harness-shaped variable was consulted. Defaults never fall back to another
project's config path, and there is no ambient global -- a caller passes the
mapping it wants.

Every setting is also overridable by constructor argument, so a test or an
embedded caller never has to mutate the process environment at all.

This module states *what was declared* and nothing more. Which tiers that
declaration turns into is :mod:`driver_core.wiring`'s job, and the driver
holds the answer -- an earlier version also answered here, which meant the
same four settings were read twice to produce two lists that had to be kept
in agreement by hand, and that a pure declaration had to import the
perception taxonomy just to order one of them.
"""
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType

from .actions import ACTION_CLASSES, IRREVERSIBLE, MUTATING, READ_ONLY

#: The one prefix this package is allowed to read.
ENV_PREFIX = "DRIVER_"

#: Variables belonging to the neighbouring project. driver-core must never
#: read these. They are named here so the guard can assert it, and so the
#: assertion fails loudly if someone later adds a cross-read.
FOREIGN_PREFIXES = ("HARNESS_", "OPENROUTER_", "FREEBUFF_")

DEFAULT_BASE_URL = "http://127.0.0.1:8791"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8791

#: Default per-step pre-flight ceilings. Both refuse *before* dispatch.
DEFAULT_STEP_CEILING_USD = 0.50
DEFAULT_RUN_CEILING_USD = 5.00

#: Jev is priced on input only; output tokens are free at the account level.
#: The rate is **$0.042/Mtok**, reconciled from an operator usage export --
#: the derivation, and the earlier rate error this corrected, are the canon
#: addendum's. Named rather than inlined so a price correction is a one-line
#: change; note the ceilings below are compared against reservations priced
#: here, so a rate error scales what they admit.
JEV_INPUT_PRICE_PER_MILLION = 0.042

#: Confidence above which a decision is acted on. Jev's calibrated
#: confidence is the signal; this is the threshold; code owns both the
#: comparison and the vocabulary it is comparing within.
DEFAULT_CONFIDENCE_THRESHOLD = 0.70

#: The confidence band Jev's answers jittered across in the operator-verified
#: probe of the canon addendum (§5b owns the measurement). A threshold within
#: one width of the band is decided by sampling noise rather than by the
#: state -- the same screen can pass and fail on two runs.
CONFIDENCE_JITTER_BAND = (0.63, 0.67)

#: What a threshold must clear the jitter band by -- §5b's measured failure
#: was a clearance smaller than this. :func:`validated_ladder` enforces it
#: on every rung, so the failure is refused at construction.
JITTER_BAND_WIDTH = 0.04

#: Slack for the ladder comparisons. Rungs are declared in hundredths and
#: binary floats do not land exactly on them (0.71 - 0.67 is
#: 0.0399999...), so a rung written exactly one band width out is judged at
#: its written value rather than at its binary approximation. Wide enough
#: to absorb that noise, far narrower than any gap a real ladder declares.
_JITTER_EPSILON = 1e-9

#: The model the image path is pinned to. Box questions exist only on Jev
#: 1.14, and ``jev-latest`` still points at 1.13 -- so a request carrying
#: images or box questions through the moving alias is sent to whatever
#: the alias names today, and the alias will move again. A pin, not an
#: alias: :func:`image_model` replaces the default alias on the image path
#: and obeys any model an operator names explicitly.
JEV_IMAGE_MODEL = "jev-1.14.0"

#: Extraction consensus policy. See driver_core.consensus for why a quorum
#: below the full slot count is a real choice rather than a shortcut.
DEFAULT_QUORUM = 2
DEFAULT_MIN_AGREEMENT = 1.0

#: The shortest token this package will accept from an operator. The REST
#: token is the only thing standing between a loopback caller and the action
#: tier, and it is bearer-only: whoever holds it may execute anything consent
#: allows. 16 characters of base64url is roughly 96 bits, which is below any
#: brute-force budget worth naming, so a shorter value is refused rather than
#: accepted with a warning nobody reads. Randomly generated tokens are longer
#: than this by construction and are not checked against it.
MIN_TOKEN_LENGTH = 16


def validated_token(value, source):
    """Return ``value`` if it is an acceptable declared token, else refuse.

    One rule, one owner, because the failure it prevents is a service that
    binds with a credential too weak to matter -- which looks exactly like a
    service that is working. The *length* is never echoed and neither is any
    part of the value: a rejection message reaches a terminal and a log, and
    a log is somewhere a credential ends up.
    """
    if not value or not value.strip():
        raise ConfigError(
            f"{source} was declared but is blank; either give it a real "
            f"value or unset it to get a generated one")
    if len(value) < MIN_TOKEN_LENGTH:
        raise ConfigError(
            f"{source} is shorter than the {MIN_TOKEN_LENGTH}-character "
            f"minimum for a bearer token")
    return value


@dataclass(frozen=True)
class ConfidenceRung:
    """One rung of the per-action confidence ladder.

    ``act`` is the confidence at which an action of this consequence class
    acts; ``confirm`` is where it stops for a human confirmation instead
    ("an irreversible action needs 0.85 or a confirmation step"). A rung
    with ``confirm == act`` has no confirmation band at all, which is the
    declaration for a class whose actions need no human in the loop.
    """

    act: float
    confirm: float

    def __post_init__(self):
        """Every malformed declaration fails here, naming the field.

        None, strings, bools (an int in Python's clothing) and non-finite
        floats are not thresholds -- they are mistakes, and a mistake that
        reaches a comparison raises ``TypeError`` far from its cause or,
        worse, compares as a number it was never meant to be. Refused at
        construction with :class:`ConfigError`, so the operator-facing
        surface reports configuration, never a crash.
        """
        for label, value in (("act", self.act), ("confirm", self.confirm)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(
                    f"confidence ladder rung {label} must be a finite "
                    f"number in [0, 1], got {value!r} "
                    f"({type(value).__name__})")
            if not math.isfinite(value):
                raise ConfigError(
                    f"confidence ladder rung {label} must be a finite "
                    f"number in [0, 1], got {value}")
            if not 0 <= value <= 1:
                raise ConfigError(
                    f"confidence ladder rung {label} {value} is outside "
                    f"[0, 1]")


#: The declared confidence ladder, per action by consequence. The global
#: floor (``confidence_threshold``) still applies to everything: a rung can
#: only raise the bar for its class, never lower it -- an action clears
#: ``max(floor, rung.act)`` to act, ``max(floor, rung.confirm)`` to reach
#: the confirmation route, and is blocked below that.
#:
#: Every value clears the observed jitter band
#: (:data:`CONFIDENCE_JITTER_BAND`) by more than its width
#: (:data:`JITTER_BAND_WIDTH`), so none is §5b's failure mode.
CONFIDENCE_LADDER = MappingProxyType({
    # Observes, changes nothing, safe to repeat: no confirmation band, the
    # rung is its own confirm floor.
    READ_ONLY: ConfidenceRung(act=0.55, confirm=0.55),
    # Changes observable state but is recoverable: acts at 0.75, and the
    # band below it stops for a confirmation.
    MUTATING: ConfidenceRung(act=0.75, confirm=0.55),
    # Cannot be undone by this system: acts at 0.85, or stops for a fresh
    # human confirmation in the band below.
    IRREVERSIBLE: ConfidenceRung(act=0.85, confirm=0.75),
})


def _clears_jitter_band(value):
    """``True`` when a threshold sits at least one band width from the band."""
    low, high = CONFIDENCE_JITTER_BAND
    if value < low:
        clearance = low - value
    elif value > high:
        clearance = value - high
    else:
        clearance = -1.0
    return clearance + _JITTER_EPSILON >= JITTER_BAND_WIDTH


def validated_ladder(ladder):
    """Return ``ladder`` if every rung is safe to enforce, else refuse.

    The rules are §5b's, enforced rather than merely documented. Every
    threshold -- act and confirm alike -- must clear the observed jitter
    band by at least its width; two act rungs must be more than one width
    apart (a smaller gap is two rungs that look separated on paper and are
    not); and a confirmation band must be either absent or wider than the
    width too. Code owns the thresholds, so code owns this check.
    """
    if not isinstance(ladder, Mapping) or not ladder:
        raise ConfigError("a confidence ladder must be a non-empty mapping")
    unknown = sorted(set(ladder) - set(ACTION_CLASSES))
    if unknown:
        raise ConfigError(
            f"confidence ladder declares thresholds for unknown consequence "
            f"class(es) {unknown}; declare one of {list(ACTION_CLASSES)}")
    for name, rung in ladder.items():
        if not isinstance(rung, ConfidenceRung):
            raise ConfigError(
                f"confidence ladder rung {name!r} must be a ConfidenceRung")
        # Well-formed values are the rung's own guarantee
        # (:meth:`ConfidenceRung.__post_init__`); what is checked here is
        # where they sit -- which is the ladder's business, not a single
        # rung's.
        for label, value in (("act", rung.act), ("confirm", rung.confirm)):
            if not _clears_jitter_band(value):
                raise ConfigError(
                    f"confidence ladder rung {name!r} {label} {value} does "
                    f"not clear the observed jitter band "
                    f"{CONFIDENCE_JITTER_BAND} by its width "
                    f"{JITTER_BAND_WIDTH}; a threshold that close is decided "
                    f"by sampling noise rather than by the state")
        if (rung.act != rung.confirm
                and rung.act - rung.confirm <= JITTER_BAND_WIDTH + _JITTER_EPSILON):
            raise ConfigError(
                f"confidence ladder rung {name!r} has a confirmation band "
                f"[{rung.confirm}, {rung.act}) no wider than the jitter band "
                f"width {JITTER_BAND_WIDTH}")
    acts = sorted(rung.act for rung in ladder.values())
    for lower, upper in zip(acts, acts[1:], strict=False):
        if upper - lower <= JITTER_BAND_WIDTH + _JITTER_EPSILON:
            raise ConfigError(
                f"confidence ladder act rungs {lower} and {upper} are not "
                f"more than one jitter band width apart; they would be "
                f"separated on paper, not by the model")
    return ladder


def image_model(settings):
    """The model an image-path request is sent to: a pin, never the alias.

    An operator who names a version (or the docs' blessed ``jev-preview``)
    has pinned their own choice and is obeyed; only the default alias is
    replaced, because the alias is the thing that moves under you.
    """
    configured = (settings.jev_model or "").strip()
    if configured and configured != "jev-latest":
        return configured
    return JEV_IMAGE_MODEL


def _env_bool(env, name, default):
    raw = env.get(ENV_PREFIX + name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(env, name, default):
    raw = env.get(ENV_PREFIX + name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(
            f"{ENV_PREFIX}{name}={raw!r} is not a number") from None


def _env_int(env, name, default):
    raw = env.get(ENV_PREFIX + name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{ENV_PREFIX}{name}={raw!r} is not an integer") from None


class ConfigError(Exception):
    """A driver-core setting is present but unusable.

    Distinct from the package's :class:`~driver_core.errors.DriverError`
    family on purpose: a bad setting is an operator configuration fault
    raised before any work starts, not a runtime condition the caller should
    catch and continue past.
    """


@dataclass(frozen=True)
class Settings:
    """Resolved settings. Frozen so nothing downstream can mutate policy."""

    base_url: str = DEFAULT_BASE_URL
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    jev_api_key: str = field(default="", repr=False)
    #: The REST bearer token, when an operator declares one. A credential:
    #: excluded from ``repr`` so it cannot reach a traceback, and absent from
    #: ``redacted()`` because that dict is printed by ``/health``. When empty
    #: the service generates one per process, which is the safe default for a
    #: caller driving the service in-process and useless for one that needs a
    #: token it can present -- hence the setting.
    token: str = field(default="", repr=False)
    jev_model: str = "jev-latest"
    #: The image surface is opt-in at the far end too: the endpoint rejects
    #: an image request without its danger-zone header, and this flag is the
    #: operator's admission of that header. Off by default; honored only
    #: when a request actually carries assets.
    jev_images: bool = False
    #: Frame history is opt-in beside the image surface: consecutive
    #: prepared frames retained only in the slot's bounded session history
    #: feed a semantic stability question in the extraction pack. Off by default.
    jev_frames: bool = False
    #: Box questions are the second opt-in beside the image surface: image
    #: traffic on the pinned model, refused by name without it. Off by
    #: default.
    jev_box: bool = False
    #: The per-action confidence ladder is the third opt-in: the rungs are
    #: always declared, but ``policy.check_decision`` enforces them only
    #: with this flag set, so an ordinary run keeps the single global
    #: threshold. Off by default.
    jev_ladder: bool = False
    #: The guardrail pack is the second half of the same landing: one
    #: billed request per acting step, thresholded in code at the execution
    #: gate. Off by default; without the flag the pack is never asked or
    #: charged.
    jev_guardrails: bool = False
    #: The ladder, keyed by consequence class. Validated on construction:
    #: every rung must clear the observed jitter band by its width.
    confidence_ladder: Mapping = CONFIDENCE_LADDER
    extractor_pool: tuple = ()
    step_ceiling_usd: float = DEFAULT_STEP_CEILING_USD
    run_ceiling_usd: float = DEFAULT_RUN_CEILING_USD
    quorum: int = DEFAULT_QUORUM
    min_agreement: float = DEFAULT_MIN_AGREEMENT
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    audit_path: str = ""
    dry_run: bool = False
    allow_write: bool = False
    #: Declared perception sources. Each is off unless the operator sets it,
    #: so the default driver observes nothing rather than guessing at what to
    #: observe. None of these is a secret and all appear in ``redacted()``.
    cli_command: str = ""
    mcp_command: str = ""
    mcp_tool: str = ""
    dom_url: str = ""
    #: The vision tier is opt-in. It is the only source that costs money and
    #: the only one that cannot be re-derived from a structured input, so it
    #: is never enabled by the presence of another setting.
    screen_enabled: bool = False

    def __post_init__(self):
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigError(
                f"base_url must be an http(s) URL, got {self.base_url!r}")
        if not 0 < self.confidence_threshold <= 1:
            raise ConfigError(
                f"confidence_threshold must be in (0, 1], got "
                f"{self.confidence_threshold}")
        if self.quorum < 1:
            raise ConfigError(f"quorum must be >= 1, got {self.quorum}")
        if not 0 < self.min_agreement <= 1:
            raise ConfigError(
                f"min_agreement must be in (0, 1], got {self.min_agreement}")
        if self.step_ceiling_usd <= 0 or self.run_ceiling_usd <= 0:
            raise ConfigError("spend ceilings must be positive")
        if self.run_ceiling_usd < self.step_ceiling_usd:
            raise ConfigError(
                f"run ceiling ${self.run_ceiling_usd} is below the step ceiling "
                f"${self.step_ceiling_usd}, so no step could ever run")
        if self.token:
            validated_token(self.token, "the declared token")
        validated_ladder(self.confidence_ladder)

    @property
    def keyed(self):
        """Honest key state. A missing key is a name, not an empty string."""
        return bool(self.jev_api_key)

    def redacted(self):
        """The settings as they may be printed or logged.

        Neither credential is rendered, not even truncated -- a prefix of a
        secret is still a secret, and this dict is destined for logs and for
        the body of ``GET /health``. That is why there is no ``token`` key
        rather than a masked one: a host that needs the token already holds
        it, and a host that does not must not be able to read it off a
        loopback endpoint that any local process can reach.
        """
        data = {
            "base_url": self.base_url,
            "host": self.host,
            "port": self.port,
            "jev_model": self.jev_model,
            "keyed": self.keyed,
            "extractor_pool": list(self.extractor_pool),
            "step_ceiling_usd": self.step_ceiling_usd,
            "run_ceiling_usd": self.run_ceiling_usd,
            "quorum": self.quorum,
            "min_agreement": self.min_agreement,
            "confidence_threshold": self.confidence_threshold,
            "dry_run": self.dry_run,
            "allow_write": self.allow_write,
        }
        return data

    def with_overrides(self, **kwargs):
        return replace(self, **kwargs)


def load_settings(env=None, **overrides):
    """Build settings from an environment mapping plus explicit overrides.

    ``env`` defaults to the real process environment, but tests pass their
    own mapping so they never have to mutate global state.
    """
    env = os.environ if env is None else env
    pool_raw = env.get(ENV_PREFIX + "EXTRACTOR_POOL", "")
    pool = tuple(p.strip() for p in pool_raw.split(",") if p.strip())
    # Presence is the whole signal here: an operator who sets DRIVER_TOKEN to
    # the empty string has made a mistake worth naming, whereas an operator who
    # never mentions it has asked for the generated default. Reading it with
    # ``.get(..., "")`` would silently collapse those two into one.
    token = validated_token(env[ENV_PREFIX + "TOKEN"], "DRIVER_TOKEN") \
        if ENV_PREFIX + "TOKEN" in env else ""
    settings = Settings(
        base_url=env.get(ENV_PREFIX + "BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        host=env.get(ENV_PREFIX + "HOST", DEFAULT_HOST),
        port=_env_int(env, "PORT", DEFAULT_PORT),
        jev_api_key=env.get(ENV_PREFIX + "JEV_API_KEY", ""),
        token=token,
        jev_model=env.get(ENV_PREFIX + "JEV_MODEL", "jev-latest"),
        jev_images=_env_bool(env, "JEV_IMAGES", False),
        jev_frames=_env_bool(env, "JEV_FRAMES", False),
        jev_box=_env_bool(env, "JEV_BOX", False),
        jev_ladder=_env_bool(env, "JEV_LADDER", False),
        jev_guardrails=_env_bool(env, "JEV_GUARDRAILS", False),
        extractor_pool=pool,
        step_ceiling_usd=_env_float(env, "STEP_CEILING_USD",
                                    DEFAULT_STEP_CEILING_USD),
        run_ceiling_usd=_env_float(env, "RUN_CEILING_USD",
                                   DEFAULT_RUN_CEILING_USD),
        quorum=_env_int(env, "QUORUM", DEFAULT_QUORUM),
        min_agreement=_env_float(env, "MIN_AGREEMENT", DEFAULT_MIN_AGREEMENT),
        confidence_threshold=_env_float(env, "CONFIDENCE_THRESHOLD",
                                        DEFAULT_CONFIDENCE_THRESHOLD),
        audit_path=env.get(ENV_PREFIX + "AUDIT_PATH", ""),
        dry_run=_env_bool(env, "DRY_RUN", False),
        allow_write=_env_bool(env, "ALLOW_WRITE", False),
        cli_command=env.get(ENV_PREFIX + "CLI_COMMAND", ""),
        mcp_command=env.get(ENV_PREFIX + "MCP_COMMAND", ""),
        mcp_tool=env.get(ENV_PREFIX + "MCP_TOOL", ""),
        dom_url=env.get(ENV_PREFIX + "DOM_URL", ""),
        screen_enabled=_env_bool(env, "SCREEN", False),
    )
    return settings.with_overrides(**overrides) if overrides else settings


def default_audit_path():
    """The audit log's home, under this project's own state directory.

    Namespaced so it can never collide with another project's state, and
    created lazily by the audit module rather than at import time -- a library
    that writes to disk on import is a library nobody can safely import twice.
    """
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if base:
        return str(Path(base) / "driver-core" / "audit.jsonl")
    return str(Path.home() / ".driver-core" / "audit.jsonl")


def assert_no_foreign_reads(names):
    """Raise if any name read does not carry the driver-core prefix.

    The companion to the namespacing rule: rather than trusting every call
    site to remember, a caller (or a test over the whole package) can hand
    over the set of variables actually consulted and get a hard failure if a
    foreign one appears.
    """
    foreign = sorted(
        n for n in names
        if not n.startswith(ENV_PREFIX) and n.startswith(FOREIGN_PREFIXES)
    )
    if foreign:
        raise ConfigError(
            f"driver-core must not read foreign configuration {foreign}; it "
            f"reads only {ENV_PREFIX}* variables")
    return True
