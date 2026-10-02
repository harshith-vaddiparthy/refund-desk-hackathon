"""Synthetic validator fixtures only; no model, payment or network calls."""
from copy import deepcopy
import unittest

from assessment import assess, preflight
from benchmarks.local_ai import CASES, POLICY
from evidence import compose_preapproval_reply, make_sources, schema_for, validate_evidence


def full_answer(case=None, policy=None):
    case, policy = case or CASES[0]["input"], policy or POLICY
    source = make_sources(case["customer_message"])[0]
    return {"recommendation": "refund", "rationale": "The supplied confirmations satisfy the policy.",
            "citations": [{"id": "P1", "quote": policy["clauses"]["P1"]}], "missing_information": [],
            "summary": "The customer requests a refund.",
            "claims": [{"kind": "intent", "source_id": source["id"], "quote": source["text"]}],
            "issues": []}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.case = deepcopy(CASES[0]["input"])
        self.case["sources"] = make_sources(self.case["customer_message"])
        self.answer = full_answer(self.case)

    def assess(self):
        return assess(self.case, POLICY, self.answer, policy_version=POLICY["version"])

    def test_source_references_and_presentations_do_not_claim_semantic_truth(self):
        value = validate_evidence(self.case, self.answer)
        self.assertEqual(value["validation_status"], "valid")
        self.assertEqual(value["claims"][0]["id"], "C1")
        self.assertTrue(value["source_references_validated"])
        self.assertFalse(value["semantic_truth_verified"])
        self.assertEqual(value['reply_draft_source'], 'application_template')
        self.assertEqual(value['reply_draft'], 'Your request is ready for a merchant decision. No refund has been approved or sent.')

    def test_preapproval_copy_contains_only_customer_questions_and_no_model_status_prose(self):
        issues = [{'owner':'customer','question':'Could you share the original receipt?'},
                  {'owner':'merchant','question':'Is the replacement in stock?'}]
        draft = compose_preapproval_reply('request_information', issues)
        self.assertIn(issues[0]['question'], draft)
        self.assertNotIn(issues[1]['question'], draft)
        self.assertEqual(compose_preapproval_reply('request_information', issues[1:]), 'Your request awaits merchant review.')
        self.assertNotIn('reply_draft', schema_for(self.case, POLICY)['properties'])

    def test_wrong_quote_id_or_source_snapshot_fails_without_exposing_accepted_claims(self):
        for field, value in (("source_id", "M99"), ("quote", "A fabricated source quote"),
                             ("quote", self.case["customer_message"][:10]), ("source_id", [])):
            with self.subTest(field=field, value=value):
                answer = deepcopy(self.answer); answer["claims"][0][field] = value
                result = validate_evidence(self.case, answer)
                self.assertEqual(result["validation_status"], "invalid")
                self.assertEqual(result["claims"], [])
        self.case["sources"][0]["text"] = "a replaced source"
        self.assertFalse(preflight(self.case, POLICY, policy_version=POLICY["version"])["valid"])

    def test_unknown_merchant_fact_is_analyzable_and_useful_information_request_is_completed(self):
        self.case["verified_transaction"]["item_used"] = None
        self.assertTrue(preflight(self.case, POLICY, policy_version=POLICY["version"])["valid"])
        self.answer.update(recommendation="request_information", missing_information=["item_used"],
            issues=[{"kind": "missing", "field": "item_used", "source_ids": [], "owner": "merchant",
                     "detail": "Merchant confirmation is absent.", "question": "Has the merchant confirmed item-use status?"}])
        result = self.assess()
        self.assertEqual(result["status"], "REVIEW_NEEDS_INFORMATION", result)
        self.assertEqual(result["accepted_recommendation"], "request_information")
        self.assertFalse(result["payment_authorized"])
        self.assertEqual(result["evidence"]["issues"][0]["id"], "I1")

    def test_missing_payment_facts_still_block_before_inference(self):
        self.case["verified_transaction"]["captured_amount_minor"] = None
        self.assertFalse(preflight(self.case, POLICY, policy_version=POLICY["version"])["valid"])

    def test_known_facts_cannot_be_declared_missing_as_in_actual_failed_pilot(self):
        self.answer.update(recommendation="request_information", missing_information=["request_date"],
            issues=[{"kind": "missing", "field": "request_date", "source_ids": [], "owner": "merchant",
                     "detail": "The date is missing.", "question": "When was the request received?"}])
        result = self.assess()
        self.assertEqual(result["status"], "REVIEW_INCOMPLETE")
        self.assertIn("known_fact_reported_missing", {e["code"] for e in result["errors"]})

    def test_refund_with_unresolved_conflict_cannot_become_accepted_advice(self):
        self.answer["issues"] = [{"kind": "conflict", "field": "amount", "source_ids": ["M1"], "owner": "customer",
                                 "detail": "The amount needs reconciliation.", "question": "Is another transaction involved?"}]
        result = self.assess()
        self.assertEqual(result["status"], "REVIEW_INCOMPLETE")
        self.assertIsNone(result["accepted_recommendation"])
        self.assertFalse(result["payment_authorized"])

    def test_known_outside_window_decline_can_explain_conflict_without_authorizing_refund(self):
        self.case['verified_transaction']['purchase_date'] = '2026-08-20'
        self.answer.update(recommendation='decline', citations=[{'id':'P2','quote':POLICY['clauses']['P2']}],
            issues=[{'kind':'conflict','field':'policy','source_ids':['M1'],'owner':'merchant',
                     'detail':'The customer asks for different terms.','question':'Is there supporting context?'}])
        result = self.assess()
        self.assertEqual(result['status'], 'REVIEW_READY', result)
        self.assertTrue(result['deterministic_decline_confirmed'])
        self.assertFalse(result['payment_authorized'])
        self.case['verified_transaction']['item_used'] = None
        self.assertEqual(self.assess()['status'], 'REVIEW_INCOMPLETE')
        self.case['verified_transaction'].update(item_used=False, purchase_date='2026-09-20')
        self.assertEqual(self.assess()['status'], 'REVIEW_INCOMPLETE')

    def test_conflict_needs_source_and_unknown_field_needs_a_concrete_question(self):
        self.case["verified_transaction"]["item_used"] = None
        self.answer.update(recommendation="request_information", missing_information=["item_used"],
            issues=[{"kind": "conflict", "field": "item_used", "source_ids": [], "owner": "customer",
                     "detail": "Alleged conflict.", "question": "Can this be resolved?"}])
        self.assertEqual(self.assess()["status"], "REVIEW_INCOMPLETE")
        self.answer["issues"] = []
        self.assertEqual(self.assess()["status"], "REVIEW_INCOMPLETE")

    def test_dynamic_schema_uses_input_unknowns_not_known_fields_or_expected_labels(self):
        allowed = schema_for(self.case, POLICY)["properties"]["missing_information"]["items"]["enum"]
        self.assertNotIn("request_date", allowed)
        self.assertNotIn("item_used", allowed)
        self.case["verified_transaction"]["item_used"] = None
        self.assertIn("item_used", schema_for(self.case, POLICY)["properties"]["missing_information"]["items"]["enum"])

    def test_policy_citation_schema_binds_each_id_to_its_exact_policy_text(self):
        properties = schema_for(self.case, POLICY)["properties"]["citations"]["items"]["properties"]
        self.assertEqual(set(properties["id"]["enum"]), set(POLICY["clauses"]))
        self.assertEqual(set(properties["quote"]["enum"]), set(POLICY["clauses"].values()))
        self.answer["citations"] = [{"id": "P2", "quote": POLICY["clauses"]["P1"]}]
        self.assertEqual(self.assess()["status"], "REVIEW_INCOMPLETE")

    def test_source_limits_are_explicit_and_no_payment_authority_field_is_accepted(self):
        with self.assertRaises(ValueError): make_sources("x" * 4001)
        with self.assertRaises(ValueError): make_sources("\n\n".join(["message"] * 13))
        self.answer["payment_authorized"] = True
        self.assertEqual(self.assess()["status"], "REVIEW_INCOMPLETE")

    def test_richer_review_text_and_count_bounds_remain_bounded(self):
        self.case['customer_message'] = '\n\n'.join(f'Customer statement {i}.' for i in range(12))
        self.case['sources'] = make_sources(self.case['customer_message'])
        self.answer = full_answer(self.case)
        self.answer.update(summary='s' * 512, recommendation='request_information', missing_information=['other'],
            claims=[{'kind':'other','source_id':s['id'],'quote':s['text']} for s in self.case['sources']],
            issues=[{'kind':'missing','field':'other','owner':'merchant','source_ids':[],
                     'detail':'d' * 512,'question':'q' * 512} for _ in range(6)])
        self.assertEqual(self.assess()['status'], 'REVIEW_NEEDS_INFORMATION')
        properties = schema_for(self.case, POLICY)['properties']
        self.assertEqual(properties['summary']['maxLength'], 512)
        self.assertEqual(properties['claims']['maxItems'], 12)
        self.assertEqual(properties['issues']['maxItems'], 6)
        for field in ('detail', 'question'):
            self.assertEqual(properties['issues']['items']['properties'][field]['maxLength'], 512)
        for field in ('summary', 'detail', 'question', 'claims', 'issues'):
            answer = deepcopy(self.answer)
            if field == 'summary': answer[field] += 's'
            elif field in ('detail', 'question'): answer['issues'][0][field] += 'x'
            else: answer[field].append(deepcopy(answer[field][0]))
            self.assertEqual(assess(self.case, POLICY, answer, policy_version=POLICY['version'])['status'], 'REVIEW_INCOMPLETE')


if __name__ == "__main__":
    unittest.main()
