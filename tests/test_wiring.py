"""Declared sources become live ones, and nothing else does.

The perception tiers existed and were provable but unreachable: a default
``Driver()`` carried no sources, so ``driver-core step`` answered ``Tried:
none`` and every request ended in ``no_capture``. These tests pin the wiring
that closes that, and pin the two properties that make it safe to expose a
capability from configuration at all:

* **a source is off unless it was named** -- no tier is inferred from
  another setting's presence, and the vision tier has its own switch;
* **a declared command is tokenised, never shelled**.

Both matter because the alternative is a configuration string becoming code,
which is the one thing :mod:`driver_core.osal` exists to prevent.
"""
import os
import unittest
from unittest import mock

from driver_core.audit import MemoryAuditLog
from driver_core.config import ConfigError, load_settings
from driver_core.driver import Driver
from driver_core.errors import PerceptionUnavailable, SchemaError
from driver_core.extractors import ExtractorPool
from driver_core.perception import CLI, DOM, GUI, MCP, StructuredSource, Target
from driver_core.schema import validate_state
from driver_core.server import Service
from driver_core.states import (
    CLI_SCHEMA, DOM_SCHEMA, SCREEN_SCHEMA, WIRE_TARGETS, resolve_wire_target,
)
from driver_core.wiring import configured_pools, configured_sources, parse_argv


def _settings(**kwargs):
    return load_settings(env={}, **kwargs)


def _driver(**kwargs):
    kwargs.setdefault("settings", _settings())
    kwargs.setdefault("audit", MemoryAuditLog())
    return Driver(**kwargs)


class DefaultsTests(unittest.TestCase):
    """Nothing configured means nothing observed."""

    def test_a_default_driver_has_no_sources_and_no_vision(self):
        driver = _driver()
        self.assertEqual(driver.sources, [])
        self.assertEqual(configured_sources(driver.settings), [])
        self.assertIsNone(driver.screen)
        # The structured pools exist and are free; they hold no vision slot,
        # and with no configured source the class refuses anyway.
        self.assertEqual(sorted(driver.pools), [CLI, DOM, MCP])
        self.assertNotIn(GUI, driver.pools)

    def test_explicitly_empty_still_means_empty(self):
        """``()`` is a deliberate choice and must survive configuration."""
        driver = _driver(settings=_settings(cli_command="echo hi"),
                         sources=(), pools={}, screen=None)
        self.assertEqual(driver.sources, [])

    def test_configured_sources_become_live_sources(self):
        driver = _driver(settings=_settings(cli_command="echo hi"))
        self.assertEqual([s.name for s in driver.sources], ["cli"])

    def test_the_vision_tier_is_off_unless_its_own_switch_is_set(self):
        driver = _driver(settings=_settings(cli_command="echo hi"))
        self.assertIsNone(driver.screen)
        self.assertNotIn(GUI, driver.pools)
        self.assertNotIn("screen", [s.name for s in driver.sources])

    def test_a_declared_screen_source_is_consulted_once_not_twice(self):
        """The screen source is in the one list, and ``screen`` reads it back.

        ``sources`` and ``screen`` used to be two handles onto the same set,
        reconciled by a method that had to be called from everywhere. Now
        there is one list and ``screen`` is derived from it, so a step cannot
        capture the same pixels twice and ``/health`` cannot report the tier
        twice. The regression test is the count.
        """
        driver = _driver(settings=_settings(screen_enabled=True))
        self.assertEqual([s.name for s in driver.sources], ["screen"])
        self.assertIs(driver.screen, driver.sources[0])
        self.assertEqual(Service(driver=driver, token="t").health()["sources"],
                         ["screen"])

    def test_an_explicit_screen_source_joins_the_one_list(self):
        """A caller registering its own screen source still gets it, once."""
        screen = StructuredSource("screen", lambda ref: {"window_title": "x"},
                                  serves=("gui",))
        driver = _driver(settings=_settings(cli_command="echo hi"),
                         sources=[StructuredSource("cli", lambda ref: None)],
                         screen=screen)
        self.assertEqual([s.name for s in driver.sources], ["cli", "screen"])
        self.assertIs(driver.screen, screen)
        # Naming the same object in both places registers it once, not twice.
        both = _driver(sources=[screen], screen=screen)
        self.assertEqual(both.sources, [screen])


class TierIsolationTests(unittest.TestCase):
    """One switch, one tier. Never inferred from another setting."""

    def test_each_setting_enables_exactly_its_own_tier(self):
        cases = {
            "cli": _settings(cli_command="echo hi"),
            "mcp": _settings(mcp_command="srv --x", mcp_tool="status"),
            "dom": _settings(dom_url="https://example.invalid/r"),
            "screen": _settings(screen_enabled=True),
        }
        for expected, settings in cases.items():
            with self.subTest(tier=expected):
                self.assertEqual([s.name for s in configured_sources(settings)],
                                 [expected])

    def test_an_mcp_command_without_a_tool_enables_nothing(self):
        """Half a declaration is not a declaration."""
        self.assertEqual(configured_sources(_settings(mcp_command="srv --x")),
                         [])

    def test_configured_sources_follow_the_declared_tier_order(self):
        settings = _settings(screen_enabled=True, dom_url="https://e.invalid/r",
                             cli_command="echo hi")
        self.assertEqual([s.name for s in configured_sources(settings)],
                         ["cli", "dom", "screen"])


class PoolWiringTests(unittest.TestCase):

    def _pools(self, **kwargs):
        return configured_pools(_settings(**kwargs), budget=None, audit=None)
    def test_structured_classes_get_free_pools(self):
        pools = self._pools()
        self.assertEqual(sorted(pools), [CLI, DOM, MCP])
        for cls in (CLI, MCP, DOM):
            self.assertEqual(pools[cls].serves, (cls,))

    def test_the_vision_pool_appears_only_when_enabled(self):
        self.assertNotIn(GUI, self._pools())
        self.assertIn(GUI, self._pools(screen_enabled=True))

    def test_a_pool_is_sized_by_the_quorum_it_has_to_satisfy(self):
        """A pool with fewer slots than the quorum can never meet it.

        A declared driver that refused every step with
        ``insufficient_agreement`` would be a tier chain that exists and is
        still unreachable, so the pool is sized from the setting rather than
        from a constant that happens to match the default.
        """
        for quorum in (1, 2, 3):
            with self.subTest(quorum=quorum):
                structured = self._pools(quorum=quorum)
                self.assertEqual(structured[CLI].size, quorum)
                vision = self._pools(quorum=quorum, screen_enabled=True)
                self.assertEqual(vision[GUI].size, quorum)

    def test_caller_supplied_pools_are_the_only_pools(self):
        """One owner per collaborator: passing ``pools`` replaces, not merges.

        The declared pools used to sit underneath whatever the caller passed
        unless all three constructor arguments were supplied, so the same
        ``pools={}`` meant "the declared ones" or "none" depending on the
        other arguments. Replace is the only version of this that can be
        stated in one sentence.
        """
        only_dom = {DOM: ExtractorPool([], serves=(DOM,))}
        driver = _driver(settings=_settings(screen_enabled=True), pools=only_dom)
        self.assertEqual(sorted(driver.pools), [DOM])
        self.assertEqual(_driver(pools={}).pools, {})


class CommandParsingTests(unittest.TestCase):
    """Tokenisation, never a shell."""

    def test_a_simple_command_splits_into_argv(self):
        self.assertEqual(parse_argv("python -c print(1)"),
                         ["python", "-c", "print(1)"])

    def test_quoting_is_respected(self):
        self.assertEqual(parse_argv('tool --name "two words"'),
                         ["tool", "--name", "two words"])

    def test_an_empty_command_is_refused(self):
        for bad in ("", "   ", None):
            with self.assertRaises(ConfigError):
                parse_argv(bad)

    def test_an_unbalanced_quote_is_refused_by_name(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_argv('tool --name "unclosed')
        self.assertIn("tokenised", str(ctx.exception))

    def test_no_expansion_happens(self):
        """The whole point: ``$VAR`` and globs stay literal text."""
        self.assertEqual(parse_argv("tool $HOME *.txt"),
                         ["tool", "$HOME", "*.txt"])

    def test_no_shell_metacharacter_becomes_a_pipe(self):
        argv = parse_argv("tool ; rm -rf /")
        self.assertEqual(argv, ["tool", ";", "rm", "-rf", "/"])
        self.assertNotIn("|", argv)


class DomSchemaTests(unittest.TestCase):
    """The DOM tier needs a schema that declares what a document can supply."""

    def test_a_dom_target_cannot_satisfy_the_screen_schema(self):
        """The reason DOM_SCHEMA exists, pinned so it is not undone."""
        with self.assertRaises(SchemaError):
            validate_state({"window_title": "Report", "visible_text": "x"},
                           SCREEN_SCHEMA)

    def test_a_document_does_satisfy_the_dom_schema(self):
        state = validate_state({"window_title": "Report", "visible_text": "x"},
                               DOM_SCHEMA)
        self.assertEqual(state["window_title"], "report")

    def test_a_document_with_no_title_is_a_shortfall_not_a_guess(self):
        with self.assertRaises(SchemaError) as ctx:
            validate_state({"visible_text": "x"}, DOM_SCHEMA)
        self.assertIn("missing required field", str(ctx.exception))

    def test_the_wire_table_binds_every_class_to_a_schema(self):
        """One table, so a class and its schema cannot drift apart.

        The old ``SCHEMAS_BY_TARGET`` answered ``"dom"`` with the screen
        schema while the wire table said nothing at all, which is how a
        request ended up able to reach pixels by default. This is the pin.
        """
        classes = {cls for cls, _ in WIRE_TARGETS.values()}
        self.assertEqual(classes, {CLI, MCP, DOM, GUI})
        self.assertEqual(WIRE_TARGETS["dom"], (DOM, DOM_SCHEMA))
        self.assertEqual(WIRE_TARGETS["cli"], (CLI, CLI_SCHEMA))
        self.assertEqual(WIRE_TARGETS["mcp"], (MCP, CLI_SCHEMA))
        self.assertEqual(WIRE_TARGETS["screen"], WIRE_TARGETS["gui"])

    def test_the_wire_table_is_resolved_by_one_rule_for_both_surfaces(self):
        """One implementation, so the CLI and the service cannot disagree.

        The service turns a refusal into a 400 and the CLI prints it; neither
        re-derives the rule, so "must this be declared?" is decided once.
        """
        for name, expected in WIRE_TARGETS.items():
            with self.subTest(name=name):
                self.assertEqual(resolve_wire_target(name), expected)
        self.assertEqual(resolve_wire_target("  CLI "), (CLI, CLI_SCHEMA))

    def test_the_rule_refuses_what_it_cannot_bind(self):
        for bad in (None, "", "   ", "banana", "teleport", 17):
            with self.subTest(bad=bad):
                with self.assertRaises(PerceptionUnavailable):
                    resolve_wire_target(bad)
        with self.assertRaises(PerceptionUnavailable) as ctx:
            resolve_wire_target(None)
        self.assertIn("schema is required", str(ctx.exception))
        with self.assertRaises(PerceptionUnavailable) as ctx:
            resolve_wire_target("banana")
        self.assertIn("unknown schema", str(ctx.exception))


class RefusalQualityTests(unittest.TestCase):
    """A refusal has to say what to do about it."""

    def test_the_refusal_names_the_configured_sources(self):
        driver = _driver(settings=_settings(cli_command="echo hi"))
        with self.assertRaises(PerceptionUnavailable) as ctx:
            driver._capture(Target("x", DOM), ())
        message = str(ctx.exception)
        self.assertIn("Configured sources", message)
        self.assertIn("cli", message)

    def test_the_refusal_says_when_nothing_is_configured(self):
        with self.assertRaises(PerceptionUnavailable) as ctx:
            _driver()._capture(Target("x", CLI), ())
        self.assertIn("none", str(ctx.exception))

    def test_a_driver_that_cannot_be_built_says_so_on_the_cli(self):
        """A bad declaration is reported, not raised as a traceback.

        ``Driver`` construction happens before the command runs, so without
        this the one fault an operator can most easily introduce -- a command
        with an unbalanced quote -- would escape the CLI's error handling
        entirely.
        """
        import contextlib
        import io
        from driver_core import cli

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ,
                             {"DRIVER_CLI_COMMAND": 'tool --name "unclosed'}):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["health"])
        self.assertEqual(code, 2)
        self.assertIn("could not be tokenised", out.getvalue() + err.getvalue())


if __name__ == "__main__":
    unittest.main()
