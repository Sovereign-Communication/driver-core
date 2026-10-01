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
import unittest

from driver_core.config import ConfigError, load_settings
from driver_core.driver import Driver
from driver_core.errors import PerceptionUnavailable, SchemaError
from driver_core.perception import CLI, DOM, GUI, MCP, StructuredSource, Target
from driver_core.schema import validate_state
from driver_core.server import Service
from driver_core.states import (
    DOM_SCHEMA, SCHEMA_BY_CLASS, WIRE_TARGETS, SCREEN_SCHEMA,
)
from driver_core.wiring import (
    configured_pools, configured_sources, parse_argv,
)


def _settings(**kwargs):
    return load_settings(env={}, **kwargs)


class DefaultsTests(unittest.TestCase):
    """Nothing configured means nothing observed."""

    def test_no_configuration_declares_no_sources(self):
        self.assertEqual(_settings().declared_sources(), [])
        self.assertEqual(configured_sources(_settings()), [])

    def test_a_default_driver_has_no_sources_and_no_vision(self):
        driver = Driver(settings=_settings(), audit=_Memory())
        self.assertEqual(driver.sources, [])
        self.assertIsNone(driver.screen)
        # The structured pools exist and are free; they hold no vision slot,
        # and with no configured source the class refuses anyway.
        self.assertEqual(sorted(driver.pools), [CLI, DOM, MCP])
        self.assertNotIn(GUI, driver.pools)

    def test_explicitly_empty_still_means_empty(self):
        """``()`` is a deliberate choice and must survive configuration."""
        driver = Driver(settings=_settings(cli_command="echo hi"),
                        sources=(), pools={}, screen=None, audit=_Memory())
        self.assertEqual(driver.sources, [])

    def test_configured_sources_become_live_sources(self):
        driver = Driver(settings=_settings(cli_command="echo hi"),
                        audit=_Memory())
        self.assertEqual([s.name for s in driver.sources], ["cli"])

    def test_the_vision_tier_is_off_unless_its_own_switch_is_set(self):
        driver = Driver(settings=_settings(cli_command="echo hi"),
                        audit=_Memory())
        self.assertIsNone(driver.screen)
        self.assertNotIn(GUI, driver.pools)
        self.assertNotIn(GUI, _settings(cli_command="echo hi")
                         .declared_sources())

    def test_a_declared_screen_source_is_consulted_once_not_twice(self):
        """``sources`` and ``screen`` are two handles onto one set.

        A driver built from ``DRIVER_SCREEN`` already has a screen source in
        its declared set, so also appending ``self.screen`` would capture the
        same pixels twice on every step and report the tier twice on
        ``/health``. The regression test is the count.
        """
        driver = Driver(settings=_settings(screen_enabled=True), audit=_Memory())
        names = [s.name for s in driver.observation_sources()]
        self.assertEqual(names, ["screen"])
        self.assertIs(driver.screen, driver.sources[0])
        self.assertEqual(
            Service(driver=driver, token="t").health()["sources"], ["screen"])

    def test_an_explicit_screen_source_beside_a_structured_one_is_kept(self):
        """A caller registering its own screen source still gets it."""
        screen = StructuredSource("screen", lambda ref: {"window_title": "x"},
                                  serves=("gui",))
        driver = Driver(settings=_settings(cli_command="echo hi"),
                        sources=[StructuredSource("cli", lambda ref: None)],
                        screen=screen, pools={}, audit=_Memory())
        self.assertIs(driver.screen, screen)
        self.assertEqual([s.name for s in driver.observation_sources()],
                         ["cli", "screen"])


class _Memory:
    """A chain-free audit log, so a Driver can be built without one."""

    def append(self, kind, **fields):
        return {}

    @property
    def count(self):
        return 0

    def read_all(self):
        return []

    def verify(self):
        class V:
            ok = True
            detail = ""
            records = 0
            def to_dict(self):
                return {"ok": True, "records": 0, "detail": ""}
        return V()

    def head(self):
        return "genesis"


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
                self.assertEqual(settings.declared_sources(), [expected])

    def test_an_mcp_command_without_a_tool_enables_nothing(self):
        """Half a declaration is not a declaration."""
        self.assertEqual(_settings(mcp_command="srv --x").declared_sources(), [])

    def test_the_order_reported_is_the_declared_tier_order(self):
        settings = _settings(screen_enabled=True, dom_url="https://e.invalid/r",
                             cli_command="echo hi")
        self.assertEqual(settings.declared_sources(), ["cli", "dom", "screen"])

    def test_configured_sources_follow_the_declared_order(self):
        settings = _settings(screen_enabled=True, dom_url="https://e.invalid/r",
                             cli_command="echo hi")
        self.assertEqual([s.name for s in configured_sources(settings)],
                         ["cli", "dom", "screen"])


class PoolWiringTests(unittest.TestCase):

    def test_structured_classes_get_free_pools_of_two(self):
        pools = configured_pools(_settings(), budget=None, audit=None)
        self.assertEqual(sorted(pools), [CLI, DOM, MCP])
        for cls in (CLI, MCP, DOM):
            self.assertEqual(pools[cls].serves, (cls,))
            self.assertEqual(pools[cls].size, 2)

    def test_the_vision_pool_appears_only_when_enabled(self):
        self.assertNotIn(GUI, configured_pools(_settings(), budget=None,
                                               audit=None))
        pools = configured_pools(_settings(screen_enabled=True), budget=None,
                                 audit=None)
        self.assertIn(GUI, pools)

    def test_a_vision_pool_refuses_every_structured_class(self):
        pools = configured_pools(_settings(screen_enabled=True), budget=None,
                                 audit=None)
        for cls in (CLI, MCP, DOM):
            self.assertTrue(pools[GUI].refuses_class(cls))

    def test_a_configured_cli_pool_refuses_a_dom_target(self):
        pools = configured_pools(_settings(), budget=None, audit=None)
        self.assertIsNone(pools[CLI].for_target(Target("x", DOM)))


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

    def test_every_class_has_a_schema_and_the_wire_table_agrees(self):
        for cls in (CLI, MCP, DOM, GUI):
            self.assertIn(cls, SCHEMA_BY_CLASS)
        for name, (cls, schema) in WIRE_TARGETS.items():
            self.assertIs(schema, SCHEMA_BY_CLASS[cls], name)


class RefusalQualityTests(unittest.TestCase):
    """A refusal has to say what to do about it."""

    def test_the_refusal_names_the_configured_sources(self):
        settings = _settings(cli_command="echo hi")
        driver = Driver(settings=settings, audit=_Memory())
        with self.assertRaises(PerceptionUnavailable) as ctx:
            driver._capture(Target("x", DOM), ())
        message = str(ctx.exception)
        self.assertIn("Configured sources", message)
        self.assertIn("cli", message)

    def test_the_refusal_says_when_nothing_is_configured(self):
        driver = Driver(settings=_settings(), audit=_Memory())
        with self.assertRaises(PerceptionUnavailable) as ctx:
            driver._capture(Target("x", CLI), ())
        self.assertIn("none", str(ctx.exception))


class ForeignNamespaceTests(unittest.TestCase):
    """The new settings are DRIVER_* and nothing else."""

    def test_no_new_foreign_prefix_is_read(self):
        from driver_core.config import FOREIGN_PREFIXES, ENV_PREFIX
        for name in ("CLI_COMMAND", "MCP_COMMAND", "MCP_TOOL", "DOM_URL",
                     "SCREEN"):
            self.assertEqual(ENV_PREFIX + name, f"DRIVER_{name}")
        for prefix in FOREIGN_PREFIXES:
            self.assertNotIn(prefix, ENV_PREFIX)

    def test_a_configured_cli_command_is_read_from_the_environment(self):
        env = {"DRIVER_CLI_COMMAND": "echo hi"}
        self.assertEqual(load_settings(env=env).cli_command, "echo hi")


if __name__ == "__main__":
    unittest.main()
