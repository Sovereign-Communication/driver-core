"""The per-action confidence ladder and the guardrail pack (Landing 5).

Three properties are pinned here.

**The ladder discriminates by consequence.** The same confidence passes for
an action that observes, reviews for one that changes recoverable state, and
blocks for one that cannot be undone -- and the rung is applied *after*
every pre-existing gate, so it can only raise the bar.

**The guardrail verdicts are code's, not the model's.** The pack supplies
probabilities; pass / review / block is computed here, fail-closed on any
missing or malformed answer, and it gates acting, never observing.

**Every declared threshold clears the observed jitter band.** §5b measured
Jev's confidence jittering across 0.63–0.67 on identical input -- a band
0.04 wide. The default threshold (0.70) clears that band by 0.03, less than
the band's own width, which is the failure mode. Every rung of the declared
ladder clears the band by at least its width, and the tests say so
numerically rather than by looking at the values.

Hermetic: no network, no model, no screen. The driver-level cases use the
same doubles the pipeline tests use.
"""
import unittest

from driver_core import policy, transport
from driver_core.actions import IRREVERSIBLE, MUTATING, READ_ONLY
from driver_core.audit import MemoryAuditLog
from driver_core.budget import Budget
from driver_core.config import (
    CONFIDENCE_JITTER_BAND, CONFIDENCE_LADDER, DEFAULT_CONFIDENCE_THRESHOLD,
    JITTER_BAND_WIDTH, ConfigError, ConfidenceRung, load_settings,
    validated_ladder,
)
from driver_core.driver import Driver
from driver_core.ev import (
    FakeJev, FakeTransport, action_answer, no_usage_response, ok_response,
)
from driver_core.executor import Consent
from driver_core.extractors import ExtractorPool, StructuredExtractor
from driver_core.guardrails import (
    BLOCK, HAZARD_KEYS, PASS, REVIEW, SEVERITY_LEVELS, GuardrailVerdict,
    guardrail_questions, verdict_from,
)
from driver_core.jev_client import JevClient
from driver_core.perception import StructuredSource
from driver_core.transport import Response

LADDER = CONFIDENCE_LADDER


def _clearance(value):
    """A threshold's distance from the observed jitter band (negative inside)."""
    low, high = CONFIDENCE_JITTER_BAND
    if value < low:
        return low - value
    if value > high:
        return value - high
    return -1.0


class LadderGateTests(unittest.TestCase):
    """The rung each action clears is chosen by its consequence."""

    def _gate(self, *, action="observe", confidence=0.72, action_class,
              threshold=0.70, ladder=LADDER, guards=None):
        decision = action_answer(action, confidence=confidence, guards=guards)
        return policy.check_decision(
            decision, threshold=threshold, require_stable=True,
            action_class=action_class, ladder=ladder)

    def test_one_confidence_passes_where_safe_and_stops_where_not(self):
        read_only = self._gate(action_class=READ_ONLY, confidence=0.72)
        mutating = self._gate(action_class=MUTATING, confidence=0.72)
        irreversible = self._gate(action_class=IRREVERSIBLE, confidence=0.72)

        self.assertTrue(read_only.passed)
        self.assertEqual(mutating.verdict, policy.REVIEW)
        self.assertFalse(mutating.passed)
        self.assertEqual(irreversible.verdict, policy.BLOCKED)
        self.assertFalse(irreversible.passed)
        # Both stop paths stay inside the 9-reason wire contract.
        self.assertEqual(mutating.reason, "confidence_below_threshold")
        self.assertEqual(irreversible.reason, "confidence_below_threshold")

    def test_an_irreversible_action_acts_at_the_documented_eighty_five(self):
        acts = self._gate(action_class=IRREVERSIBLE, confidence=0.85)
        self.assertTrue(acts.passed)

    def test_an_irreversible_action_below_the_act_rung_stops_for_confirmation(self):
        review = self._gate(action_class=IRREVERSIBLE, confidence=0.80)
        self.assertEqual(review.verdict, policy.REVIEW)
        self.assertIn("confirmation", review.detail)

    def test_confidence_exactly_at_the_confirm_rung_stops_for_confirmation(self):
        """The band is [confirm, act): the confirm rung itself is a review,
        and one notch below it is a block."""
        at = self._gate(action_class=MUTATING, confidence=0.55, threshold=0.5)
        below = self._gate(action_class=MUTATING, confidence=0.5499,
                           threshold=0.5)
        self.assertEqual(at.verdict, policy.REVIEW)
        self.assertEqual(below.verdict, policy.BLOCKED)

    def test_below_the_confirmation_floor_blocks_the_confirmation_route(self):
        review = self._gate(action_class=MUTATING, confidence=0.58,
                            threshold=0.5)
        blocked = self._gate(action_class=MUTATING, confidence=0.52,
                             threshold=0.5)
        self.assertEqual(review.verdict, policy.REVIEW)
        self.assertEqual(blocked.verdict, policy.BLOCKED)
        self.assertEqual(blocked.reason, "confidence_below_threshold")
        self.assertIn("confirmation rung", blocked.detail)

    def test_no_ladder_keeps_the_existing_binary_gate(self):
        """The ladder is an extension, not a rewrite: without one the gate
        passes exactly what it passed before."""
        gate = self._gate(action_class=MUTATING, confidence=0.72, ladder=None)
        self.assertTrue(gate.passed)

    def test_the_ladder_only_raises_the_bar_never_lowers_it(self):
        """A rung below the global floor is not a way past it: the floor
        still refuses first, so no rung can weaken an existing gate."""
        gate = self._gate(action_class=READ_ONLY, confidence=0.68,
                          threshold=0.70)
        self.assertEqual(gate.verdict, policy.BLOCKED)
        self.assertEqual(gate.reason, "confidence_below_threshold")

    def test_an_unstable_state_still_blocks_with_the_ladder_in_play(self):
        gate = self._gate(
            action_class=IRREVERSIBLE, confidence=0.95,
            guards={"state_is_stable": 0.2,
                    "a_blocking_choice_is_required": 0.1})
        self.assertEqual(gate.verdict, policy.BLOCKED)
        self.assertEqual(gate.reason, "state_not_stable")

    def test_a_class_with_no_rung_keeps_the_global_threshold_alone(self):
        decision = action_answer("click", confidence=0.72)
        gate = policy.check_decision(
            decision, threshold=0.70, require_stable=True,
            action_class=MUTATING,
            ladder={READ_ONLY: ConfidenceRung(act=0.55, confirm=0.55)})
        self.assertTrue(gate.passed)


class LadderDriverTests(unittest.TestCase):
    """The ladder through the driver, opt-in like the earlier landings."""

    def _run(self, *, confidence=0.72, ladder=False, action="click",
             guard_verdict=None, consent=None):
        jev = FakeJev(action_answer(action, confidence=confidence),
                      guard_verdict=guard_verdict)
        settings = load_settings(
            env={}, quorum=2, min_agreement=1.0, confidence_threshold=0.7,
            run_ceiling_usd=1.0, step_ceiling_usd=1.0, dry_run=False,
            jev_ladder=ladder)
        driver = Driver(settings=settings,
                        budget=Budget(1.0, step_ceiling_usd=1.0),
                        audit=MemoryAuditLog(), pool=_pool(), sources=[_source()],
                        jev=jev)
        performed = []
        driver.register_executor("click", lambda a, p: performed.append(p))
        result = driver.step("target", params={"target": "OK"},
                             consent=consent)
        return result, performed, jev

    def test_a_mutating_action_stops_at_its_rung_and_never_executes(self):
        """Without a consent to confirm it, a review stops as it always did."""
        result, performed, _ = self._run(ladder=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "confidence_below_threshold")
        self.assertEqual(result.stopped_at, "decision")
        self.assertIsNone(result.execution)
        self.assertEqual(performed, [])
        self.assertIn("confirmation", result.detail)

    def test_a_read_only_action_passes_at_the_same_confidence(self):
        """The driver-level form of the contrast: 0.72 observes and acts,
        while the same 0.72 stops a mutating action one rung up."""
        jev = FakeJev(action_answer("observe", confidence=0.72))
        settings = load_settings(
            env={}, quorum=2, min_agreement=1.0, confidence_threshold=0.7,
            run_ceiling_usd=1.0, step_ceiling_usd=1.0, dry_run=True,
            jev_ladder=True)
        driver = Driver(settings=settings,
                        budget=Budget(1.0, step_ceiling_usd=1.0),
                        audit=MemoryAuditLog(), pool=_pool(), sources=[_source()],
                        jev=jev)
        result = driver.step("target")
        self.assertTrue(result.ok)
        self.assertEqual(result.stopped_at, "executed")

    def test_without_the_flag_the_driver_keeps_the_single_global_threshold(self):
        result, performed, _ = self._run(
            ladder=False, consent=Consent(True, "click", {"target": "OK"}))
        self.assertTrue(result.ok)
        self.assertEqual(performed, [{"target": "OK"}])


class JitterBandTests(unittest.TestCase):
    """§5b's finding, enforced on every declared rung."""

    def test_every_declared_rung_clears_the_observed_jitter_band(self):
        for name, rung in LADDER.items():
            for label, value in (("act", rung.act), ("confirm", rung.confirm)):
                self.assertGreaterEqual(
                    _clearance(value), JITTER_BAND_WIDTH,
                    f"{name} {label} {value} clears the jitter band "
                    f"{CONFIDENCE_JITTER_BAND} by less than its width")

    def test_the_act_rungs_are_separated_by_more_than_the_band_width(self):
        acts = sorted(rung.act for rung in LADDER.values())
        for lower, upper in zip(acts, acts[1:], strict=False):
            self.assertGreater(
                upper - lower, JITTER_BAND_WIDTH,
                f"act rungs {lower} and {upper} would be separated on paper, "
                f"not by the model")

    def test_a_confirmation_band_is_absent_or_wider_than_the_jitter(self):
        for name, rung in LADDER.items():
            if rung.act == rung.confirm:
                continue
            self.assertGreater(
                rung.act - rung.confirm, JITTER_BAND_WIDTH,
                f"{name} has a confirmation band narrower than the jitter")

    def test_the_declared_defaults_pass_their_own_validator(self):
        self.assertIs(validated_ladder(LADDER), LADDER)

    def test_a_malformed_rung_value_is_refused_naming_the_field(self):
        """None, strings, bools and non-finite floats name no threshold:
        each refuses at construction with ConfigError naming the field, so a
        malformed declaration never exists to be compared later."""
        for field in ("act", "confirm"):
            for value in (None, "0.5", True, False, float("nan"),
                          float("inf"), 1.5, -0.1):
                rung = {"act": 0.55, "confirm": 0.55}
                rung[field] = value
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ConfigError) as ctx:
                        ConfidenceRung(**rung)
                    self.assertIn(field, str(ctx.exception))

    def test_a_threshold_inside_the_no_go_zone_is_refused(self):
        """The §5b failure, named: 0.70 clears the band by 0.03 -- less than
        the band's width -- so a rung there is refused at construction."""
        with self.assertRaises(ConfigError):
            validated_ladder({READ_ONLY: ConfidenceRung(act=0.70, confirm=0.55)})
        with self.assertRaises(ConfigError):
            validated_ladder({READ_ONLY: ConfidenceRung(act=0.65, confirm=0.55)})

    def test_a_rung_exactly_one_band_width_away_is_accepted(self):
        ladder = {
            READ_ONLY: ConfidenceRung(act=0.59, confirm=0.59),
            MUTATING: ConfidenceRung(act=0.71, confirm=0.59),
            IRREVERSIBLE: ConfidenceRung(act=0.85, confirm=0.75),
        }
        self.assertIs(validated_ladder(ladder), ladder)

    def test_two_act_rungs_closer_than_the_jitter_are_refused(self):
        with self.assertRaises(ConfigError):
            validated_ladder({
                READ_ONLY: ConfidenceRung(act=0.71, confirm=0.51),
                MUTATING: ConfidenceRung(act=0.74, confirm=0.51),
            })

    def test_a_thin_confirmation_band_is_refused(self):
        with self.assertRaises(ConfigError):
            validated_ladder({
                IRREVERSIBLE: ConfidenceRung(act=0.79, confirm=0.75),
            })

    def test_an_unknown_consequence_class_is_refused(self):
        with self.assertRaises(ConfigError):
            validated_ladder({"bogus": ConfidenceRung(act=0.55, confirm=0.55)})

    def test_the_global_floor_still_sits_inside_the_no_go_zone(self):
        """The documented tension the ladder exists because of: §5b measured
        the default threshold clearing the band by less than its width. The
        floor is an existing gate and stays untouched; the rungs are what
        must clear."""
        self.assertLess(
            _clearance(DEFAULT_CONFIDENCE_THRESHOLD), JITTER_BAND_WIDTH)


class ConfigFlagsTests(unittest.TestCase):
    """Presence-based opt-ins, consistent with the earlier landings."""

    def test_the_ladder_and_guardrails_are_off_unless_declared(self):
        settings = load_settings(env={})
        self.assertFalse(settings.jev_ladder)
        self.assertFalse(settings.jev_guardrails)

    def test_the_flags_are_presence_signals(self):
        settings = load_settings(env={"DRIVER_JEV_LADDER": "1",
                                      "DRIVER_JEV_GUARDRAILS": "true"})
        self.assertTrue(settings.jev_ladder)
        self.assertTrue(settings.jev_guardrails)

    def test_settings_carry_the_declared_ladder(self):
        self.assertIs(load_settings(env={}).confidence_ladder, LADDER)

    def test_settings_refuse_a_ladder_that_does_not_clear_the_band(self):
        with self.assertRaises(ConfigError):
            load_settings(env={}, confidence_ladder={
                READ_ONLY: ConfidenceRung(act=0.70, confirm=0.55)})

    def test_the_operator_surface_reports_configuration_never_a_crash(self):
        """A malformed rung through load_settings is a ConfigError, whether
        it arrives as a junk value or a junk type."""
        with self.assertRaises(ConfigError):
            load_settings(env={}, confidence_ladder={READ_ONLY: "a rung"})
        with self.assertRaises(ConfigError):
            ConfidenceRung(act="0.5", confirm="0.5")


def _noul(value):
    return {"type": "noul", "noul": value}


def _score(value):
    return {"type": "score", "score": value, "confidence": 0.9,
            "legend": {}, "probabilities": {}}


def _answers(**over):
    base = {"hazard_data_loss": _noul(0.05),
            "hazard_irreversible_change": _noul(0.1),
            "hazard_exfiltration": _noul(0.05),
            "severity": _score(0.4)}
    base.update(over)
    return base


class GuardrailPackTests(unittest.TestCase):
    """One request: hazard Nouls plus a severity Score, judged in code."""

    def test_the_pack_is_one_request_of_three_hazards_and_a_severity(self):
        questions = guardrail_questions("delete_file")
        transport.validate_questions(questions)
        self.assertEqual(sorted(questions), sorted((*HAZARD_KEYS, "severity")))
        for key in HAZARD_KEYS:
            self.assertEqual(questions[key]["type"], "noul")
        self.assertEqual(questions["severity"]["type"], "score")
        self.assertEqual(tuple(questions["severity"]["criteria"]),
                         SEVERITY_LEVELS)
        # The pack names the action it judges and the screen it judges against.
        self.assertIn("delete_file", questions["hazard_data_loss"]["instructions"])
        self.assertIn("screen", questions["hazard_data_loss"]["instructions"])

    def test_a_clear_pack_passes(self):
        verdict = verdict_from(_answers())
        self.assertEqual(verdict.verdict, PASS)
        self.assertTrue(verdict.passed)

    def test_a_hazard_routes_to_block(self):
        verdict = verdict_from(_answers(hazard_data_loss=_noul(0.6)))
        self.assertEqual(verdict.verdict, BLOCK)
        self.assertFalse(verdict.passed)
        self.assertIn("hazard_data_loss", verdict.detail)
        self.assertEqual(verdict.hazard, 0.6)

    def test_a_suspected_hazard_routes_to_review(self):
        verdict = verdict_from(_answers(hazard_exfiltration=_noul(0.3)))
        self.assertEqual(verdict.verdict, REVIEW)

    def test_serious_severity_routes_to_review(self):
        verdict = verdict_from(_answers(severity=_score(2.2)))
        self.assertEqual(verdict.verdict, REVIEW)
        self.assertEqual(verdict.severity, 2.2)

    def test_irreversible_severity_blocks_even_without_a_loud_hazard(self):
        verdict = verdict_from(_answers(severity=_score(2.6)))
        self.assertEqual(verdict.verdict, BLOCK)

    def test_the_documented_floors_are_inclusive(self):
        """Every floor is ``>=``: exactly on it fires the verdict. A
        one-character change to ``>`` would silently invert the documented
        rule, so the boundaries are pinned rather than assumed."""
        self.assertEqual(verdict_from(
            _answers(hazard_data_loss=_noul(0.5))).verdict, BLOCK)
        self.assertEqual(verdict_from(
            _answers(hazard_data_loss=_noul(0.2))).verdict, REVIEW)
        self.assertEqual(verdict_from(
            _answers(severity=_score(2.0))).verdict, REVIEW)
        self.assertEqual(verdict_from(
            _answers(severity=_score(2.5))).verdict, BLOCK)

    def test_just_under_the_floors_does_not_cross_them(self):
        self.assertEqual(verdict_from(
            _answers(hazard_data_loss=_noul(0.1999))).verdict, PASS)
        self.assertEqual(verdict_from(
            _answers(severity=_score(1.999))).verdict, PASS)
        self.assertEqual(verdict_from(
            _answers(hazard_data_loss=_noul(0.4999))).verdict, REVIEW)
        self.assertEqual(verdict_from(
            _answers(severity=_score(2.4999))).verdict, REVIEW)

    def test_a_missing_answer_is_a_block_not_a_pass(self):
        answers = _answers()
        del answers["hazard_data_loss"]
        self.assertEqual(verdict_from(answers).verdict, BLOCK)

    def test_a_wrong_typed_answer_is_a_block(self):
        verdict = verdict_from(_answers(severity=_noul(0.1)))
        self.assertEqual(verdict.verdict, BLOCK)

    def test_a_non_numeric_answer_is_a_block(self):
        verdict = verdict_from(_answers(hazard_data_loss=_noul("high")))
        self.assertEqual(verdict.verdict, BLOCK)

    def test_an_out_of_range_answer_is_a_block(self):
        self.assertEqual(verdict_from(_answers(severity=_score(3.5))).verdict,
                         BLOCK)
        self.assertEqual(verdict_from(_answers(hazard_data_loss=_noul(1.5))).verdict,
                         BLOCK)

    def test_an_undeclared_answer_key_is_a_block(self):
        self.assertEqual(verdict_from(_answers(surprise=_noul(0.0))).verdict,
                         BLOCK)

    def test_answers_that_are_not_an_object_are_a_block(self):
        self.assertEqual(verdict_from(None).verdict, BLOCK)
        self.assertEqual(verdict_from([1, 2]).verdict, BLOCK)


class GuardrailGateTests(unittest.TestCase):
    """The pack at the execution gate: acting is gated, observing is not."""

    def _run(self, *, action="click", confidence=0.95, guardrails=True,
             guard_verdict=None, consent=None):
        jev = FakeJev(action_answer(action, confidence=confidence),
                      guard_verdict=guard_verdict)
        settings = load_settings(
            env={}, quorum=2, min_agreement=1.0, confidence_threshold=0.7,
            run_ceiling_usd=1.0, step_ceiling_usd=1.0, dry_run=False,
            jev_guardrails=guardrails)
        driver = Driver(settings=settings,
                        budget=Budget(1.0, step_ceiling_usd=1.0),
                        audit=MemoryAuditLog(), pool=_pool(), sources=[_source()],
                        jev=jev)
        performed = []
        driver.register_executor("click", lambda a, p: performed.append(p))
        # An observation takes no parameters and needs no consent; the
        # decision tier's own parameter provenance rule refuses a step that
        # hands it any.
        if action == "click":
            result = driver.step(
                "target", params={"target": "OK"}, consent=consent)
        else:
            result = driver.step("target")
        return result, performed, jev, driver

    def test_a_blocked_guard_never_reaches_the_executor(self):
        """A matching consent changes nothing here: a block is never
        confirmable -- the remedy for a hazard is a different action."""
        verdict = GuardrailVerdict(BLOCK, "a hazard was found", hazard=0.9,
                                   severity=3.0, cost=0.03)
        result, performed, jev, driver = self._run(
            guard_verdict=verdict,
            consent=Consent(True, "click", {"target": "OK"}))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "execution_refused")
        self.assertIn("guardrail block", result.detail)
        self.assertIsNone(result.execution)
        self.assertEqual(performed, [])
        # The stop's cost is honest about the judgment that stopped it.
        self.assertAlmostEqual(result.cost, result.decision.cost + 0.03)
        self.assertTrue(any(r.get("kind") == "refusal"
                            and r.get("reason") == "execution_refused"
                            for r in driver.audit.read_all()))

    def test_a_review_guard_is_a_refusal_with_the_verdict_named(self):
        """Without a consent to confirm it, a review stops as it always did."""
        verdict = GuardrailVerdict(REVIEW, "a hazard was suspected",
                                   hazard=0.3, severity=1.0)
        result, performed, _, _ = self._run(guard_verdict=verdict)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "execution_refused")
        self.assertIn("guardrail review", result.detail)
        self.assertEqual(performed, [])

    def test_a_passing_guard_lets_the_action_through(self):
        verdict = GuardrailVerdict(PASS, "clear", hazard=0.05, severity=0.4)
        result, performed, jev, _ = self._run(
            guard_verdict=verdict,
            consent=Consent(True, "click", {"target": "OK"}))
        self.assertTrue(result.ok)
        self.assertEqual(result.stopped_at, "executed")
        self.assertEqual(performed, [{"target": "OK"}])
        self.assertEqual(len(jev.guard_calls), 1)

    def test_the_pack_judges_the_proposed_action_against_the_observed_state(self):
        verdict = GuardrailVerdict(PASS, "clear", hazard=0.05, severity=0.4)
        result, _, jev, _ = self._run(
            guard_verdict=verdict,
            consent=Consent(True, "click", {"target": "OK"}))
        self.assertEqual(len(jev.guard_calls), 1)
        self.assertEqual(jev.guard_calls[0]["action"], "click")
        self.assertEqual(jev.guard_calls[0]["state"], result.agreement.state)

    def test_the_pack_is_never_asked_about_an_observation(self):
        result, _, jev, _ = self._run(action="observe")
        self.assertTrue(result.ok)
        self.assertEqual(jev.guard_calls, [])

    def test_without_the_flag_the_pack_is_never_asked_or_billed(self):
        verdict = GuardrailVerdict(BLOCK, "a hazard was found", hazard=0.9,
                                   severity=3.0)
        result, performed, jev, _ = self._run(
            guardrails=False, guard_verdict=verdict,
            consent=Consent(True, "click", {"target": "OK"}))
        self.assertTrue(result.ok)
        self.assertEqual(performed, [{"target": "OK"}])
        self.assertEqual(jev.guard_calls, [])


class ConfirmationTests(unittest.TestCase):
    """The ladder's "or a confirmation step", in the repo's own Consent.

    The composition rule is asymmetric on purpose and tested as such: a
    confirmation substitutes for the confidence rung -- the review band
    below the act rung, and the guardrail review -- and for nothing else. A
    block, an unstable state, and below-floor confidence are not waiting for
    a braver human.
    """

    def _run(self, *, confidence=0.72, action="click", params=None,
             consent=None, guard_verdict=None, guards=None):
        jev = FakeJev(action_answer(action, confidence=confidence,
                                    guards=guards),
                      guard_verdict=guard_verdict)
        settings = load_settings(
            env={}, quorum=2, min_agreement=1.0, confidence_threshold=0.7,
            run_ceiling_usd=1.0, step_ceiling_usd=1.0, dry_run=False,
            jev_ladder=True,
            # The pack is asked only when a verdict is staged for it, so the
            # ladder's confirmation is exercised alone everywhere else.
            jev_guardrails=guard_verdict is not None)
        driver = Driver(settings=settings,
                        budget=Budget(1.0, step_ceiling_usd=1.0),
                        audit=MemoryAuditLog(), pool=_pool(), sources=[_source()],
                        jev=jev)
        performed = []
        driver.register_executor("click", lambda a, p: performed.append(p))
        if params is None:
            params = {"target": "OK"} if action == "click" else None
        result = driver.step("target", params=params, consent=consent)
        return result, performed, jev, driver

    @staticmethod
    def _consent(action="click", params=None):
        return Consent(True, action,
                       params if params is not None else {"target": "OK"})

    def test_a_bound_consent_satisfies_the_rung_and_the_action_runs_once(self):
        result, performed, _, driver = self._run(consent=self._consent())
        self.assertTrue(result.ok)
        self.assertEqual(result.stopped_at, "executed")
        self.assertEqual(performed, [{"target": "OK"}])
        self.assertTrue(any(
            r.get("kind") == "escalation" and r.get("reason") == "review_confirmed"
            and r.get("action") == "click"
            for r in driver.audit.read_all()),
            "a below-rung action that ran on a confirmation must say in the "
            "log that a human was asked")

    def test_a_consent_for_another_action_does_not_confirm(self):
        result, performed, _, _ = self._run(
            consent=self._consent("type_text", {"text": "x"}))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "confidence_below_threshold")
        self.assertIsNone(result.execution)
        self.assertEqual(performed, [])

    def test_a_consent_for_other_params_does_not_confirm(self):
        result, performed, _, _ = self._run(
            consent=self._consent("click", {"target": "OTHER"}))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "confidence_below_threshold")
        self.assertEqual(performed, [])

    def test_an_unresolvable_proposal_is_not_confirmed_and_never_raises(self):
        """A proposal whose params cannot even be resolved is unconfirmed,
        not an exception: the review stop stands, and ``step`` keeps its
        never-raises-for-a-blocked-step contract."""
        result, performed, _, _ = self._run(
            action="write_file", params={"path": "", "content": "x"},
            consent=Consent(True, "write_file", {"path": "", "content": "x"}))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "confidence_below_threshold")
        self.assertEqual(performed, [])

    def test_review_without_consent_stops_with_the_confirmation_named(self):
        result, _, _, _ = self._run()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "confidence_below_threshold")
        self.assertIn("confirmation", result.detail)

    def test_a_ladder_block_refuses_confirmation(self):
        """Below the confirm rung there is nothing to confirm: a perfect
        consent for an ``irreversible`` action at 0.72 changes the stop by
        exactly nothing."""
        result, performed, _, _ = self._run(
            action="delete_file", params={"path": "x"}, confidence=0.72,
            consent=self._consent("delete_file", {"path": "x"}))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "confidence_below_threshold")
        self.assertIn("confirmation rung", result.detail)
        self.assertIsNone(result.execution)
        self.assertEqual(performed, [])

    def test_an_unstable_state_refuses_confirmation(self):
        """A confirmation substitutes for the confidence rung and never for
        an existing gate: an in-flight screen is not a confidence question."""
        result, performed, _, _ = self._run(
            confidence=0.95, consent=self._consent(),
            guards={"state_is_stable": 0.2,
                    "a_blocking_choice_is_required": 0.1})
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "state_not_stable")
        self.assertEqual(performed, [])

    def test_below_the_floor_still_blocks_regardless_of_consent(self):
        result, performed, _, _ = self._run(confidence=0.68,
                                            consent=self._consent())
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "confidence_below_threshold")
        self.assertEqual(performed, [])

    def test_a_guardrail_review_is_confirmable_by_the_same_consent(self):
        verdict = GuardrailVerdict(REVIEW, "a hazard was suspected",
                                   hazard=0.3, severity=1.0)
        result, performed, _, driver = self._run(
            confidence=0.95, guard_verdict=verdict, consent=self._consent())
        self.assertTrue(result.ok)
        self.assertEqual(performed, [{"target": "OK"}])
        self.assertTrue(any(
            r.get("kind") == "escalation" and r.get("reason") == "review_confirmed"
            for r in driver.audit.read_all()))

    def test_a_guardrail_review_without_consent_stops_as_before(self):
        verdict = GuardrailVerdict(REVIEW, "a hazard was suspected",
                                   hazard=0.3, severity=1.0)
        result, performed, _, _ = self._run(
            confidence=0.95, guard_verdict=verdict)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "execution_refused")
        self.assertIn("guardrail review", result.detail)
        self.assertEqual(performed, [])


class GuardClientTests(unittest.TestCase):
    """The guard call itself: one billed judgment, one metadata record."""

    def _client(self, *responses, keyed=True):
        settings = load_settings(
            env={"DRIVER_JEV_API_KEY": "k" * 20} if keyed else {})
        budget = Budget(1.0, step_ceiling_usd=1.0)
        audit = MemoryAuditLog()
        wire = FakeTransport(*responses)
        client = JevClient(settings, budget=budget, audit=audit,
                           transport_module=wire)
        return client, budget, audit, wire

    def test_one_billed_judgment_writes_one_metadata_only_record(self):
        client, _, audit, wire = self._client(
            ok_response(_answers(), input_tokens=100, output_tokens=10))
        verdict = client.guard("delete_file", {"screen": "ref"}, step_id="s1")
        self.assertEqual(verdict.verdict, PASS)
        self.assertGreater(verdict.cost, 0.0)
        self.assertEqual(len(wire.requests), 1)
        self.assertEqual(wire.requests[0]["body"]["state"], {"screen": "ref"})
        records = audit.read_all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["mode"], "guardrail")
        self.assertEqual(records[0]["action"], "delete_file")
        self.assertEqual(records[0]["verdict"], "pass")
        self.assertNotIn("state", records[0])

    def test_malformed_answers_block_and_settle_honestly(self):
        """The fail-closed path is still a billed call: it settles and
        records rather than quietly costing nothing."""
        client, _, audit, _ = self._client(
            ok_response({"junk": {}}, input_tokens=100, output_tokens=5))
        verdict = client.guard("click", {"screen": "ref"})
        self.assertEqual(verdict.verdict, BLOCK)
        self.assertGreater(verdict.cost, 0.0)
        self.assertEqual(len(audit.read_all()), 1)

    def test_no_usage_settles_the_full_reservation_rather_than_zero(self):
        client, _, _, _ = self._client(no_usage_response(_answers()))
        verdict = client.guard("click", {"screen": "ref"})
        self.assertEqual(verdict.verdict, PASS)
        self.assertEqual(verdict.usage_source, "unavailable")
        self.assertGreater(verdict.cost, 0.0)

    def test_an_unkeyed_client_blocks_without_dispatching(self):
        client, _, _, wire = self._client(keyed=False)
        verdict = client.guard("click", {"screen": "ref"})
        self.assertEqual(verdict.verdict, BLOCK)
        self.assertEqual(wire.requests, [])
        self.assertEqual(verdict.cost, 0.0)

    def test_a_failed_call_blocks_and_settles_honestly(self):
        client, _, audit, _ = self._client(
            Response(500, "http_error", detail="boom"))
        verdict = client.guard("click", {"screen": "ref"})
        self.assertEqual(verdict.verdict, BLOCK)
        self.assertIn("boom", verdict.detail)
        self.assertGreater(verdict.cost, 0.0)
        self.assertEqual(len(audit.read_all()), 1)


def _state(**over):
    base = {"window_title": "Report - Editor",
            "foreground_app": "editor",
            "error_dialog_present": False}
    base.update(over)
    return base


def _pool():
    return ExtractorPool([
        StructuredExtractor(f"structured-{i}",
                            lambda capture, schema: dict(_state()))
        for i in range(2)
    ])


def _source():
    return StructuredSource("cli", lambda target: dict(_state()))


if __name__ == "__main__":
    unittest.main()
