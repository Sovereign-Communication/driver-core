"""The audit's own contract, hermetically.

The audit is the one place in this package that asks a model hundreds of
questions, so the properties worth pinning are the ones that keep those
hundreds of answers *readable*: the questions are declared and closed, every
symbol is judged against a mechanically computed dossier rather than against
the model's word, a bucket nothing can supply comes back ``ungrounded``
instead of being invented, and the run stops on budget and *says* it stopped.

No key, no network, no spend. The one transport here answers the declared pack
exactly, which is what a real endpoint does.
"""
import json
import os
import pathlib
import shutil
import tempfile
import unittest

from driver_core import jev_audit
from driver_core import transport
from driver_core.config import JEV_INPUT_PRICE_PER_MILLION, load_settings
from driver_core.jev_context import CONTEXT_BUCKETS

PACKAGE = pathlib.Path(jev_audit.__file__).parent
ROOT = str(PACKAGE.parent)


def _keyed():
    return load_settings(env={"DRIVER_JEV_API_KEY": "sk-test"})


#: Parsing this tree is cheap but not free, and a dozen tests here each want
#: the same list. Cached per root so the *audit's* behaviour is what the tests
#: spend their time on, not the fixture's.
_SYMBOL_CACHE = {}


def _symbols(root=ROOT, tests_root=None):
    if (root, tests_root) not in _SYMBOL_CACHE:
        _SYMBOL_CACHE[(root, tests_root)] = list(
            jev_audit.symbols(root, tests_root=tests_root))
    return _SYMBOL_CACHE[(root, tests_root)]


def _distribution(options, chosen, confidence):
    others = [name for name in options if name != chosen]
    share = (1.0 - confidence) / len(others) if others else 0.0
    return {name: (confidence if name == chosen else share) for name in options}


class AuditingTransport:
    """A transport answering the declared pack, with the levers named."""

    def __init__(self, *, need=0, bucket="none", driver="contract_fidelity",
                 dimension="contract_fidelity", verdict="true",
                 confidence=0.99):
        self.need = need
        self.bucket = bucket
        self.driver = driver
        self.dimension = dimension
        self.verdict = verdict
        self.confidence = confidence
        self.requests = []

    def call_service(self, url, questions, *, headers=None, timeout=60,
                     body_extra=None):
        self.requests.append({"url": url, "questions": questions,
                              "body": body_extra, "headers": headers})
        answers = {}
        for name, question in questions.items():
            kind = question["type"]
            if kind == "noul":
                noul = (self.confidence if self.verdict == "true"
                        else 1.0 - self.confidence)
                answers[name] = {"type": "noul", "noul": noul,
                                 "confidence": self.confidence}
            elif kind == "choice":
                answers[name] = self._choice(name, question)
            else:
                answers[name] = {
                    "type": "score", "score": self.need,
                    "confidence": self.confidence,
                    "legend": list(question["criteria"]),
                    "probabilities": {str(level): 0.0
                                      for level in question["criteria"]},
                }
        return transport.Response(
            200, "ok",
            payload={"model": "audit-fake", "answers": answers,
                     "usage": {"input_tokens": 1200, "output_tokens": 90}})

    def _choice(self, name, question):
        options = list(question["criteria"])
        chosen = options[0]
        if name == "context_bucket":
            chosen = self.bucket
        elif name == "driving_bucket":
            chosen = self.driver
        elif name == jev_audit.DIMENSION_NEEDING_CONTEXT:
            chosen = self.dimension
        return {"type": "choice", "choice": chosen,
                "confidence": self.confidence,
                "probabilities": _distribution(options, chosen,
                                               self.confidence)}


class SymbolCollectionTests(unittest.TestCase):
    """Enumeration is deterministic, and the dossier is computed, not asked."""

    def test_every_symbol_is_collected_exactly_once(self):
        found = _symbols(tests_root=os.path.join(ROOT, "tests"))
        keys = [symbol.key for symbol in found]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertGreater(len(found), 100)

    def test_the_order_is_stable_across_two_reads(self):
        first = [s.key for s in _symbols()]
        second = [s.key for s in _symbols()]
        self.assertEqual(first, second)
        self.assertEqual(len(first), len(set(first)))

    def test_the_walk_never_descends_into_a_copy_of_the_tree(self):
        """An audit that walks into build/ and dist/ reports every symbol two
        or three times, doubling every count in the report without failing."""
        for symbol in _symbols(tests_root=os.path.join(ROOT, "tests")):
            parts = pathlib.Path(symbol.path).parts
            self.assertFalse(jev_audit._pruned(parts[:-1]), symbol.path)

    def test_a_nested_function_is_found_and_named_by_its_parent(self):
        found = {symbol.key: symbol for symbol in _symbols()}
        self.assertIn("driver_core.driver:_params_for", found)
        self.assertEqual(
            found["driver_core.driver:_params_for"].kind, "function")

    def test_two_files_of_the_same_stem_get_two_different_keys(self):
        """A package module and the runner beside it are both named
        ``jev_audit``; one key for two symbols would silently mis-count."""
        found = {symbol.key for symbol in _symbols()}
        self.assertTrue(any(key.startswith("driver_core.jev_audit:")
                            for key in found))
        self.assertTrue(any(key.startswith("tools.jev_audit:")
                            for key in found))

    def test_the_dossier_counts_tests_that_really_name_the_symbol(self):
        found = {symbol.key: symbol for symbol in
                 _symbols(tests_root=os.path.join(ROOT, "tests"))}
        guarded = found["driver_core.jev_client:offered_actions"]
        self.assertGreater(guarded.dossier["test_files_mentioning"], 0)
        for path in guarded.dossier["test_files"]:
            self.assertIn("test", pathlib.Path(path).name)
        # The count discriminates: some symbol in this tree is named by no
        # test at all, and the dossier says so without asking a model.
        unnamed = [s for s in found.values()
                   if s.dossier["test_files_mentioning"] == 0]
        self.assertTrue(unnamed, "the count never reaches zero")

    def test_a_copy_of_the_package_is_not_counted_twice(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="driver-audit-copy-") as tmp:
            real = pathlib.Path(tmp, "pkg")
            real.mkdir()
            real.joinpath("mod.py").write_text(
                'def only_once():\n    """One symbol."""\n    return 1\n',
                encoding="utf-8")
            copy = pathlib.Path(tmp, "pkg", "build")
            copy.mkdir()
            copy.joinpath("mod.py").write_text(
                'def only_once():\n    """A stale copy."""\n    return 1\n',
                encoding="utf-8")
            found = list(jev_audit.symbols(tmp))
            self.assertEqual([s.key for s in found], ["pkg.mod:only_once"])

    def test_the_dossier_never_reads_the_source_from_a_model(self):
        symbol = next(jev_audit.symbols(str(PACKAGE / "osal.py")))
        self.assertIn("lines", symbol.dossier)
        self.assertIsInstance(symbol.dossier["branches"], int)
        self.assertIsInstance(symbol.dossier["raises"], list)


class DeclaredPackTests(unittest.TestCase):
    """The questions are declared, closed, and shaped like real ones."""

    def test_the_pack_is_a_valid_question_set(self):
        pack = jev_audit.pack_for()
        transport.validate_questions(pack)
        self.assertEqual(len(pack), len(jev_audit.DIMENSION_NAMES) + 1)
        self.assertIn(jev_audit.DIMENSION_NEEDING_CONTEXT, pack)

    def test_every_dimension_names_the_buckets_that_would_ground_it(self):
        declared = {bucket for bucket, _text in CONTEXT_BUCKETS}
        for dimension in jev_audit.DIMENSIONS:
            self.assertTrue(dimension.context, dimension.name)
            self.assertLessEqual(set(dimension.context), declared)

    def test_the_bucket_pairing_question_offers_every_dimension(self):
        pack = jev_audit.pack_for()
        question = pack[jev_audit.DIMENSION_NEEDING_CONTEXT]
        self.assertEqual(set(question["criteria"]),
                         set(jev_audit.DIMENSION_NAMES))


class SupplierTests(unittest.TestCase):
    """Every supplied bucket answers with real material; the rest refuse."""

    def setUp(self):
        self.symbols = _symbols(tests_root=os.path.join(ROOT, "tests"))
        self.symbol = next(s for s in self.symbols if s.key ==
                           "driver_core.jev_client:offered_actions")
        self.supplier = jev_audit.make_supplier(
            self.symbol, jev_audit.package_text(ROOT),
            peers=jev_audit.siblings(self.symbol, self.symbols))

    def test_every_supplied_bucket_answers_with_text(self):
        for bucket in jev_audit.SUPPLIED_BUCKETS:
            text = self.supplier(bucket, 1)
            self.assertTrue(text, bucket)
            self.assertIsInstance(text, str)

    def test_the_code_bucket_carries_the_symbol_source(self):
        self.assertIn("def offered_actions", self.supplier("code", 1))

    def test_the_contract_bucket_carries_the_docstring(self):
        self.assertIn("offered", self.supplier("contract", 1))

    def test_the_tests_bucket_carries_a_real_test_not_a_promise(self):
        text = self.supplier("tests", 1)
        self.assertIn("def test_", text)

    def test_the_constraints_bucket_is_derived_from_the_tree(self):
        text = self.supplier("constraints", 1)
        self.assertIn("DRIVER_", text)

    def test_the_counterexample_bucket_is_refused_by_design(self):
        """No supplier, because a counterexample needs a *run*."""
        self.assertIn("counterexample", jev_audit.UNSUPPLIED_BUCKETS)
        self.assertIsNone(self.supplier("counterexample", 1))

    def test_an_unknown_bucket_is_refused_rather_than_improvised(self):
        self.assertIsNone(self.supplier("vibes", 1))


class AuditSymbolTests(unittest.TestCase):
    """One symbol, every dimension, with the context protocol attached."""

    def _audit(self, fake, **kwargs):
        symbols = _symbols()
        symbol = next(s for s in symbols if s.key ==
                      "driver_core.jev_client:offered_actions")
        return jev_audit.audit_symbol(
            symbol, symbols, jev_audit.package_text(ROOT), settings=_keyed(),
            transport_module=fake, **kwargs)

    def test_every_dimension_gets_a_reading(self):
        record = self._audit(AuditingTransport())
        for name in jev_audit.DIMENSION_NAMES:
            self.assertIn(name, record["readings"], name)
            self.assertIsNotNone(record["readings"][name]["answer"], name)

    def test_every_call_carries_the_context_protocol(self):
        fake = AuditingTransport()
        self._audit(fake)
        sent = fake.requests[0]["questions"]
        for name in ("context_needed", "context_bucket", "driving_bucket"):
            self.assertIn(name, sent)

    def test_a_grounded_answer_settles_in_one_round(self):
        fake = AuditingTransport(need=0, bucket="none")
        record = self._audit(fake)
        self.assertEqual(record["rounds"], 1)
        self.assertFalse(record["ungrounded"])
        self.assertEqual(len(fake.requests), 1)

    def test_an_outstanding_need_supplies_the_named_bucket_and_asks_again(self):
        fake = AuditingTransport(need=6, bucket="tests")
        record = self._audit(fake)
        self.assertGreater(record["rounds"], 1)
        self.assertIn("tests", record["grounding"]["supplied"][0]["bucket"])
        # The supplied material really is in the next request's state.
        second = fake.requests[1]["body"]["state"]
        self.assertIn("tests", second["supplied_context"])

    def test_a_bucket_nothing_can_supply_comes_back_ungrounded(self):
        """The honest failure: not "low confidence", but "no evidence"."""
        fake = AuditingTransport(need=9, bucket="counterexample")
        record = self._audit(fake)
        self.assertTrue(record["ungrounded"])
        self.assertEqual(record["reason_kind"], "unanswerable")
        self.assertIn("counterexample", record["ungrounded_reason"])
        self.assertEqual(record["rounds"], 1)

    def test_a_question_with_no_confident_answer_is_reported_apart(self):
        """"Nothing could answer" and "nothing settled it" are different.

        Both leave the loop ungrounded, and only one of them is a missing
        input: here every bucket the model wanted was supplied, it then said
        it needed nothing further, and its declared answers are simply not at
        the gate. Reporting that as an absent supplier would send a reader
        looking for material that exists and was already provided.
        """
        fake = AuditingTransport(need=9, bucket="none", confidence=0.5)
        record = self._audit(fake)
        self.assertTrue(record["ungrounded"])
        self.assertEqual(record["reason_kind"], "still_short")
        self.assertEqual(record["context_bucket"], "none")
        self.assertEqual(record["grounding"]["supplied"], [])

    def test_no_credential_crosses_to_the_model(self):
        fake = AuditingTransport()
        self._audit(fake)
        rendered = json.dumps([request["body"] for request in fake.requests],
                              default=str)
        self.assertNotIn("sk-test", rendered)
        self.assertNotIn("authorization", rendered.lower())

    def test_the_record_names_the_dimension_and_the_kind_of_context_it_needs(self):
        fake = AuditingTransport(need=4, bucket="tests",
                                 dimension="regression_guarded")
        record = self._audit(fake)
        self.assertEqual(record["dimension_needing_most_context"],
                         "regression_guarded")
        self.assertEqual(record["context_bucket"], "tests")

    def test_the_cost_matches_the_one_declared_price(self):
        fake = AuditingTransport()
        record = self._audit(fake)
        self.assertAlmostEqual(
            record["cost_usd"],
            record["input_tokens"] * JEV_INPUT_PRICE_PER_MILLION / 1e6,
            places=9)


class AggregateAndReportTests(unittest.TestCase):
    """Counts, not prose; and the caveat travels with the count."""

    def setUp(self):
        symbols = _symbols()
        texts = jev_audit.package_text(ROOT)
        wanted = {"driver_core.jev_client:offered_actions",
                  "driver_core.driver:_params_for"}
        chosen = [s for s in symbols if s.key in wanted]
        self.records = [
            jev_audit.audit_symbol(symbol, symbols, texts, settings=_keyed(),
                                   transport_module=AuditingTransport(
                                       need=9, bucket="counterexample"))
            for symbol in chosen]

    def test_the_summary_counts_every_dimension(self):
        summary = jev_audit.aggregate(self.records)
        self.assertEqual(summary["symbols_audited"], 2)
        for name in jev_audit.DIMENSION_NAMES:
            self.assertIn(name, summary["by_dimension"])
            self.assertIn("answers", summary["by_dimension"][name])

    def test_ungrounded_symbols_are_counted_and_attributed(self):
        summary = jev_audit.aggregate(self.records)
        self.assertEqual(summary["ungrounded"], 2)
        self.assertEqual(summary["unanswered_buckets"]["counterexample"], 2)
        self.assertEqual(summary["unsettled_by_kind"]["unanswerable"], 2)

    def test_the_report_prints_the_caveat_not_only_the_verdict(self):
        report = jev_audit.render_report(jev_audit.aggregate(self.records),
                                         root=ROOT, model="audit-fake", gate=0.9)
        self.assertIn("Where nothing could answer", report)
        self.assertIn("Where the loop ended unsettled, and why", report)
        self.assertIn("counterexample", report)
        for name in jev_audit.DIMENSION_NAMES:
            self.assertIn(f"`{name}`", report)

    def test_the_report_renders_with_nothing_audited(self):
        report = jev_audit.render_report(jev_audit.aggregate([]))
        self.assertIn("0 symbols", report)


class RunTests(unittest.TestCase):
    """A run checkpoints, resumes, and stops on budget out loud."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="driver-audit-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        package = pathlib.Path(self.directory, "target")
        package.mkdir()
        package.joinpath("sample.py").write_text(
            '"""A tiny package to audit."""\n'
            "\n"
            "\n"
            "def first(value):\n"
            '    """Double a value."""\n'
            "    return value * 2\n"
            "\n"
            "\n"
            "class Second:\n"
            '    """A class."""\n'
            "\n"
            "    def method(self):\n"
            '        """Do nothing."""\n'
            "        return None\n",
            encoding="utf-8")
        self.package = package
        self.checkpoint = os.path.join(self.directory, "symbols.jsonl")

    def test_a_run_audits_every_symbol_then_says_it_completed(self):
        written = []
        records = list(jev_audit.audit(
            str(self.package), settings=_keyed(),
            transport_module=AuditingTransport(),
            checkpoint=written.append))
        self.assertEqual(len(written), 3)
        self.assertTrue(records[-1]["stopped"] is False)
        self.assertEqual(records[-1]["audit"], "complete")
        self.assertEqual(records[-1]["audited"], 3)

    def test_a_budget_of_zero_stops_before_spending_anything(self):
        records = list(jev_audit.audit(
            str(self.package), settings=_keyed(), budget_usd=0.0,
            transport_module=AuditingTransport()))
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["stopped"])
        self.assertEqual(records[0]["audit"], "budget exhausted")
        self.assertEqual(records[0]["audited"], 0)

    def test_a_symbol_already_done_is_not_paid_for_twice(self):
        first = list(jev_audit.audit(str(self.package), settings=_keyed(),
                                    transport_module=AuditingTransport()))
        done = {record["key"] for record in first if "key" in record}
        fake = AuditingTransport()
        again = list(jev_audit.audit(str(self.package), settings=_keyed(),
                                    transport_module=fake, done=done))
        self.assertEqual(fake.requests, [])
        self.assertEqual(again[-1]["audited"], 0)

    def test_the_checkpoint_is_one_json_object_per_line(self):
        path = pathlib.Path(self.directory, "cp.jsonl")
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            list(jev_audit.audit(
                str(self.package), settings=_keyed(),
                transport_module=AuditingTransport(),
                checkpoint=lambda record: handle.write(json.dumps(record) + "\n")))
        read = jev_audit.load_checkpoint(path)
        self.assertEqual(len(read), 3)

    def test_a_truncated_final_line_does_not_lose_the_rest(self):
        path = pathlib.Path(self.directory, "cp2.jsonl")
        path.write_text('{"key": "a"}\n{"key": "b"}\n{"key": "unfinis',
                        encoding="utf-8", newline="\n")
        read = jev_audit.load_checkpoint(path)
        self.assertEqual([record["key"] for record in read], ["a", "b"])


class DownTransport:
    """A provider that is down: the loop must say *transport*, not guess."""

    def __init__(self):
        self.calls = 0

    def call_service(self, url, questions, *, headers=None, timeout=60,
                     body_extra=None):
        self.calls += 1
        return transport.Response(503, transport.HTTP_ERROR,
                                  detail="upstream down")


class CheckpointReasonKindTests(unittest.TestCase):
    """The three ways a loop can end ungrounded survive a real checkpoint.

    ``Grounding.reason_kind`` is only worth having if it survives the trip the
    runner actually takes: record -> one JSON object per line -> read back ->
    aggregated. A kind that lived only in-process would leave the runner's
    report showing ``unknown`` for every unsettled symbol, which is exactly
    the conflation the split exists to remove -- and the three call for three
    different responses, so a reader cannot tell them apart from a count.
    """

    def _record(self, transport_module):
        symbols = _symbols()
        symbol = next(s for s in symbols
                      if s.key == "driver_core.jev_client:offered_actions")
        return jev_audit.audit_symbol(
            symbol, symbols, jev_audit.package_text(ROOT), settings=_keyed(),
            transport_module=transport_module)

    def _summary(self):
        """One record per kind, round-tripped through a checkpoint file.

        The lines are written the way ``tools/jev_audit.py`` writes them
        (``sort_keys``, ``default=str``), so what is asserted here is the
        runner's own path and not a friendlier one.
        """
        directory = tempfile.mkdtemp(prefix="driver-audit-kinds-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = pathlib.Path(directory, "kinds.jsonl")
        cases = [AuditingTransport(need=9, bucket="counterexample"),
                 AuditingTransport(need=9, bucket="code"),
                 AuditingTransport(need=9, bucket="none", confidence=0.5),
                 AlwaysAskingTransport(), DownTransport()]
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for case in cases:
                handle.write(json.dumps(self._record(case), sort_keys=True,
                                        default=str) + "\n")
        return jev_audit.aggregate(jev_audit.load_checkpoint(path))

    def test_each_kind_survives_a_checkpoint_and_aggregates_apart(self):
        summary = self._summary()
        self.assertEqual(summary["unsettled_by_kind"], {
            "unanswerable": 1, "no_new_evidence": 1, "still_short": 1,
            "rounds_exhausted": 1, "transport": 1})
        # Only the missing *input* counts as an unanswered bucket. A bucket
        # that was supplied and then asked for again is not an absence, and
        # reporting it as material to fetch would send a reader after
        # something the run already had.
        self.assertEqual(summary["unanswered_buckets"], {"counterexample": 1})
        self.assertEqual(summary["context_bucket_asked_for"]["code"], 1)

    def test_the_report_separates_missing_material_from_a_hard_question(self):
        report = jev_audit.render_report(self._summary(), root=ROOT,
                                         model="audit-fake", gate=0.9)
        self.assertIn("Where nothing could answer", report)
        self.assertIn("| `counterexample` | 1 |", report)
        self.assertIn("Where the loop ended unsettled, and why", report)
        self.assertIn("| `no_new_evidence` | 1 |", report)
        self.assertIn("| `still_short` | 1 |", report)
        self.assertIn("| `rounds_exhausted` | 1 |", report)
        self.assertIn("| `transport` | 1 |", report)
        self.assertNotIn("| `unknown` |", report)


class RepeatedRequestTests(unittest.TestCase):
    """A bucket supplied twice is not a bucket nobody has.

    Both stop the loop ungrounded, and one `unanswerable` count merged them:
    253 of 305 in the superseded checkpoint were the *same* bucket arriving
    twice, which reads in a report as missing material and sends a reader
    looking for something the run already had. The two want opposite
    responses -- go and obtain it, versus there is nothing to obtain.
    """

    def _record(self, **kwargs):
        symbols = _symbols()
        symbol = next(s for s in symbols
                      if s.key == "driver_core.jev_client:offered_actions")
        return jev_audit.audit_symbol(
            symbol, symbols, jev_audit.package_text(ROOT), settings=_keyed(),
            transport_module=AuditingTransport(**kwargs))

    def test_asking_twice_for_the_same_bucket_is_not_unanswerable(self):
        record = self._record(need=9, bucket="contract")
        self.assertTrue(record["ungrounded"])
        self.assertEqual(record["reason_kind"], "no_new_evidence")
        self.assertIn("asked again", record["ungrounded_reason"])
        self.assertEqual([entry["bucket"]
                          for entry in record["grounding"]["supplied"]],
                         ["contract"])
        summary = jev_audit.aggregate([record])
        self.assertNotIn("contract", summary["unanswered_buckets"])
        self.assertEqual(summary["unsettled_by_kind"], {"no_new_evidence": 1})

    def test_a_bucket_no_supplier_has_is_still_unanswerable(self):
        record = self._record(need=9, bucket="counterexample")
        self.assertEqual(record["reason_kind"], "unanswerable")
        summary = jev_audit.aggregate([record])
        self.assertEqual(summary["unanswered_buckets"], {"counterexample": 1})


class AlwaysAskingTransport:
    """Asks for a *different* bucket every round, and never stops.

    A supplier that always has new material and a model that always asks for
    more is the exact shape of a loop that runs out of rounds.
    """

    def __init__(self, buckets=("code", "tests", "facts", "contract")):
        self.buckets = list(buckets)
        self.round = 0
        self.requests = []

    def call_service(self, url, questions, *, headers=None, timeout=60,
                     body_extra=None):
        self.requests.append({"questions": questions, "body": body_extra})
        chosen = self.buckets[self.round % len(self.buckets)]
        self.round += 1
        answers = {}
        for name, question in questions.items():
            kind = question["type"]
            if kind == "noul":
                answers[name] = {"type": "noul", "noul": 0.97,
                                 "confidence": 0.97}
            elif kind == "choice":
                options = list(question["criteria"])
                pick = options[0]
                if name == "context_bucket":
                    pick = chosen
                elif name == "driving_bucket":
                    pick = "fidelity"
                elif name == jev_audit.DIMENSION_NEEDING_CONTEXT:
                    pick = "contract_fidelity"
                answers[name] = {"type": "choice", "choice": pick,
                                 "confidence": 0.97,
                                 "probabilities": _distribution(options, pick,
                                                                0.97)}
            else:
                answers[name] = {
                    "type": "score", "score": 9, "confidence": 0.97,
                    "legend": list(question["criteria"]),
                    "probabilities": {str(level): 0.0
                                      for level in question["criteria"]},
                }
        return transport.Response(
            200, "ok",
            payload={"model": "audit-fake", "answers": answers,
                     "usage": {"input_tokens": 1000, "output_tokens": 50}})


class SettlingTransport(AuditingTransport):
    """Asks for one bucket, then reports itself satisfied.

    Built to settle on the *last* permitted round -- the case a naive
    "``rounds == max_rounds``, therefore exhausted" rule would get wrong.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._round = 0

    def call_service(self, url, questions, *, headers=None, timeout=60,
                     body_extra=None):
        self._round += 1
        if self._round == 1:
            self.need, self.bucket = 9, "code"
        else:
            self.need, self.bucket = 0, "none"
        return super().call_service(url, questions, headers=headers,
                                    timeout=timeout, body_extra=body_extra)


class RoundBudgetTests(unittest.TestCase):
    """Exhausting the round budget is not the same as a settled answer.

    A loop still asking for new context when its round budget ran out fell
    through the bottom of the ``for`` and was returned with
    ``ungrounded=False`` -- a settled answer -- for a question that never
    settled. It was then counted in the report as *grounded*, so a false
    success read exactly like a true one, which is the one failure mode this
    module exists to prevent. ``rounds_exhausted`` names it, and the
    derivation is exact rather than heuristic: the loop supplies in the round
    it breaks, so a supply recorded in the final round is a loop that had no
    round left to ask in.
    """

    def _record(self, transport_module, **kwargs):
        symbols = _symbols()
        symbol = next(s for s in symbols
                      if s.key == "driver_core.jev_client:offered_actions")
        return jev_audit.audit_symbol(
            symbol, symbols, jev_audit.package_text(ROOT), settings=_keyed(),
            transport_module=transport_module, **kwargs)

    def test_exhausting_the_round_budget_is_reported_ungrounded(self):
        record = self._record(AlwaysAskingTransport(), max_rounds=3)
        self.assertEqual(record["rounds"], 3)
        self.assertTrue(record["ungrounded"])
        self.assertEqual(record["reason_kind"], "rounds_exhausted")
        supplied = record["grounding"]["supplied"]
        self.assertEqual([entry["bucket"] for entry in supplied],
                         ["code", "tests", "facts"])
        self.assertEqual(supplied[-1]["round"], record["rounds"])

    def test_settling_on_the_last_permitted_round_is_not_exhaustion(self):
        record = self._record(SettlingTransport(), max_rounds=2)
        self.assertEqual(record["rounds"], 2)
        self.assertFalse(record["ungrounded"])
        self.assertEqual(record["reason_kind"], "")
        self.assertEqual([entry["round"]
                          for entry in record["grounding"]["supplied"]], [1])

    def test_a_zero_round_budget_is_not_a_settled_answer(self):
        """``max_rounds=0`` never enters the loop, so the net must catch it."""
        record = self._record(SettlingTransport(), max_rounds=0)
        self.assertEqual(record["rounds"], 0)
        self.assertTrue(record["ungrounded"])
        self.assertEqual(record["reason_kind"], "rounds_exhausted")

    def test_the_new_kind_aggregates_apart_from_the_others(self):
        record = self._record(AlwaysAskingTransport(), max_rounds=3)
        summary = jev_audit.aggregate([record])
        self.assertEqual(summary["unsettled_by_kind"], {"rounds_exhausted": 1})
        # Nothing was missing, so it must not appear as material to fetch.
        self.assertEqual(summary["unanswered_buckets"], {})


class NeedBandTests(unittest.TestCase):
    """A continuous score is banded, because the declared levels are anchors."""

    def test_a_fractional_score_lands_in_a_band(self):
        self.assertEqual(jev_audit.need_band(0), "0 nothing further")
        self.assertEqual(jev_audit.need_band(1.26), "1–2")
        self.assertEqual(jev_audit.need_band(2.0), "1–2")
        self.assertEqual(jev_audit.need_band(4.71), "above 4")

    def test_an_unanswered_score_is_named_rather_than_dropped(self):
        self.assertEqual(jev_audit.need_band(None), "unscored")
        self.assertEqual(jev_audit.need_band("nonsense"), "unscored")


class EstimateTests(unittest.TestCase):
    """The estimate is a real count of what a pass would cover."""

    def test_the_estimate_covers_the_package(self):
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "jev_audit.py"),
             "--estimate"],
            capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("symbols would be audited", result.stdout)
        self.assertIn("questions per symbol", result.stdout)

    def test_the_runner_refuses_without_a_key_rather_than_reporting_clean(self):
        import subprocess
        import sys
        env = {key: value for key, value in os.environ.items()
               if key != "DRIVER_JEV_API_KEY"}
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "jev_audit.py"),
             "--limit", "1"],
            capture_output=True, text=True, cwd=ROOT, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("refusing to run", result.stderr)


if __name__ == "__main__":
    unittest.main()
