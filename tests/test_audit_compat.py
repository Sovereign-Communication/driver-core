"""The audit chain is a compatibility surface, and this is the test for it.

An audit log is the one artifact whose value is that a record means one thing
and cannot be quietly reinterpreted later. Two claims are checked, and they
are different claims:

* **An existing log still verifies.** ``RECORDED_CHAIN`` below is a real
  chain, recorded before the record-kind vocabulary was given an owner *and*
  before the Jev input price was corrected. It must re-hash clean under the
  current code.
* **The same run still writes the same bytes.** The scenario pins the clock
  and every ``step_id``, so what it produces is a pure function of the code,
  and it is compared against ``GOLDEN_CHAIN`` -- the same scenario, regenerated
  against the current code.

The second is the stronger claim and the one that catches a renamed constant.
A log can verify perfectly while meaning something new.

These two claims are served by **two different literals**, which they did not
used to be. Both were carried by one ``GOLDEN_CHAIN``, and correcting the Jev
price forced them apart: the correction moves every ``cost_usd`` a run writes,
so the byte-for-byte literal had to move, and regenerating it in place would
have quietly restated the first claim as "a log written by the current code
verifies" -- which asserts nothing at all. Keeping the old chain frozen costs
ten literal lines and buys a compatibility surface that did not exist before:
an operator's existing log, carrying costs from the superseded price, still
verifies.

The chain is inline rather than a ``.jsonl`` file on purpose: ``.gitignore``
refuses ``*.jsonl`` because an audit log is evidence about a machine and must
never be committed, and carrying it here also puts any change to it in the
diff beside the test that explains it. Regenerate with
``python -m tests.test_audit_compat --emit``.
"""
import ast
import importlib
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import driver_core.audit as audit_module
from driver_core.actions import DEFAULT_VOCABULARY
from driver_core.audit import AuditLog
from driver_core.budget import Budget
from driver_core.config import load_settings
from driver_core.driver import Driver
from driver_core.ev import FakeTransport, ok_response
from driver_core.extractors import ExtractorPool, StructuredExtractor
from driver_core.jev_client import JevClient
from driver_core.perception import CLI, StructuredSource, Target
from driver_core.states import CLI_SCHEMA

#: A chain recorded by an **earlier version** of this package, kept frozen.
#:
#: Originally this literal served both claims in the module docstring, and
#: that was one fixture doing two jobs. It no longer can: the Jev input rate
#: was corrected from $0.0042/Mtok to the operator-verified $0.042/Mtok, which
#: changes every ``cost_usd`` a run writes, so the byte-for-byte golden had to
#: move. Regenerating it in place would have quietly turned "a log written
#: before this change still verifies" into "a log written by the current code
#: verifies", which is a claim about nothing.
#:
#: So the old chain is frozen here permanently and the regenerated one is
#: :data:`GOLDEN_CHAIN`. That splits the fixture along the line the docstring
#: already draws, and it buys a real compatibility surface for free: this
#: recorded log carries 0.0042-era costs, so it now also proves that
#: correcting a price constant does not break verification of logs an operator
#: already has on disk. Re-hashing is over the record content, so the chain
#: still verifies under the current code.
#:
#: One JSON object per line, exactly as the log stores them, wrapped to stay
#: inside the line length.
RECORDED_CHAIN = (
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

#: The same scenario under the **current** code, regenerated after the Jev
#: input rate was corrected. This is the byte-for-byte determinism claim;
#: the "an older log still verifies" claim is :data:`RECORDED_CHAIN` above.
#: Regenerate with ``python -m tests.test_audit_compat --emit`` and never in
#: CI -- a chain produced by the code under test proves nothing about it.
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
     "d\":4.2e-06,\"extraction\":{\"agreed_fields\":[\"exit_code\",\"stdout\",\"st"
     "derr\"],\"answering\":2,\"asked\":2,\"contested_fields\":[],\"cost\":0.0,\"e"
     "xtraction_id\":\"step000000002\",\"outcome\":\"agreed\",\"schema\":\"driver-"
     "core-cli@1.0.0\",\"unanswered\":[]},\"guards\":{\"a_blocking_choice_is_r"
     "equired\":0.2,\"state_is_stable\":0.95},\"hash\":\"3f4a0e2d86603a03f4f15"
     "b7b97257fc0cba7f8939e593e3233cfc55522a2d62a\",\"kind\":\"decision\",\"mo"
     "del\":\"fake-jev\",\"native\":true,\"previous\":\"e9ceec17fd3e853a7e465f37"
     "baec2c5812140ebd144da1c45854a06b9590de8c\",\"probabilities\":{\"call_r"
     "ead_tool\":0.06923076923076923,\"click\":0.06923076923076923,\"delete_"
     "file\":0.06923076923076923,\"focus\":0.06923076923076923,\"no_action\":"
     "0.06923076923076923,\"observe\":0.06923076923076923,\"press_key\":0.06"
     "923076923076923,\"read_dom\":0.06923076923076923,\"read_value\":0.0692"
     "3076923076923,\"run_probe\":0.06923076923076923,\"scroll\":0.069230769"
     "23076923,\"submit_irreversible\":0.06923076923076923,\"type_text\":0.0"
     "6923076923076923,\"write_file\":0.1},\"probability_deviation\":0.0,\"re"
     "commended_action\":\"write_file\",\"seq\":3,\"status\":\"native\",\"step_id\""
     ":\"step000000002\",\"usage_source\":\"actual\"}"
    ),
    (
     "{\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"detail\":\"confidence 0.1 "
     "is below the 0.7 threshold\",\"hash\":\"9f7b2bb40849bce25c327509216405"
     "96e27b92e29d679c626f77f5fbe0296647\",\"kind\":\"escalation\",\"previous\""
     ":\"3f4a0e2d86603a03f4f15b7b97257fc0cba7f8939e593e3233cfc55522a2d62a"
     "\",\"reason\":\"confidence_below_threshold\",\"seq\":4,\"step_id\":\"step000"
     "000002\"}"
    ),
    (
     "{\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"detail\":\"confidence 0.1 "
     "is below the 0.7 threshold\",\"hash\":\"0d5bcbbd472c84b2dff21623e4e8ec"
     "bdadcdfd0156ad6afde14cceb77e1b00ed\",\"kind\":\"refusal\",\"previous\":\"9"
     "f7b2bb40849bce25c32750921640596e27b92e29d679c626f77f5fbe0296647\",\""
     "reason\":\"confidence_below_threshold\",\"seq\":5,\"step_id\":\"step000000"
     "002\",\"stopped_at\":\"decision\"}"
    ),
    (
     "{\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"detail\":\"\",\"fingerprint\""
     ":\"45475af213f2b5ea\",\"hash\":\"473cb4989936b3ad3e448be237bc9a5fdd3d71"
     "834a223d76217406489989c451\",\"kind\":\"capture\",\"ok\":true,\"previous\":"
     "\"0d5bcbbd472c84b2dff21623e4e8ecbdadcdfd0156ad6afde14cceb77e1b00ed\""
     ",\"seq\":6,\"source\":\"cli\",\"step_id\":\"step000000003\",\"target\":\"observ"
     "ed\"}"
    ),
    (
     "{\"agreed_fields\":[\"exit_code\",\"stdout\",\"stderr\"],\"answering\":2,\"as"
     "ked\":2,\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"contested_fields\":"
     "[],\"cost\":0.0,\"extraction_id\":\"step000000003\",\"hash\":\"c4eda45c689c"
     "dba33171860d4269ab02b9b4418d22f1f53bbd84bdf135fd6588\",\"kind\":\"extr"
     "action\",\"outcome\":\"agreed\",\"previous\":\"473cb4989936b3ad3e448be237b"
     "c9a5fdd3d71834a223d76217406489989c451\",\"schema\":\"driver-core-cli@1"
     ".0.0\",\"seq\":7,\"step_id\":\"step000000003\",\"unanswered\":[]}"
    ),
    (
     "{\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"confidence\":0.99,\"cost_u"
     "sd\":4.2e-06,\"extraction\":{\"agreed_fields\":[\"exit_code\",\"stdout\",\"s"
     "tderr\"],\"answering\":2,\"asked\":2,\"contested_fields\":[],\"cost\":0.0,\""
     "extraction_id\":\"step000000003\",\"outcome\":\"agreed\",\"schema\":\"driver"
     "-core-cli@1.0.0\",\"unanswered\":[]},\"guards\":{\"a_blocking_choice_is_"
     "required\":0.2,\"state_is_stable\":0.95},\"hash\":\"834b631eaf95b3bedb88"
     "969dfd119a3ed3a228b61037721b16df5eb3109558a3\",\"kind\":\"decision\",\"m"
     "odel\":\"fake-jev\",\"native\":true,\"previous\":\"c4eda45c689cdba33171860"
     "d4269ab02b9b4418d22f1f53bbd84bdf135fd6588\",\"probabilities\":{\"call_"
     "read_tool\":0.0007692307692307699,\"click\":0.0007692307692307699,\"de"
     "lete_file\":0.0007692307692307699,\"focus\":0.0007692307692307699,\"no"
     "_action\":0.0007692307692307699,\"observe\":0.99,\"press_key\":0.000769"
     "2307692307699,\"read_dom\":0.0007692307692307699,\"read_value\":0.0007"
     "692307692307699,\"run_probe\":0.0007692307692307699,\"scroll\":0.00076"
     "92307692307699,\"submit_irreversible\":0.0007692307692307699,\"type_t"
     "ext\":0.0007692307692307699,\"write_file\":0.0007692307692307699},\"pr"
     "obability_deviation\":0.0,\"recommended_action\":\"observe\",\"seq\":8,\"s"
     "tatus\":\"native\",\"step_id\":\"step000000003\",\"usage_source\":\"actual\"}"
    ),
    (
     "{\"action\":\"observe\",\"action_class\":\"read_only\",\"at\":\"2026-01-01T00"
     ":00:00.000000+00:00\",\"consent\":null,\"detail\":\"\",\"dry_run\":false,\"h"
     "ash\":\"2057cde216ee32e9debb2186dc44b8b846e5462aadcf575b5e99bfc1291a"
     "95c6\",\"kind\":\"action\",\"ok\":true,\"previous\":\"834b631eaf95b3bedb8896"
     "9dfd119a3ed3a228b61037721b16df5eb3109558a3\",\"seq\":9,\"step_id\":\"ste"
     "p000000003\"}"
    ),
)
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
    real one is what puts the fifth record kind in the recorded chain.
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


class ChainCompatibilityTests(unittest.TestCase):
    """The two compatibility claims, and the two guards on the vocabulary."""

    def _declared_kinds(self):
        return {value for name, value in vars(audit_module).items()
                if name.startswith("KIND_")}

    def _verify_recorded_chain(self):
        """Verify the recorded chain the way a host does: read it from a file.

        The chain is a literal in this module, so writing it out and reading
        it back is what makes the claim about *logs on disk* rather than about
        a list in memory. It verifies :data:`RECORDED_CHAIN`, which was
        written by an earlier version -- including under the superseded Jev
        price -- and must therefore still re-hash clean today.
        """
        with tempfile.TemporaryDirectory() as workdir:
            path = os.path.join(workdir, "audit.jsonl")
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                for record in RECORDED_CHAIN:
                    handle.write(record + "\n")
            return AuditLog(path).verify()

    def _run_once(self):
        with tempfile.TemporaryDirectory() as workdir:
            with mock.patch("driver_core.audit._now", return_value=FIXED_NOW):
                return run_scenario(workdir).read_all()

    def test_a_log_written_before_this_change_still_verifies(self):
        verdict = self._verify_recorded_chain()
        self.assertTrue(
            verdict.ok, f"an existing log stopped verifying: {verdict}")

    def test_the_recorded_chain_covers_every_declared_kind(self):
        kinds = {record["kind"] for record in golden_records()}
        self.assertEqual(kinds, self._declared_kinds())

    def test_the_same_run_writes_the_same_bytes(self):
        """Stronger than verification: identical records, not a valid chain.

        Run twice so the comparison cannot pass by luck -- one run agreeing
        with the chain says nothing about the next one doing the same.
        """
        for attempt in range(2):
            with self.subTest(run=attempt):
                self.assertEqual(self._run_once(), golden_records())

    def test_no_record_kind_is_written_as_a_string(self):
        """A record kind is a declared name, never a literal, wherever it is
        written -- in a module nobody listed, or through an alias.

        The scan covers the whole package and keys on the *shape* of the call
        rather than the name of its receiver, so neither a new producer nor a
        local ``log = self.audit`` can smuggle a string past it.

        The rule is that a string literal may only be appended when the call
        passes nothing by keyword: a record always carries fields, and the
        three places that append bare strings -- the problem messages in
        ``jev_client`` -- never do.
        """
        offenders = []
        for path, tree in _package():
            for node in _appends(tree):
                first = node.args[0]
                if (isinstance(first, ast.Constant) and isinstance(first.value, str)
                        and node.keywords):
                    offenders.append(
                        f"{path.name}:{node.lineno} appends the literal "
                        f"{first.value!r}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_vocabulary_is_declared_once_and_every_name_is_written(self):
        """Both directions of the same claim, and neither needs a list here.

        A declared name nothing writes is a claim about the log that can rot
        with nothing to notice it. So is a second copy of a name, which is
        why ``audit`` is the only module allowed to declare one: everyone
        else imports it. The producers are derived from the code -- every
        module that appends a ``KIND_*`` name -- so a new one is covered
        without anyone editing a list.
        """
        produced, redeclared = set(), []
        for path, tree in _package():
            for node in ast.walk(tree):
                if (path.stem != "audit" and isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name)
                                and target.id.startswith("KIND_")
                                for target in node.targets)):
                    redeclared.append(f"{path.name}:{node.lineno}")
            names = {node.args[0].id for node in _appends(tree)
                     if isinstance(node.args[0], ast.Name)
                     and node.args[0].id.startswith("KIND_")}
            if not names:
                continue
            module = importlib.import_module(
                "driver_core" if path.stem == "__init__"
                else f"driver_core.{path.stem}")
            produced.update(getattr(module, name) for name in names)
        self.assertEqual(
            redeclared, [], f"a KIND_* declared outside audit.py: {redeclared}")
        self.assertEqual(produced, self._declared_kinds())


def _package():
    """Every module in the package, parsed once, for the two scans above."""
    root = pathlib.Path(audit_module.__file__).parent
    for path in sorted(root.glob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def _appends(tree):
    """Every ``<anything>.append(<something>, ...)`` call in a parsed module.

    Deliberately ignorant of what is being appended to. Naming the receiver
    would miss an alias, and listing the modules would miss a new one.
    """
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append" and node.args):
            yield node


def _emit(records, width=66):
    """The chain as wrapped Python literals, for pasting over GOLDEN_CHAIN.

    Wrapped because ``ruff`` selects ``E``, so a 700-character record on one
    line is a lint failure. Dumping each fragment on its own is enough: the
    escaping is recomputed per fragment, so a split never lands inside one.
    """
    for record in records:
        text = json.dumps(record, sort_keys=True, separators=(",", ":"))
        print("    (")
        for start in range(0, len(text), width):
            print("     " + json.dumps(text[start:start + width]))
        print("    ),")


if __name__ == "__main__":
    import sys

    if "--emit" in sys.argv:
        # Run once, against the code as it stands, to record what the chain
        # looks like *now*. Never run in CI: a chain regenerated by the code
        # under test proves nothing about the code under test.
        with tempfile.TemporaryDirectory() as workdir:
            with mock.patch("driver_core.audit._now", return_value=FIXED_NOW):
                _emit(run_scenario(workdir).read_all())
