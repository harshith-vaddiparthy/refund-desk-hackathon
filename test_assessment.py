"""Offline guards against the retained actual local-model answers; no inference."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from assessment import REQUIRED_FACTS, assess


class AssessmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.benchmark = json.loads((Path(__file__).parent / "benchmarks" / "first-results.json").read_text())

    def setUp(self):
        self.policy = deepcopy(self.benchmark["policy"])
        self.version = self.policy["version"]
        self.case = deepcopy(self.benchmark["cases"][0]["input"])
        self.answer = deepcopy(self.benchmark["cases"][0]["answer"])

    def evaluate(self):
        return assess(self.case, self.policy, self.answer, policy_version=self.version)

    def assert_incomplete(self, result):
        self.assertEqual(result["status"], "REVIEW_INCOMPLETE")
        self.assertIsNone(result["accepted_recommendation"])
        self.assertFalse(result["payment_authorized"])
        self.assertTrue(result["errors"])

    def test_retained_model_failure_stays_visible_and_other_cases_pass_guards(self):
        for case in self.benchmark["cases"]:
            with self.subTest(case=case["id"]):
                original = deepcopy(case["answer"])
                result = assess(case["input"], self.policy, case["answer"], policy_version=self.version)
                self.assertEqual(case["answer"], original)
                self.assertEqual(result["original_model_response"], original)
                if case["id"] == "missing_facts":
                    self.assert_incomplete(result)
                    self.assertEqual(result["original_model_response"]["recommendation"], "decline")
                    self.assertEqual(result["missing_verified_facts"], ["purchase_date", "item_used"])
                    self.assertIn("contradictory_recommendation", {error["code"] for error in result["errors"]})
                else:
                    self.assertEqual(result["status"], "REVIEW_READY", result)
                    self.assertEqual(result["accepted_recommendation"], original["recommendation"])

    def test_false_is_a_present_fact_and_ready_advice_does_not_authorize_payment(self):
        result = self.evaluate()
        self.assertEqual(result["status"], "REVIEW_READY", result)
        self.assertNotIn("item_used", result["missing_verified_facts"])
        self.assertEqual(result["accepted_recommendation"], "refund")
        self.assertFalse(result["payment_authorized"])
        self.assertTrue(result["human_approval_required_for_payment"])

    def test_missing_required_facts_block_refund_and_decline(self):
        for field in REQUIRED_FACTS:
            for recommendation in ("refund", "decline"):
                with self.subTest(field=field, recommendation=recommendation):
                    case = deepcopy(self.case)
                    del case["verified_transaction"][field]
                    answer = dict(self.answer, recommendation=recommendation)
                    result = assess(case, self.policy, answer, policy_version=self.version)
                    self.assert_incomplete(result)
                    self.assertIn(field, result["missing_verified_facts"])

    def test_money_rejects_booleans_floats_strings_and_invalid_balances(self):
        for field, values in {
            "captured_amount_minor": (True, False, 39.0, "3900", -1, 0),
            "completed_refunds_minor": (True, False, 0.0, "0", -1, 3901, 3900),
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    case = deepcopy(self.case)
                    case["verified_transaction"][field] = value
                    self.assert_incomplete(assess(case, self.policy, self.answer, policy_version=self.version))

    def test_invalid_dates_currency_and_item_status_do_not_become_usable_facts(self):
        for field, values in {
            "purchase_date": ("20260912", "2026-02-30", "2026-10-03", 20260912),
            "request_date": (False, "bad-date", "2026-09-01"),
            "item_used": (0, 1, "false", []),
            "currency": (False, "usd", "EUR", 840),
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    case = deepcopy(self.case)
                    case["verified_transaction"][field] = value
                    self.assert_incomplete(assess(case, self.policy, self.answer, policy_version=self.version))

    def test_declared_missing_information_and_conflicting_recommendations_are_incomplete(self):
        for recommendation, missing in (("refund", ["purchase_date"]),
                                        ("decline", ["additional_evidence"]),
                                        ("request_information", []),
                                        ("request_information", ["additional_evidence"])):
            with self.subTest(recommendation=recommendation, missing=missing):
                answer = dict(self.answer, recommendation=recommendation, missing_information=missing)
                self.assert_incomplete(assess(self.case, self.policy, answer, policy_version=self.version))

    def test_invented_malformed_and_wrong_version_citations_are_rejected(self):
        variants = [[], "P1", ["P1"], [{"id": "P99", "quote": "Invented exception"}],
                    [{"id": "P1", "quote": self.policy["clauses"]["P1"] + " "}],
                    [{"id": "P2", "quote": self.policy["clauses"]["P1"]}],
                    [{"id": "P1"}], [{"id": [], "quote": "bad"}]]
        for citations in variants:
            with self.subTest(citations=citations):
                answer = dict(self.answer, citations=citations)
                self.assert_incomplete(assess(self.case, self.policy, answer, policy_version=self.version))
        self.policy["version"] = "newer-policy-same-clause-text"
        result = self.evaluate()
        self.assert_incomplete(result)
        self.assertIn("policy_version_mismatch", {error["code"] for error in result["errors"]})

    def test_malformed_policy_or_model_response_is_not_accepted(self):
        for answer in (None, [], {}, dict(self.answer, recommendation="approve"),
                       dict(self.answer, recommendation=True), dict(self.answer, rationale=""),
                       dict(self.answer, payment_authorized=True), dict(self.answer, missing_information=None)):
            with self.subTest(answer=answer):
                self.assert_incomplete(assess(self.case, self.policy, answer, policy_version=self.version))
        for policy in (None, {}, {"version": self.version, "clauses": {}},
                       {"version": self.version, "clauses": {"P1": None}}):
            with self.subTest(policy=policy):
                self.assert_incomplete(assess(self.case, policy, self.answer, policy_version=self.version))

    def test_preserved_response_is_a_copy_and_no_input_is_rewritten(self):
        before = deepcopy((self.case, self.policy, self.answer))
        result = self.evaluate()
        result["original_model_response"]["citations"][0]["quote"] = "changed result copy"
        self.assertEqual((self.case, self.policy, self.answer), before)


if __name__ == "__main__":
    unittest.main()
