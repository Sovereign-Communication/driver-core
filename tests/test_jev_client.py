"""Decision-client tests: the fail-closed contract and honest spend.

The theme is that every degraded path is *distinguishable* and *cheap*. A
response that named an undeclared action, a response with no distribution,
a transport failure and a missing key are four different situations, and a
caller needs to tell them apart -- both to react correctly and to be able to
say afterwards what actually happened.
"""
import unittest

from driver_core.actions import DEFAULT_VOCABULARY
from driver_core.audit import MemoryAuditLog
from driver_core.budget import Budget
from driver_core.config import JEV_INPUT_PRICE_PER_MILLION, load_settings
from driver_core.errors import VocabularyError
from driver_core.executor_registry import build_driver_registry
from driver_core.jev_client import (
    COARSEST_INFERRED_DECIMAL_PLACE, FLOAT_NOISE_TOLERANCE,
    MALFORMED, MAX_ACCEPTED_DEVIATION, NATIVE, UNAVAILABLE, UNKEYED,
    JevClient, accepted_deviation, build_questions, inferred_grid,
    offered_actions, probability_deviation, validate_answers,
)
from driver_core.transport import Response
from driver_core.ev import (
    FakeTransport, action_answer, no_usage_response, ok_response,
)

STATE = {"window_title": "report - editor", "foreground_app": "editor",
         "error_dialog_present": False}


def _answers(action="observe", confidence=0.9, **over):
    """A realistic response body.

    The distribution covers *every* declared option, not just the chosen one
    -- which is what the real endpoint returns, and what makes the
    "sums to 1" check meaningful. A body that listed only the winner would be
    rejected, correctly: a distribution missing the alternatives cannot
    express the confidence the model claims.
    """
    options = DEFAULT_VOCABULARY.names()
    others = [name for name in options if name != action]
    share = (1.0 - confidence) / len(others) if others else 0.0
    distribution = {name: (confidence if name == action else share)
                    for name in options}
    answer = {
        "action": {"type": "choice", "choice": action,
                   "probabilities": distribution,
                   "confidence": confidence},
        "state_is_stable": {"type": "noul", "noul": 0.95},
        "a_blocking_choice_is_required": {"type": "noul", "noul": 0.2},
    }
    answer.update(over)
    return answer


class ValidationTests(unittest.TestCase):

    def _validate(self, answers):
        questions = build_questions(STATE, DEFAULT_VOCABULARY)
        return validate_answers(answers, questions, DEFAULT_VOCABULARY)

    def test_a_well_formed_response_validates(self):
        action, confidence, probs, guards, reasons = self._validate(
            _answers())
        self.assertEqual(reasons, [])
        self.assertEqual(action, "observe")
        self.assertAlmostEqual(confidence, 0.9)
        self.assertEqual(guards["state_is_stable"], 0.95)

    def test_an_undeclared_action_is_refused(self):
        action, _, _, _, reasons = self._validate(_answers("rm_rf"))
        self.assertIsNone(action)
        self.assertTrue(any("not a declared action" in r for r in reasons))

    def test_a_missing_guard_is_a_refusal_not_a_default(self):
        answers = _answers()
        del answers["state_is_stable"]
        action, _, _, guards, reasons = self._validate(answers)
        self.assertIsNone(action)
        self.assertIn("missing answer(s) ['state_is_stable']", reasons)

    def test_a_probability_distribution_that_does_not_sum_to_one_is_refused(self):
        answers = _answers()
        answers["action"]["probabilities"] = {"observe": 0.9, "click": 0.9}
        action, _, _, _, reasons = self._validate(answers)
        self.assertIsNone(action)
        self.assertTrue(any("not 1" in r for r in reasons))

    def test_a_chosen_option_absent_from_its_own_distribution_is_refused(self):
        """A response that names an option its own distribution does not
        contain is internally inconsistent, and its confidence cannot be
        trusted."""
        answers = _answers()
        answers["action"]["choice"] = "click"
        answers["action"]["probabilities"] = {
            k: v for k, v in answers["action"]["probabilities"].items()
            if k != "click"}
        action, _, _, _, reasons = self._validate(answers)
        self.assertIsNone(action)
        self.assertTrue(any("absent from the distribution" in r for r in reasons))

    def test_a_confidence_outside_the_unit_interval_is_refused(self):
        answers = _answers()
        answers["action"]["confidence"] = 1.4
        action, _, _, _, reasons = self._validate(answers)
        self.assertIsNone(action)
        self.assertTrue(any("outside [0, 1]" in r for r in reasons))

    def test_a_type_mismatch_is_refused(self):
        answers = _answers()
        answers["state_is_stable"] = {"type": "noul", "noul": "high"}
        action, _, _, _, reasons = self._validate(answers)
        self.assertIsNone(action)
        self.assertTrue(reasons)

    def test_a_partial_response_yields_nothing_at_all(self):
        """All-or-nothing: a partial answer returns no action, not a partial."""
        answers = _answers()
        del answers["a_blocking_choice_is_required"]
        action, confidence, _, _, reasons = self._validate(answers)
        self.assertIsNone(action)
        self.assertIsNone(confidence)
        self.assertTrue(reasons)


class ClientPathTests(unittest.TestCase):

    def _client(self, *responses, key="k", ceiling=1.0, settings=None):
        settings = settings or load_settings(env={}, jev_api_key=key)
        self.audit = MemoryAuditLog()
        self.budget = Budget(ceiling, step_ceiling_usd=ceiling)
        return JevClient(settings, budget=self.budget, audit=self.audit,
                         transport_module=FakeTransport(*responses))

    def test_a_keyed_call_settles_once_and_records_once(self):
        client = self._client(ok_response(_answers(), input_tokens=1000))
        decision = client.decide(STATE, DEFAULT_VOCABULARY, step_id="s1")
        self.assertEqual(decision.status, NATIVE)
        self.assertTrue(decision.usable)
        self.assertEqual(decision.usage_source, "actual")
        # Priced through the constant rather than a literal, so a future rate
        # correction lands here automatically instead of leaving a test that
        # pins a stale number and calls it a pass.
        self.assertAlmostEqual(
            decision.cost,
            1000 * JEV_INPUT_PRICE_PER_MILLION / 1_000_000)
        self.assertEqual(len(self.audit.read_all()), 1)
        self.assertAlmostEqual(self.budget.spent, decision.cost)
        self.assertEqual(self.budget.reserved, 0.0)

    def test_an_unkeyed_client_never_dispatches(self):
        client = self._client(ok_response(_answers()), key="")
        decision = client.decide(STATE, DEFAULT_VOCABULARY)
        self.assertEqual(decision.status, UNKEYED)
        self.assertFalse(decision.native)
        self.assertIsNone(decision.recommended_action)
        self.assertEqual(self.budget.spent, 0.0)
        self.assertEqual(len(self.audit.read_all()), 1)

    def test_a_transport_failure_charges_the_reservation_not_zero(self):
        client = self._client(Response(0, "transport_error", detail="down"))
        decision = client.decide(STATE, DEFAULT_VOCABULARY)
        self.assertEqual(decision.status, UNAVAILABLE)
        self.assertGreater(decision.cost, 0.0)
        self.assertEqual(decision.usage_source, "unavailable")
        self.assertAlmostEqual(self.budget.spent, decision.cost)

    def test_a_response_with_no_usage_is_charged_the_full_reservation(self):
        """The load-bearing spend rule: a cost we cannot read is charged at
        the estimate, never at zero."""
        client = self._client(no_usage_response(_answers()))
        decision = client.decide(STATE, DEFAULT_VOCABULARY)
        self.assertEqual(decision.status, NATIVE)
        self.assertEqual(decision.usage_source, "unavailable")
        self.assertGreater(decision.cost, 0.0)

    def test_a_malformed_response_settles_the_call_it_still_made(self):
        """A billed call whose answer was unusable is still billed, and
        flagged so a coverage report can be honest about it."""
        client = self._client(ok_response(_answers(action="nonsense"),
                                          input_tokens=500))
        decision = client.decide(STATE, DEFAULT_VOCABULARY)
        self.assertEqual(decision.status, MALFORMED)
        self.assertFalse(decision.usable)
        self.assertIsNone(decision.recommended_action)
        self.assertGreater(decision.cost, 0.0)

    def test_a_budget_refusal_dispatches_nothing_and_records_a_refusal(self):
        client = self._client(ok_response(_answers()), ceiling=1e-9)
        decision = client.decide(STATE, DEFAULT_VOCABULARY)
        self.assertEqual(decision.status, UNAVAILABLE)
        self.assertIn("budget refused", decision.stop_reason)
        record = self.audit.read_all()[0]
        self.assertEqual(record["kind"], "decision")
        self.assertEqual(record["status"], UNAVAILABLE)

    def test_every_path_writes_exactly_one_record(self):
        """No silent holes in the ledger, including on the failure paths."""
        cases = [
            self._client(ok_response(_answers())),
            self._client(Response(500, "http_error", detail="boom")),
            self._client(Response(0, "transport_error")),
            self._client(no_usage_response(_answers(action="bogus"))),
            self._client(ok_response(_answers()), key=""),
        ]
        for client in cases:
            client.decide(STATE, DEFAULT_VOCABULARY, step_id="s")
            self.assertEqual(len(client.audit.read_all()), 1)

    def test_the_audit_record_carries_metadata_not_state(self):
        """A decision record must not become a transcript of the screen."""
        client = self._client(ok_response(_answers()))
        client.decide(STATE, DEFAULT_VOCABULARY)
        record = client.audit.read_all()[0]
        self.assertNotIn("state", record)
        self.assertNotIn("window_title", str(record))
        self.assertEqual(record["recommended_action"], "observe")


class EnvelopeTests(unittest.TestCase):

    def test_usable_requires_every_guard_to_have_answered(self):
        decision = action_answer("observe", confidence=0.9,
                                 guards={"state_is_stable": 1.0})
        self.assertFalse(decision.usable)

    def test_unusable_envelope_exposes_no_confidence(self):
        from driver_core.jev_client import Decision, UNAVAILABLE
        decision = Decision(UNAVAILABLE)
        self.assertFalse(decision.usable)
        self.assertIsNone(decision.confidence)
        self.assertIsNone(decision.recommended_action)


class OfferedActionTests(unittest.TestCase):
    """Only what this build can actually perform is ever offered.

    Two sets are in play and only one is safe to show: the vocabulary
    declares what the system can *name*, the registry holds what this build
    can *do*. Offering the vocabulary alone is how a real model came to
    choose ``read_dom`` at 0.98 confidence and ``run_probe`` at 1.00, only
    for the step to end in a refusal naming an executor nothing registered.

    The declaration is untouched either way -- a host that backs one of the
    three through ``Driver.register_executor`` puts it straight back on the
    menu, which is the extension point the refusal message already promises.
    """

    def setUp(self):
        self.registry = build_driver_registry(allow_write=True)

    def test_the_offered_set_is_exactly_what_the_registry_can_perform(self):
        offered = offered_actions(DEFAULT_VOCABULARY, self.registry.names())
        self.assertEqual(set(offered), set(self.registry.names()))
        self.assertEqual(len(offered), 11)
        self.assertEqual(len(DEFAULT_VOCABULARY.names()), 14)

    def test_the_three_names_nothing_implements_are_withheld(self):
        offered = offered_actions(DEFAULT_VOCABULARY, self.registry.names())
        for name in ("run_probe", "call_read_tool", "read_dom"):
            self.assertIn(name, DEFAULT_VOCABULARY.names())
            self.assertNotIn(name, offered)

    def test_without_a_registry_nothing_is_narrowed(self):
        """A caller holding no registry gets exactly what it always got, so
        nothing that exists today changes shape."""
        self.assertEqual(offered_actions(DEFAULT_VOCABULARY),
                         DEFAULT_VOCABULARY.names())

    def test_the_question_offers_exactly_the_narrowed_set(self):
        questions = build_questions(STATE, DEFAULT_VOCABULARY,
                                   performable=self.registry.names())
        self.assertEqual(set(questions["action"]["criteria"]),
                         set(self.registry.names()))

    def test_a_registry_that_can_perform_nothing_declared_is_refused(self):
        """An empty option set is not a question. Refused by name rather than
        left to surface as a bare ValueError from the question builder."""
        with self.assertRaises(VocabularyError):
            offered_actions(DEFAULT_VOCABULARY, ("not_a_declared_action",))

    def test_a_host_can_put_a_withheld_action_back_on_the_menu(self):
        registry = self.registry.clone()
        registry.register("run_probe", lambda action, params: {})
        self.assertIn("run_probe",
                      offered_actions(DEFAULT_VOCABULARY, registry.names()))


#: A distribution exactly as a live eleven-option call returned it: every
#: entry on a hundredth, and summing to 0.99 because the residue was lost.
#: Captured verbatim from the real endpoint during the live audit.
OBSERVED_ROUNDED = {
    "click": 0.03, "delete_file": 0.0, "focus": 0.01, "no_action": 0.79,
    "observe": 0.14, "press_key": 0.0, "read_value": 0.02, "scroll": 0.0,
    "submit_irreversible": 0.0, "type_text": 0.0, "write_file": 0.0,
}


def _rounded_answers(probabilities, chosen, confidence):
    return {
        "action": {"type": "choice", "choice": chosen,
                   "probabilities": dict(probabilities),
                   "confidence": confidence},
        "state_is_stable": {"type": "noul", "noul": 0.95},
        "a_blocking_choice_is_required": {"type": "noul", "noul": 0.2},
    }


class DistributionDerivationTests(unittest.TestCase):
    """The tolerance is read off the values, not chosen."""

    def test_a_hundredth_grid_gets_half_a_hundredth_per_entry(self):
        self.assertEqual(inferred_grid({"a": 0.99, "b": 0.01}), 2)
        self.assertAlmostEqual(accepted_deviation({"a": 0.99, "b": 0.01}),
                               0.01)

    def test_whole_numbers_cannot_imply_that_a_hundredth_is_the_grid(self):
        self.assertEqual(inferred_grid({"a": 1.0, "b": 0.0}),
                         COARSEST_INFERRED_DECIMAL_PLACE)
        self.assertAlmostEqual(accepted_deviation({"a": 1.0, "b": 0.0}), 0.1)

    def test_full_precision_gets_no_tolerance_beyond_float_noise(self):
        """Full precision means the sum is meant to be exactly 1."""
        repeating = {"a": (1.0 - 0.99) / 13, "b": 0.99}
        self.assertAlmostEqual(accepted_deviation(repeating),
                               FLOAT_NOISE_TOLERANCE)

    def test_the_tolerance_is_capped_however_coarse_the_grid(self):
        """A very coarse grid must not license an unbounded deviation."""
        coarse = {str(i): 0.1 for i in range(200)}
        self.assertEqual(accepted_deviation(coarse), MAX_ACCEPTED_DEVIATION)

    def test_an_empty_distribution_cannot_widen_anything(self):
        self.assertGreater(accepted_deviation({}), 0.0)
        self.assertLessEqual(accepted_deviation({}), MAX_ACCEPTED_DEVIATION)

    def test_the_deviation_is_the_distance_from_one(self):
        self.assertAlmostEqual(probability_deviation({"a": 0.5}), -0.5)
        self.assertAlmostEqual(probability_deviation({"a": 1.0}), 0.0)
        self.assertIsNone(probability_deviation({"a": "nonsense"}))


class RoundedDistributionTests(unittest.TestCase):
    """A distribution short by its own printed precision is still a decision.

    The endpoint documents that the distribution sums to 1, and publishes
    every worked example rounded to two decimal places. At eleven options
    those two documented facts stop being simultaneously satisfiable: live
    calls landed exactly 0.01 short on nine of twenty, then seven of sixteen,
    and the superseded fixed 1e-3 bound discarded the whole envelope each
    time. Because a discarded envelope is unusable, the cost of that bound
    was a step that acted on nothing while holding a perfectly good
    ``no_action`` at 0.79 confidence.
    """

    def setUp(self):
        self.offered = build_driver_registry(allow_write=True).names()
        self.questions = build_questions(STATE, DEFAULT_VOCABULARY,
                                        performable=self.offered)

    def _validate(self, probabilities, chosen="no_action", confidence=0.79):
        return validate_answers(
            _rounded_answers(probabilities, chosen, confidence),
            self.questions, DEFAULT_VOCABULARY)

    def test_the_observed_rounded_distribution_now_validates(self):
        action, confidence, probabilities, guards, reasons = self._validate(
            OBSERVED_ROUNDED)
        self.assertEqual(reasons, [])
        self.assertEqual(action, "no_action")
        self.assertEqual(set(probabilities), set(self.offered))
        self.assertNotIn("run_probe", probabilities)
        self.assertAlmostEqual(confidence, 0.79)
        self.assertIn("state_is_stable", guards)

    def test_a_distribution_short_beyond_its_grid_is_still_refused(self):
        """The rule is unchanged; only the bound moved with the precision."""
        wrong = dict(OBSERVED_ROUNDED)
        wrong["no_action"] = 0.59
        action, _, _, _, reasons = self._validate(wrong)
        self.assertIsNone(action)
        self.assertTrue(any("not 1" in reason for reason in reasons), reasons)
        self.assertTrue(any("precision" in reason for reason in reasons),
                        reasons)

    def test_the_superseded_case_is_still_refused(self):
        action, _, _, _, reasons = self._validate({"observe": 0.9,
                                                   "click": 0.9},
                                                  chosen="observe",
                                                  confidence=0.9)
        self.assertIsNone(action)
        self.assertTrue(any("not 1" in reason for reason in reasons))

    def test_the_record_names_the_deviation_it_tolerated(self):
        settings = load_settings(env={"DRIVER_JEV_API_KEY": "k"})
        audit = MemoryAuditLog()
        client = JevClient(settings, budget=Budget(1.0, step_ceiling_usd=1.0),
                           audit=audit,
                           transport_module=FakeTransport(ok_response(
                               _rounded_answers(OBSERVED_ROUNDED,
                                                "no_action", 0.79))))
        decision = client.decide(STATE, DEFAULT_VOCABULARY,
                                 performable=self.offered)
        self.assertTrue(decision.usable)
        self.assertEqual(decision.status, NATIVE)
        self.assertAlmostEqual(decision.probability_deviation, -0.01)
        self.assertAlmostEqual(decision.to_dict()["probability_deviation"],
                               -0.01)
        record = audit.read_all()[0]
        self.assertAlmostEqual(record["probability_deviation"], -0.01)

    def test_an_exact_distribution_records_no_deviation(self):
        settings = load_settings(env={"DRIVER_JEV_API_KEY": "k"})
        audit = MemoryAuditLog()
        exact = dict(OBSERVED_ROUNDED)
        exact["no_action"] = 0.80
        client = JevClient(settings, budget=Budget(1.0, step_ceiling_usd=1.0),
                           audit=audit,
                           transport_module=FakeTransport(ok_response(
                               _rounded_answers(exact, "no_action", 0.80))))
        decision = client.decide(STATE, DEFAULT_VOCABULARY,
                                 performable=self.offered)
        self.assertTrue(decision.usable)
        self.assertEqual(decision.probability_deviation, 0.0)


if __name__ == "__main__":
    unittest.main()
