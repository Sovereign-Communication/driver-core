"""The audit chain is a compatibility surface, and this is the test for it.

An audit log is the one artifact whose whole value is that a record means one
thing and cannot be quietly reinterpreted later. A refactor that renames a
record kind, adds a field, or changes the order records are written in makes
every log already on disk mean something subtly different -- and unlike a
response shape, nobody is watching for it.

Two claims are checked, and they are different claims:

* **An existing log still verifies.** ``tests/data/audit_v1.jsonl`` is a real
  chain, written by the code as it stood before the record-kind vocabulary was
  given an owner. It must re-hash clean under the current code.
* **The same run still writes the same bytes.** The scenario below pins the
  clock and every ``step_id``, so the records it produces are a pure function
  of the code. They are compared against a golden recorded at the same moment
  as the fixture.

The second is the stronger claim and the one that catches a renamed constant.
A log can verify perfectly while meaning something new.
"""
import ast
import json
import os
import tempfile
import unittest
from unittest import mock

import driver_core.audit as audit_module
from driver_core.actions import DEFAULT_VOCABULARY
from driver_core.audit import (
    KIND_ACTION, KIND_CAPTURE, KIND_DECISION, KIND_ESCALATION, KIND_EXTRACTION,
    KIND_REFUSAL, AuditLog,
)
from driver_core.budget import Budget
from driver_core.config import load_settings
from driver_core.driver import Driver
from driver_core.ev import FakeTransport, ok_response
from driver_core.extractors import ExtractorPool, StructuredExtractor
from driver_core.jev_client import JevClient
from driver_core.perception import CLI, StructuredSource, Target
from driver_core.states import CLI_SCHEMA


#: Every kind the driver can actually produce. If this and the fixture
#: disagree, one of them is lying, and the test comparing them notices.
LIVE_KINDS = (KIND_CAPTURE, KIND_EXTRACTION, KIND_DECISION, KIND_ESCALATION,
              KIND_REFUSAL, KIND_ACTION)

#: The modules that write records. Read as source, because the claim is about
#: literals surviving in code rather than about a call graph.
#: A chain recorded by the code as it stood **before** the record-kind
#: vocabulary was given an owner. It is inline rather than a data file on
#: purpose: ``.gitignore`` refuses ``*.jsonl`` because an audit log is
#: evidence about a machine and must never be committed, and weakening
#: that rule -- or renaming the file to dodge it -- would be worse than
#: carrying the records here. It also means a change to the chain shows
#: up in the diff beside the test that explains it.
#:
#: One JSON object per line, exactly as the log stores them, wrapped to
#: stay inside the line length. Regenerate with
#: ``python tests/test_audit_compat.py --write-golden``.
GOLDEN_CHAIN = (
    (
     "{\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"detail\":\"target 'unobser"
     "vable' is declared 'cli' and no configured source serves that clas"
     "s; nothing was tried. Configured sources: ['none'].\",\"hash\":\"98c94"
     "270f43a33f50f308df7fad8290836267bb9f7b504e02aee5056d59bd982\",\"kind"
     "\":\"refusal\",\"previous\":\"driver-core/audit/v1\",\"reason\":\"no_capture"
     "\",\"seq\":0,\"step_id\":\"step000000001\",\"stopped_at\":\"capture\"}"
    ),
    (
     "{\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"detail\":\"\",\"fingerprint\""
     ":\"45475af213f2b5ea\",\"hash\":\"7218b26519ca0fffe828626465a02b2d97f46e"
     "5423089fce1e6ecfb781f14da3\",\"kind\":\"capture\",\"ok\":true,\"previous\":"
     "\"98c94270f43a33f50f308df7fad8290836267bb9f7b504e02aee5056d59bd982\""
     ",\"seq\":1,\"source\":\"cli\",\"step_id\":\"step000000002\",\"target\":\"observ"
     "ed\"}"
    ),
    (
     "{\"agreed_fields\":[\"exit_code\",\"stdout\",\"stderr\"],\"answering\":2,\"as"
     "ked\":2,\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"contested_fields\":"
     "[],\"cost\":0.0,\"extraction_id\":\"step000000002\",\"hash\":\"e9ceec17fd3e"
     "853a7e465f37baec2c5812140ebd144da1c45854a06b9590de8c\",\"kind\":\"extr"
     "action\",\"outcome\":\"agreed\",\"previous\":\"7218b26519ca0fffe828626465a"
     "02b2d97f46e5423089fce1e6ecfb781f14da3\",\"schema\":\"driver-core-cli@1"
     ".0.0\",\"seq\":2,\"step_id\":\"step000000002\",\"unanswered\":[]}"
    ),
    (
     "{\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"confidence\":0.1,\"cost_us"
     "d\":4.2e-07,\"extraction\":{\"agreed_fields\":[\"exit_code\",\"stdout\",\"st"
     "derr\"],\"answering\":2,\"asked\":2,\"contested_fields\":[],\"cost\":0.0,\"e"
     "xtraction_id\":\"step000000002\",\"outcome\":\"agreed\",\"schema\":\"driver-"
     "core-cli@1.0.0\",\"unanswered\":[]},\"guards\":{\"a_blocking_choice_is_r"
     "equired\":0.2,\"state_is_stable\":0.95},\"hash\":\"e32de8630538d36c81984"
     "551c1535e1faf563170c7fd0c90d030bb14a3e54639\",\"kind\":\"decision\",\"mo"
     "del\":\"fake-jev\",\"native\":true,\"previous\":\"e9ceec17fd3e853a7e465f37"
     "baec2c5812140ebd144da1c45854a06b9590de8c\",\"probabilities\":{\"call_r"
     "ead_tool\":0.06923076923076923,\"click\":0.06923076923076923,\"delete_"
     "file\":0.06923076923076923,\"focus\":0.06923076923076923,\"no_action\":"
     "0.06923076923076923,\"observe\":0.06923076923076923,\"press_key\":0.06"
     "923076923076923,\"read_dom\":0.06923076923076923,\"read_value\":0.0692"
     "3076923076923,\"run_probe\":0.06923076923076923,\"scroll\":0.069230769"
     "23076923,\"submit_irreversible\":0.06923076923076923,\"type_text\":0.0"
     "6923076923076923,\"write_file\":0.1},\"recommended_action\":\"write_fil"
     "e\",\"seq\":3,\"status\":\"native\",\"step_id\":\"step000000002\",\"usage_sour"
     "ce\":\"actual\"}"
    ),
    (
     "{\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"detail\":\"confidence 0.1 "
     "is below the 0.7 threshold\",\"hash\":\"284cc38a8b8cfa74e7dae5a3d1bbf1"
     "206565b65a55676b56534c8925db94bf6a\",\"kind\":\"escalation\",\"previous\""
     ":\"e32de8630538d36c81984551c1535e1faf563170c7fd0c90d030bb14a3e54639"
     "\",\"reason\":\"confidence_below_threshold\",\"seq\":4,\"step_id\":\"step000"
     "000002\"}"
    ),
    (
     "{\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"detail\":\"confidence 0.1 "
     "is below the 0.7 threshold\",\"hash\":\"823a477b791038411857da9fa02553"
     "4853d7447487af33927b150ba69331191f\",\"kind\":\"refusal\",\"previous\":\"2"
     "84cc38a8b8cfa74e7dae5a3d1bbf1206565b65a55676b56534c8925db94bf6a\",\""
     "reason\":\"confidence_below_threshold\",\"seq\":5,\"step_id\":\"step000000"
     "002\",\"stopped_at\":\"decision\"}"
    ),
    (
     "{\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"detail\":\"\",\"fingerprint\""
     ":\"45475af213f2b5ea\",\"hash\":\"46740e756e2a7eb5b0c0fcb8b17e4f054c47b1"
     "64e24d82a4ab7efc8c47f41252\",\"kind\":\"capture\",\"ok\":true,\"previous\":"
     "\"823a477b791038411857da9fa025534853d7447487af33927b150ba69331191f\""
     ",\"seq\":6,\"source\":\"cli\",\"step_id\":\"step000000003\",\"target\":\"observ"
     "ed\"}"
    ),
    (
     "{\"agreed_fields\":[\"exit_code\",\"stdout\",\"stderr\"],\"answering\":2,\"as"
     "ked\":2,\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"contested_fields\":"
     "[],\"cost\":0.0,\"extraction_id\":\"step000000003\",\"hash\":\"aa39c5682c71"
     "b2a87a63509068be101290ecb313f96b90127e60a63f15b6f7df\",\"kind\":\"extr"
     "action\",\"outcome\":\"agreed\",\"previous\":\"46740e756e2a7eb5b0c0fcb8b17"
     "e4f054c47b164e24d82a4ab7efc8c47f41252\",\"schema\":\"driver-core-cli@1"
     ".0.0\",\"seq\":7,\"step_id\":\"step000000003\",\"unanswered\":[]}"
    ),
    (
     "{\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"confidence\":0.99,\"cost_u"
     "sd\":4.2e-07,\"extraction\":{\"agreed_fields\":[\"exit_code\",\"stdout\",\"s"
     "tderr\"],\"answering\":2,\"asked\":2,\"contested_fields\":[],\"cost\":0.0,\""
     "extraction_id\":\"step000000003\",\"outcome\":\"agreed\",\"schema\":\"driver"
     "-core-cli@1.0.0\",\"unanswered\":[]},\"guards\":{\"a_blocking_choice_is_"
     "required\":0.2,\"state_is_stable\":0.95},\"hash\":\"e47fbdf380195fbeb95f"
     "e342b475924347eeb83d20a81d0f9cf3e1a26016441b\",\"kind\":\"decision\",\"m"
     "odel\":\"fake-jev\",\"native\":true,\"previous\":\"aa39c5682c71b2a87a63509"
     "068be101290ecb313f96b90127e60a63f15b6f7df\",\"probabilities\":{\"call_"
     "read_tool\":0.0007692307692307699,\"click\":0.0007692307692307699,\"de"
     "lete_file\":0.0007692307692307699,\"focus\":0.0007692307692307699,\"no"
     "_action\":0.0007692307692307699,\"observe\":0.99,\"press_key\":0.000769"
     "2307692307699,\"read_dom\":0.0007692307692307699,\"read_value\":0.0007"
     "692307692307699,\"run_probe\":0.0007692307692307699,\"scroll\":0.00076"
     "92307692307699,\"submit_irreversible\":0.0007692307692307699,\"type_t"
     "ext\":0.0007692307692307699,\"write_file\":0.0007692307692307699},\"re"
     "commended_action\":\"observe\",\"seq\":8,\"status\":\"native\",\"step_id\":\"s"
     "tep000000003\",\"usage_source\":\"actual\"}"
    ),
    (
     "{\"action\":\"observe\",\"action_class\":\"read_only\",\"at\":\"2026-01-01T00"
     ":00:00.000000+00:00\",\"consent\":null,\"detail\":\"\",\"dry_run\":false,\"h"
     "ash\":\"3eb75644eb0d51f5c062b6f11dadd766ccd2247d0e481c96316668497702"
     "70e9\",\"kind\":\"action\",\"ok\":true,\"previous\":\"e47fbdf380195fbeb95fe3"
     "42b475924347eeb83d20a81d0f9cf3e1a26016441b\",\"seq\":9,\"step_id\":\"ste"
     "p000000003\"}"
    ),
)

PRODUCERS = ("driver", "executor", "extractors", "jev_client")

FIXED_NOW = "2026-01-01T00:00:00.000000+00:00"


def _capture(target):
    """A structured capture with nothing random in it."""
    return {"exit_code": 0, "stdout": f"observed {target}", "stderr": ""}


def _reader(capture, schema):
    return dict(capture.payload)


def _answers(action, confidence):
    """A response body covering every declared option, as the real one does."""
    options = DEFAULT_VOCABULARY.names()
    others = [name for name in options if name != action]
    share = (1.0 - confidence) / len(others) if others else 0.0
    return {
        "action": {"type": "choice", "choice": action,
                   "probabilities": {
                       name: (confidence if name == action else share)
                       for name in options},
                   "confidence": confidence},
        "state_is_stable": {"type": "noul", "noul": 0.95},
        "a_blocking_choice_is_required": {"type": "noul", "noul": 0.2},
    }


def _driver(workdir, responses, *, blind=False):
    """A driver whose decision tier is the real client on a fake transport.

    :class:`~driver_core.ev.FakeJev` would be shorter, but it writes no
    ``decision`` record at all -- it is not the client that ships. Using the
    real one is what puts the fifth record kind in the fixture.
    """
    settings = load_settings(
        env={}, jev_api_key="k",
        quorum=2, min_agreement=1.0, confidence_threshold=0.7,
        run_ceiling_usd=1.0, step_ceiling_usd=1.0, allow_write=True,
    )
    budget = Budget(1.0, step_ceiling_usd=1.0)
    audit = AuditLog(os.path.join(workdir, "audit.jsonl"))
    return Driver(
        settings=settings,
        budget=budget,
        audit=audit,
        pool=ExtractorPool([StructuredExtractor(f"cli-{i}", _reader)
                            for i in range(2)]),
        jev=JevClient(settings, budget=budget, audit=audit,
                      transport_module=FakeTransport(*responses)),
        sources=[] if blind else [StructuredSource("cli", _capture)],
    )


def run_scenario(workdir):
    """Every path that writes a record, deterministically.

    Three steps, chosen to cover all six live kinds:

    * an unobservable target -> a refusal and nothing else;
    * a decision below the threshold -> capture, extraction, decision,
      escalation, refusal;
    * a decision above it, naming a read-only action -> capture, extraction,
      decision, action.

    The third step deliberately names ``observe``, which is read-only and
    takes no parameters, rather than a mutating action. A mutating action
    would put an absolute path in its record -- and into the hash of that
    record -- so the golden would carry whichever temporary directory
    generated it and could not be compared on any other machine. A golden that
    only verifies on the machine that wrote it is not a golden.
    """
    previous = os.getcwd()
    os.chdir(workdir)
    try:
        _driver(workdir, [], blind=True).step(
            Target("unobservable", CLI), schema=CLI_SCHEMA,
            step_id="step000000001")

        _driver(workdir, [ok_response(_answers("write_file", 0.10))]
                ).step(Target("observed", CLI), schema=CLI_SCHEMA,
                       step_id="step000000002")

        _driver(workdir, [ok_response(_answers("observe", 0.99))]
                ).step(Target("observed", CLI), schema=CLI_SCHEMA,
                       step_id="step000000003")
    finally:
        os.chdir(previous)

    return AuditLog(os.path.join(workdir, "audit.jsonl"))


def golden_records():
    return [json.loads(record) for record in GOLDEN_CHAIN]


def _folded(records, workdir=None):
    """Records with any machine-specific prefix replaced by a marker.

    The scenario is built to contain no such prefix -- see
    :func:`run_scenario` -- so this folds nothing today. It is here so that a
    future scenario which does acquire one fails with a readable diff rather
    than with a hash mismatch, and so the portability of the golden is
    checked rather than assumed.
    """
    out = json.loads(json.dumps(records))
    blob = json.dumps(out)
    for root in filter(None, (workdir, tempfile.gettempdir(),
                              os.path.expanduser("~"))):
        marker = json.dumps(root)[1:-1]
        if marker and marker in blob:
            blob = blob.replace(marker, "<root>")
    return json.loads(blob)


class ChainCompatibilityTests(unittest.TestCase):

    def _verify_round_trip(self):
        """Verify the recorded chain the way a host does: read from a file.

        The golden is a literal in this module, so writing it out and
        re-reading it is what makes the test a claim about *logs on disk*
        rather than about a list in memory.
        """
        with tempfile.TemporaryDirectory() as workdir:
            path = os.path.join(workdir, "audit.jsonl")
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                for record in GOLDEN_CHAIN:
                    handle.write(record + "\n")
            return AuditLog(path).verify()

    def test_a_log_written_before_this_change_still_verifies(self):
        """The compatibility claim in its plainest form."""
        verdict = self._verify_round_trip()
        self.assertTrue(verdict.ok,
                        f"an existing log stopped verifying: {verdict}")

    def test_the_fixture_covers_every_kind_the_driver_can_produce(self):
        kinds = [r["kind"] for r in golden_records()]
        self.assertEqual(set(kinds), set(LIVE_KINDS))

    def test_the_same_run_writes_the_same_bytes(self):
        """Stronger than verification: identical records, not a valid chain.

        Every machine-specific prefix is folded to a marker first, so the
        comparison is about what the code *decides to record* rather than
        about where the temporary directory happened to land.
        """
        with tempfile.TemporaryDirectory() as workdir:
            with mock.patch("driver_core.audit._now", return_value=FIXED_NOW):
                produced = _folded(run_scenario(workdir).read_all(), workdir)
        self.assertEqual(produced, _folded(golden_records()))

    def test_the_records_are_actually_deterministic(self):
        """Without this, the test above could pass by comparing nothing."""
        runs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as workdir:
                with mock.patch("driver_core.audit._now",
                                return_value=FIXED_NOW):
                    records = run_scenario(workdir).read_all()
                runs.append(_folded(records, workdir))
        self.assertGreater(len(runs[0]), 5)
        self.assertEqual(runs[0], runs[1])

    def test_no_declared_kind_names_a_record_nothing_produces(self):
        declared = {name: value for name, value in vars(audit_module).items()
                    if name.startswith("KIND_")}
        self.assertEqual(
            sorted(declared.values()), sorted(set(LIVE_KINDS)),
            "a declared kind names a record nothing produces, or a produced "
            "record has no declared name")

    def test_every_record_is_appended_by_a_declared_name(self):
        """The other direction: a record written without a constant is a claim
        the log makes that nothing owns.

        Checked on the call sites rather than by searching for the strings.
        ``"capture"`` is also a ``__slots__`` entry, a key in ``to_dict`` and a
        tier name in ``stopped_at``, and none of those is a record kind -- a
        text search cannot tell them apart.
        """
        import importlib
        kinds = {name for name in dir(audit_module)
                 if name.startswith("KIND_")}
        found = []
        for module_name in PRODUCERS:
            path = importlib.import_module(
                f"driver_core.{module_name}").__file__
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute)
                        and func.attr == "append" and node.args):
                    continue
                receiver = func.value
                owner = (receiver.attr if isinstance(receiver, ast.Attribute)
                         else getattr(receiver, "id", None))
                if owner != "audit":
                    continue
                first = node.args[0]
                found.append(
                    f"driver_core/{module_name}.py:{node.lineno} "
                    + (f"appends the literal {first.value!r}"
                       if isinstance(first, ast.Constant)
                       else f"appends {ast.unparse(first)}"))
                if isinstance(first, ast.Name):
                    self.assertIn(first.id, kinds)
        self.assertEqual([f for f in found if "literal" in f], [],
                         "a record kind written as a bare string")

    def test_every_declared_kind_is_produced_by_the_scenario(self):
        """Ties the declaration to real behaviour, not to a list in a test."""
        produced = {r["kind"] for r in golden_records()}
        self.assertEqual(produced, set(LIVE_KINDS))


def _emit_golden_literal(records, width=66):
    """The recorded chain as a wrapped Python literal, for pasting back in.

    Wrapped because ``ruff`` selects ``E``, so a 700-character record on one
    line is a lint failure. The split never lands inside a ``\\`` escape, so
    the fragments concatenate back to exactly the recorded text.
    """
    lines = ["GOLDEN_CHAIN = ("]
    for record in records:
        text = json.dumps(record, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))
        fragments, current, escaped = [], "", False
        for ch in text:
            current += ch
            if ch == "\\" and not escaped:
                escaped = True
                continue
            escaped = False
            if len(current) >= width:
                fragments.append(current)
                current = ""
        if current:
            fragments.append(current)
        lines.append("    (")
        for fragment in fragments:
            escaped_text = fragment.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'     "{escaped_text}"')
        lines.append("    ),")
    lines.append(")")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    if "--write-golden" in sys.argv:
        # Run once, against the code as it stands, to record what the chain
        # looks like *now*. Never run in CI: a golden regenerated by the code
        # under test proves nothing.
        with tempfile.TemporaryDirectory() as workdir:
            with mock.patch("driver_core.audit._now", return_value=FIXED_NOW):
                records = run_scenario(workdir).read_all()
        kinds = {}
        for record in records:
            kinds[record["kind"]] = kinds.get(record["kind"], 0) + 1
        print(_emit_golden_literal(records))
        print(f"# {len(records)} records, kinds: {kinds}", file=sys.stderr)
        raise SystemExit(0)

    unittest.main()
