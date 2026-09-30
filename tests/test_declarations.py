"""Declaration guards and the OS boundary.

The OS boundary test is the one that keeps a platform difference from being
rediscovered by a user on the platform nobody tested. It is a build-failing
scan, not a convention, and it is deliberately written as an AST walk rather
than a grep so that ``import subprocess as sp`` and
``os.name`` behind an alias are both caught.
"""
import ast
import os
import unittest

from driver_core import osal
from driver_core.actions import (
    DEFAULT_VOCABULARY, READ_ONLY, Vocabulary, check_batch,
)
from driver_core.errors import SchemaError, VocabularyError
from driver_core.perception import StructuredSource, fingerprint
from driver_core.schema import Field, Schema, validate_state
from driver_core.states import SCREEN_SCHEMA

PACKAGE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "driver_core")

#: Modules permitted to touch the OS directly. Everything else is scanned.
BOUNDARY_OWNER = "osal.py"

BANNED = (
    ("subprocess import",
     lambda n: isinstance(n, (ast.Import, ast.ImportFrom))
     and any(a.name.split(".")[0] == "subprocess"
             for a in getattr(n, "names", []))),
    ("os.name platform probe",
     lambda n: isinstance(n, ast.Attribute) and n.attr == "name"
     and isinstance(n.value, ast.Name) and n.value.id == "os"),
    ("sys.platform platform probe",
     lambda n: isinstance(n, ast.Attribute) and n.attr == "platform"
     and isinstance(n.value, ast.Name) and n.value.id == "sys"),
)


def _package_modules():
    for name in sorted(os.listdir(PACKAGE)):
        if name.endswith(".py"):
            yield name, os.path.join(PACKAGE, name)


class OsBoundaryTests(unittest.TestCase):

    def test_os_contact_lives_only_in_osal(self):
        findings = []
        for name, path in _package_modules():
            if name == BOUNDARY_OWNER:
                continue
            tree = ast.parse(open(path, encoding="utf-8").read(), path)
            for node in ast.walk(tree):
                for label, predicate in BANNED:
                    if predicate(node):
                        findings.append(f"{name}:{node.lineno} {label}")
        self.assertEqual(findings, [],
                         "OS contact escaped the boundary: " + "; ".join(findings))

    def test_the_owner_actually_owns_what_the_scan_bans(self):
        """If the owner stopped using subprocess the ban would pass
        vacuously, and the guard would be protecting nothing."""
        source = open(os.path.join(PACKAGE, BOUNDARY_OWNER),
                      encoding="utf-8").read()
        self.assertIn("import subprocess", source)

    def test_the_scan_covers_the_whole_package(self):
        modules = dict(_package_modules())
        self.assertIn(BOUNDARY_OWNER, modules)
        self.assertGreaterEqual(len(modules), 10)

    def test_run_refuses_a_bare_string_so_a_shell_is_impossible(self):
        with self.assertRaises(TypeError) as ctx:
            osal.run("ls -la")
        self.assertIn("argv list", str(ctx.exception))

    def test_a_missing_binary_is_a_named_condition_not_an_exception(self):
        result = osal.run(["definitely-not-a-real-binary-xyz"])
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "not_found")

    def test_a_non_zero_exit_is_an_answer_not_a_failure(self):
        result = osal.run(["python", "-c", "import sys; sys.exit(3)"])
        self.assertEqual(result.returncode, 3)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "")

    def test_a_command_that_actually_runs_returns_its_output(self):
        result = osal.run(["python", "-c", "print('hello')"])
        self.assertTrue(result.ok)
        self.assertIn("hello", result.stdout)

    def test_synthetic_input_is_declared_unsupported_rather_than_faked(self):
        ok, detail = osal.send_input("click", target="OK")
        self.assertFalse(ok)
        self.assertIn("no registered backend", detail)

    def test_an_unknown_input_kind_is_refused(self):
        ok, detail = osal.send_input("teleport", value="x")
        self.assertFalse(ok)
        self.assertIn("unknown input kind", detail)


class SchemaDeclarationTests(unittest.TestCase):

    def test_a_schema_with_no_required_field_is_refused(self):
        """A schema nothing must satisfy would let an empty state pass."""
        with self.assertRaises(SchemaError):
            Schema("s", "1", [Field("a", "string", presence="optional")])

    def test_duplicate_fields_are_refused(self):
        with self.assertRaises(SchemaError):
            Schema("s", "1", [Field("a", "string"), Field("a", "string")])

    def test_an_enum_without_values_is_refused(self):
        with self.assertRaises(SchemaError):
            Field("a", "enum")

    def test_provenance_must_match_the_field_type(self):
        with self.assertRaises(SchemaError):
            Field("a", "string", provenance="numeric")
        with self.assertRaises(SchemaError):
            Field("a", "boolean", provenance="cased")

    def test_an_undeclared_field_makes_a_state_invalid(self):
        with self.assertRaises(SchemaError) as ctx:
            validate_state({"window_title": "x", "surprise": 1}, SCREEN_SCHEMA)
        self.assertIn("undeclared field", str(ctx.exception))

    def test_a_missing_required_field_is_invalid(self):
        with self.assertRaises(SchemaError):
            validate_state({"window_title": "x"}, SCREEN_SCHEMA)

    def test_an_optional_field_with_a_bad_value_is_invalid_not_absent(self):
        """Treated as absent, a hallucinated value becomes invisible instead
        of rejected."""
        with self.assertRaises(SchemaError):
            validate_state({"window_title": "x", "foreground_app": "app",
                            "error_dialog_present": False,
                            "dialog_kind": "not_a_kind"}, SCREEN_SCHEMA)

    def test_normalisation_is_applied_not_stored_raw(self):
        state = validate_state({"window_title": "  Report  ", "foreground_app": "e",
                                "error_dialog_present": "YES"},
                               SCREEN_SCHEMA)
        self.assertEqual(state["window_title"], "report")
        self.assertIs(state["error_dialog_present"], True)


class VocabularyTests(unittest.TestCase):

    def test_an_undeclared_action_raises_rather_than_defaulting(self):
        with self.assertRaises(VocabularyError) as ctx:
            DEFAULT_VOCABULARY.resolve("rm_rf")
        self.assertIn("not in the declared vocabulary", str(ctx.exception))

    def test_a_near_miss_does_not_resolve(self):
        for near in ("Observe", "observe ", "observes", "OBSERVE"):
            with self.assertRaises(VocabularyError):
                DEFAULT_VOCABULARY.resolve(near)

    def test_every_action_declares_an_executor_and_params(self):
        for name in DEFAULT_VOCABULARY.names():
            action = DEFAULT_VOCABULARY.resolve(name)
            self.assertTrue(action.executor)
            self.assertTrue(action.target)

    def test_irreversible_actions_require_exact_consent_scope(self):
        self.assertTrue(
            DEFAULT_VOCABULARY.resolve("delete_file").requires_human_confirmation)
        self.assertFalse(
            DEFAULT_VOCABULARY.resolve("click").requires_human_confirmation)

    def test_undeclared_parameters_are_refused(self):
        action = DEFAULT_VOCABULARY.resolve("read_value")
        with self.assertRaises(VocabularyError):
            action.check_params({"field": "x", "sneaky": 1})

    def test_missing_parameters_are_refused(self):
        action = DEFAULT_VOCABULARY.resolve("read_value")
        with self.assertRaises(VocabularyError):
            action.check_params({})

    def test_a_batch_may_not_smuggle_several_irreversible_actions(self):
        with self.assertRaises(VocabularyError):
            check_batch(["delete_file", "submit_irreversible"],
                        DEFAULT_VOCABULARY)

    def test_a_duplicate_action_name_is_refused(self):
        from driver_core.actions import Action
        dup = Action("x", READ_ONLY, "e", (), "", "any")
        with self.assertRaises(VocabularyError):
            Vocabulary("v", "1", [dup, dup])


class PerceptionTests(unittest.TestCase):

    def test_a_fingerprint_does_not_leak_the_payload(self):
        payload = {"window_title": "Confidential Payroll 2026"}
        digest = fingerprint(payload)
        self.assertNotIn("Payroll", digest)
        self.assertEqual(len(digest), 16)

    def test_an_undeclared_source_name_is_refused(self):
        with self.assertRaises(Exception) as ctx:
            StructuredSource("telepathy", lambda t: None)
        self.assertIn("unknown structured source", str(ctx.exception))

    def test_a_declined_source_produces_a_named_capture_not_a_crash(self):
        source = StructuredSource("cli", lambda target: None)
        capture = source.capture("t")
        self.assertFalse(capture.ok)
        self.assertIn("nothing", capture.detail)


if __name__ == "__main__":
    unittest.main()
