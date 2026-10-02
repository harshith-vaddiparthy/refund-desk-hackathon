"""Focused coordinator integration tests with explicit test clients; no network."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from local_model import DIGEST, MODEL
from model_config import select_runtime
from paypal import PayPalError
from service import RefundService, ServiceError


MERCHANT = "MERCHANT123"
EMAIL = "merchant@example.test"
CAPTURE = "CAPTURE123"


def completion_fixture(provider):
    """Constructed receipt metadata for guard tests; no provider was called."""
    model = {"model_request_attempted": True, "completed_model_response": True,
             "elapsed_seconds": 1.0, "runtime": {}}
    if provider == "groq":
        provenance = "hosted_groq"
        model.update(provider="groq", model="openai/gpt-oss-120b", digest=None,
                     endpoint="https://api.groq.com/openai/v1/chat/completions", http_status=200,
                     finish_reason="stop", response_id="chatcmpl-offline-fixture")
    else:
        provenance = "local_ollama"
        model.update(model=MODEL, digest=DIGEST, endpoint="http://127.0.0.1:11434")
    return {"error": None, "generator_invocations": 1, "runtime_provenance": provenance,
            "model_calls": 1, "model_request_attempts": 1, "completed_model_responses": 1, "model": model}


def response(method, path, data, *, status=200, request_id=None):
    return {"environment": "sandbox", "provenance": "injected_test_transport",
            "method": method, "path": path, "status": status, "request_id": request_id,
            "debug_id": "TESTDEBUG", "data": deepcopy(data)}


class TestClient:
    is_live = False

    def __init__(self):
        purchased = datetime.now(timezone.utc) - timedelta(days=5)
        self.capture = {"id": CAPTURE, "status": "COMPLETED", "create_time": purchased.isoformat(),
                        "amount": {"currency_code": "USD", "value": "39.00"},
                        "payee": {"merchant_id": MERCHANT, "email_address": EMAIL}}
        self.get_capture_calls = 0
        self.refund_calls = []
        self.readback_calls = []
        self.post_error = None
        self.get_error = None
        self.on_post = None
        self.refund_status = "COMPLETED"

    def get_capture(self, capture_id):
        self.get_capture_calls += 1
        return response("GET", f"/v2/payments/captures/{capture_id}", self.capture)

    def refund_capture(self, capture_id, amount_minor, request_id):
        self.refund_calls.append((capture_id, amount_minor, request_id))
        if self.on_post:
            self.on_post()
        if self.post_error:
            raise self.post_error
        self.capture["status"] = "REFUNDED"
        return response("POST", f"/v2/payments/captures/{capture_id}/refund", self._refund(),
                        status=201, request_id=request_id)

    def _refund(self):
        return {"id": "REFUND123", "status": self.refund_status,
                "amount": {"value": "39.00", "currency_code": "USD"},
                "links": [{"rel": "up", "href": f"https://api-m.sandbox.paypal.com/v2/payments/captures/{CAPTURE}"}]}

    def get_refund(self, refund_id):
        self.readback_calls.append(refund_id)
        if self.get_error:
            raise self.get_error
        return response("GET", f"/v2/payments/refunds/{refund_id}", self._refund())


class TestGenerator:
    def __init__(self):
        self.calls = 0
        self.recommendation = "refund"
        self.completed = True
        self.invalid_json = False
        self.bad_quote = False
        self.issues = []
        self.report_missing = True
        self.corrupted_fields = {}
        self.inputs = []

    def __call__(self, case, policy):
        self.calls += 1
        self.inputs.append(deepcopy(case))
        missing = [field for field in ("item_used", "request_date")
                   if case["verified_transaction"][field] is None] if self.report_missing else []
        clause = "P3" if missing else "P1"
        answer = {"recommendation": "request_information" if missing else self.recommendation,
                  "rationale": "Confirm missing facts before deciding." if missing else "Test response citing the fixed policy.",
                  "citations": [{"id": clause, "quote": policy["clauses"][clause]}], "missing_information": missing,
                  "summary": "The customer requests a refund.",
                  "claims": [{"kind": "intent", "source_id": "M1",
                              "quote": "Fabricated customer quote." if self.bad_quote else case["sources"][0]["text"]}],
                  "issues": deepcopy(self.issues) + [{"kind": "missing", "field": field, "source_ids": [], "owner": "merchant",
                              "detail": "The merchant has not confirmed this fact.", "question": f"Please confirm {field}."} for field in missing]}
        answer.update(self.corrupted_fields)
        return {"model": MODEL, "digest": DIGEST, "response_text": "not-json" if self.invalid_json else json.dumps(answer),
                "model_request_attempted": True, "completed_model_response": self.completed,
                "runtime": {"prompt_eval_count": 100, "eval_count": 60}, "error": None}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="refund-desk-service-test-")
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name) / "data"
        self.client = TestClient()
        self.generator = TestGenerator()
        self.service = self.make_service()

    def make_service(self, **options):
        return RefundService(self.data, self.client, MERCHANT, EMAIL, generator=self.generator,
                             approval_kind="test_operator", approved_by="automated-test", **options)

    def create(self, **options):
        return self.service.create_case(CAPTURE, options.get("customer_message", "My item is unused. Please refund it."),
                                        options.get("item_used", False), options.get("request_date", datetime.now(timezone.utc).date().isoformat()))

    def reviewed(self, **options):
        case = self.create(**options)
        return self.service.review_case(case["id"])

    def groq_runtime(self):
        path = Path(self.temporary.name) / "test-only-groq.json"
        path.write_text(json.dumps({"provider": "groq", "model": "openai/gpt-oss-120b",
                                    "api_key": "test-only-secret-not-real", "verified_plan": "Free",
                                    "key_name": "fixture", "expires_date": "2026-12-16"}))
        path.chmod(0o600)
        return select_runtime("groq", path)

    def test_complete_case_review_approval_refund_and_independent_readback(self):
        case = self.reviewed()
        self.assertTrue(case["can_approve"], case)
        self.assertEqual(case["amount_minor"], 3900)
        self.assertEqual(case["transaction_provenance"], "injected_test_transport")
        self.assertFalse(case["transaction_verified_by_service"])
        self.assertEqual(case["physical_facts_provenance"], "merchant_supplied")
        self.assertEqual(case["latest_review"]["model_calls"], 0)
        report = json.loads((self.data / "reviews" / case["latest_review"]["id"] / "report.json").read_text())
        self.assertNotEqual(case["latest_review"]["review_hash"], report["inputs_sha256"])

        def before_http_result():
            current = self.service.get_case(case["id"])
            self.assertIsNotNone(current["operation"])
            self.assertEqual(current["operation"]["state"], "submitting")

        self.client.on_post = before_http_result
        completed = self.service.approve(case["id"], case["latest_review"]["review_hash"])
        self.assertEqual(completed["state"], "verified")
        self.assertEqual(self.client.get_capture_calls, 2)
        self.assertEqual(len(self.client.refund_calls), 1)
        self.assertEqual(self.client.refund_calls[0][:2], (CAPTURE, 3900))
        self.assertEqual(self.client.readback_calls, ["REFUND123"])
        self.assertEqual(completed["operation"]["approval"]["approval_kind"], "test_operator")
        self.assertEqual(completed["operation"]["verification_provenance"], "injected_test_transport")
        self.assertFalse(completed["can_approve"])
        self.assertFalse(completed["can_review"])
        self.assertEqual(len(self.service.list_cases()), 1)
        self.assertEqual(self.data.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.data / "desk.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_capture_merchant_amount_and_partial_state_cannot_be_supplied_or_bypassed(self):
        for field in ("merchant_id", "email_address", "missing_payee", "currency", "partial"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                client = TestClient()
                if field in ("merchant_id", "email_address"):
                    client.capture["payee"][field] = "wrong"
                elif field == "missing_payee":
                    del client.capture["payee"]
                elif field == "currency":
                    client.capture["amount"]["currency_code"] = "EUR"
                else:
                    client.capture["status"] = "PARTIALLY_REFUNDED"
                service = RefundService(temporary, client, MERCHANT, EMAIL, generator=self.generator)
                with self.assertRaises(ServiceError):
                    service.create_case(CAPTURE, "Refund request", False)
                self.assertEqual(service.list_cases(), [])
                self.assertEqual(client.refund_calls, [])
        for invalid in (0, "unused", []):
            with self.assertRaises(ServiceError):
                self.service.create_case(CAPTURE, "Refund request", invalid)

    def test_no_model_decline_used_and_outside_window_never_reach_payment(self):
        case = self.create()
        with self.assertRaises(ServiceError):
            self.service.approve(case["id"], "a" * 64)
        for problem in ("decline", "used", "outside", "no_completion"):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as temporary:
                client, generator = TestClient(), TestGenerator()
                if problem == "outside":
                    client.capture["create_time"] = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
                if problem == "decline":
                    generator.recommendation = "decline"
                if problem == "no_completion":
                    generator.completed = False
                service = RefundService(temporary, client, MERCHANT, EMAIL, generator=generator)
                row = service.create_case(CAPTURE, "Please refund", problem == "used", datetime.now(timezone.utc).date().isoformat())
                row = service.review_case(row["id"])
                self.assertFalse(row["can_approve"], row)
                with self.assertRaises(ServiceError):
                    service.approve(row["id"], row["latest_review"]["review_hash"])
                self.assertEqual(client.refund_calls, [])

    def test_stale_review_and_changed_capture_cannot_be_approved(self):
        case = self.reviewed()
        with self.assertRaises(ServiceError) as caught:
            self.service.approve(case["id"], "b" * 64)
        self.assertEqual(caught.exception.code, "stale_review")
        self.client.capture["amount"]["value"] = "40.00"
        with self.assertRaises(ServiceError) as caught:
            self.service.approve(case["id"], case["latest_review"]["review_hash"])
        self.assertEqual(caught.exception.code, "capture_changed")
        self.assertEqual(self.client.refund_calls, [])
        self.assertFalse(self.service.get_case(case["id"])["can_approve"])

    def test_mutated_report_is_not_a_new_approval_and_failed_review_retries_are_immutable(self):
        self.generator.invalid_json = True
        first = self.reviewed()
        self.assertEqual(first["state"], "review_incomplete")
        self.assertTrue(first["can_review"])
        first_path = self.data / "reviews" / first["latest_review"]["id"] / "report.json"
        first_bytes = first_path.read_bytes()
        self.generator.invalid_json = False
        second = self.service.review_case(first["id"])
        self.assertNotEqual(second["latest_review"]["id"], first["latest_review"]["id"])
        self.assertEqual(first_path.read_bytes(), first_bytes)
        self.assertEqual(len(second["reviews"]), 2)
        latest_path = self.data / "reviews" / second["latest_review"]["id"] / "report.json"
        report = json.loads(latest_path.read_text())
        report["accepted_recommendation"] = "decline"
        latest_path.write_text(json.dumps(report))
        with self.assertRaises(ServiceError) as caught:
            self.service.approve(second["id"], second["latest_review"]["review_hash"])
        self.assertEqual(caught.exception.code, "review_changed")
        self.assertFalse(self.service.get_case(second["id"])["can_approve"])
        self.assertEqual(self.client.refund_calls, [])

    def test_duplicate_creation_review_approval_and_restart_do_not_repeat_actions(self):
        first = self.create()
        again = self.create()
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(self.client.get_capture_calls, 1)
        with self.assertRaises(ServiceError):
            self.service.create_case(CAPTURE, "Changed case", False)
        reviewed = self.service.review_case(first["id"])
        self.service.review_case(first["id"])
        self.assertEqual(self.generator.calls, 1)
        completed = self.service.approve(first["id"], reviewed["latest_review"]["review_hash"])
        self.service = self.make_service()
        duplicate = self.service.approve(first["id"], reviewed["latest_review"]["review_hash"])
        self.assertEqual(completed["operation"]["request_id"], duplicate["operation"]["request_id"])
        self.assertEqual(len(self.client.refund_calls), 1)
        self.assertEqual(self.client.get_capture_calls, 2)

    def test_concurrent_approval_has_one_post_and_uncertain_result_never_retries(self):
        case = self.reviewed()
        entered, release = threading.Event(), threading.Event()

        def hold():
            entered.set()
            self.assertTrue(release.wait(3))

        self.client.on_post = hold
        self.client.post_error = PayPalError("uncertain", error_name="TEST_TIMEOUT")
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.service.approve, case["id"], case["latest_review"]["review_hash"])
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaises(ServiceError) as caught:
                    self.service.approve(case["id"], case["latest_review"]["review_hash"])
                self.assertEqual(caught.exception.code, "busy")
                with self.assertRaises(ServiceError) as caught:
                    self.service.resolve_case(case["id"], case["case_version"], item_used=None,
                                              resolution_note="A new question arrived during submission.")
                self.assertEqual(caught.exception.code, "busy")
            finally:
                release.set()
            result = first.result()
        self.assertEqual(result["state"], "uncertain")
        self.assertIsNone(result["operation"]["refund_id"])
        self.service = self.make_service()
        self.service.approve(case["id"], case["latest_review"]["review_hash"])
        self.service.refresh(case["id"])
        self.assertEqual(len(self.client.refund_calls), 1)
        self.assertEqual(self.client.readback_calls, [])

    def test_pending_refresh_is_get_only_and_verified_lookup_failure_keeps_proof(self):
        case = self.reviewed()
        self.client.refund_status = "PENDING"
        pending = self.service.approve(case["id"], case["latest_review"]["review_hash"])
        self.assertEqual(pending["state"], "pending")
        self.client.refund_status = "COMPLETED"
        verified = self.service.refresh(case["id"])
        self.assertEqual(verified["state"], "verified")
        self.client.get_error = PayPalError("uncertain", error_name="TEST_LOOKUP_FAILED")
        retained = self.service.refresh(case["id"])
        self.assertEqual(retained["state"], "verified")
        self.assertEqual(retained["error"]["code"], "readback_conflict")
        self.assertEqual(len(self.client.refund_calls), 1)

    def test_restart_recovers_interrupted_review_without_automatic_inference(self):
        case = self.create()
        with sqlite3.connect(self.service.db_path) as connection:
            connection.execute("INSERT INTO reviews(id,case_id,created_at,status) VALUES('interrupted',?,?,'reviewing')",
                               (case["id"], datetime.now(timezone.utc).isoformat()))
            connection.execute("UPDATE cases SET latest_review_id='interrupted' WHERE id=?", (case["id"],))
        restarted = self.make_service()
        recovered = restarted.get_case(case["id"])
        self.assertEqual(recovered["state"], "review_incomplete")
        self.assertTrue(recovered["can_review"])
        self.assertEqual(self.generator.calls, 0)
        self.assertEqual(self.client.refund_calls, [])

    def test_missing_facts_reach_analysis_and_useful_result_is_not_automatically_rerun(self):
        case = self.create(item_used=None, request_date=None,
                           customer_message="Customer: Please refund the item.\n\nSupport: When did you first request this?")
        self.assertIsNone(case["item_used"])
        self.assertIsNone(case["request_date"])
        self.assertEqual([s["id"] for s in case["sources"]], ["M1", "M2"])
        self.assertTrue(case["can_resolve"])
        self.assertTrue(case["can_review"])
        analyzed = self.service.review_case(case["id"])
        self.assertEqual(self.generator.calls, 1)
        self.assertEqual(analyzed["state"], "needs_information", analyzed)
        self.assertEqual(analyzed["latest_review"]["status"], "REVIEW_NEEDS_INFORMATION")
        self.assertEqual(analyzed["latest_review"]["accepted_recommendation"], "request_information")
        self.assertEqual(analyzed["latest_review"]["evidence"]["validation_status"], "valid")
        evidence = analyzed["latest_review"]["evidence"]
        self.assertTrue(all(issue["owner"] == "merchant" for issue in evidence["issues"]))
        for issue in evidence["issues"]:
            self.assertNotIn(issue["question"], evidence["reply_draft"])
        self.assertIsNone(analyzed["latest_review"]["error"])
        self.assertFalse(analyzed["can_review"])
        self.assertFalse(analyzed["can_approve"])
        again = self.service.review_case(case["id"])
        self.assertEqual(again["latest_review"]["review_hash"], analyzed["latest_review"]["review_hash"])
        self.assertEqual(self.generator.calls, 1)
        with self.assertRaises(ServiceError):
            self.service.approve(case["id"], analyzed["latest_review"]["review_hash"])
        self.assertEqual(self.client.refund_calls, [])

    def test_resolution_preserves_snapshots_and_reports_and_requires_a_new_review(self):
        first = self.reviewed(item_used=None, request_date=None)
        first_review = first["latest_review"]
        old_path = self.data / "reviews" / first_review["id"] / "report.json"
        old_report = old_path.read_bytes()
        with sqlite3.connect(self.service.db_path) as db:
            original_snapshot = db.execute("SELECT case_json,case_version FROM cases WHERE id=?", (first["id"],)).fetchone()
        resolved = self.service.resolve_case(first["id"], first["case_version"], item_used=False,
                                            request_date=datetime.now(timezone.utc).date().isoformat(),
                                            resolution_note="Merchant inspected the item and checked the received date.")
        self.assertNotEqual(resolved["case_version"], first["case_version"])
        self.assertIsNone(resolved["latest_review"])
        self.assertEqual(len(resolved["reviews"]), 1)
        self.assertEqual(len(resolved["revision_history"]), 2)
        self.assertEqual(resolved["revision_history"][0]["case_version"], first["case_version"])
        self.assertFalse(resolved["can_approve"])
        self.assertTrue(resolved["can_review"])
        with self.assertRaises(ServiceError) as caught:
            self.service.approve(first["id"], first_review["review_hash"])
        self.assertEqual(caught.exception.code, "stale_review")
        with self.assertRaises(ServiceError) as caught:
            self.service.resolve_case(first["id"], first["case_version"], item_used=True,
                                      resolution_note="This client had a stale revision.")
        self.assertEqual(caught.exception.code, "stale_case")
        repeated = self.service.resolve_case(first["id"], resolved["case_version"], item_used=False)
        self.assertEqual(repeated["case_version"], resolved["case_version"])
        self.assertEqual(len(repeated["revision_history"]), 2)
        self.assertEqual(self.generator.calls, 1)
        self.service = self.make_service()
        current = self.service.review_case(first["id"])
        self.assertTrue(current["can_approve"], current)
        self.assertEqual(current["latest_review"]["case_version"], resolved["case_version"])
        self.assertNotEqual(current["latest_review"]["review_hash"], first_review["review_hash"])
        self.assertEqual(old_path.read_bytes(), old_report)
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT case_json,case_version FROM cases WHERE id=?", (first["id"],)).fetchone(), original_snapshot)
            for statement in ("UPDATE case_revisions SET case_json='{}' WHERE case_id=?",
                              "DELETE FROM case_revisions WHERE case_id=?",
                              "UPDATE cases SET case_json='{}' WHERE id=?"):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(statement, (first["id"],))
        self.assertEqual(self.client.refund_calls, [])

    def test_resolution_requires_explanation_and_nullable_facts_are_not_coerced(self):
        case = self.create()
        for fields in ({"item_used": None}, {"item_used": None, "resolution_note": "   "},
                       {"item_used": 0, "resolution_note": "Invalid boolean"},
                       {"request_date": "1900-01-01", "resolution_note": "Before capture"},
                       {"request_date": "2999-01-01", "resolution_note": "Future date"},
                       {"resolution_note": "x" * 2001}):
            with self.subTest(fields=fields), self.assertRaises(ServiceError):
                self.service.resolve_case(case["id"], case["case_version"], **fields)
        unchanged = self.service.get_case(case["id"])
        self.assertEqual(len(unchanged["revision_history"]), 1)
        unknown = self.service.resolve_case(case["id"], case["case_version"], item_used=None,
                                           resolution_note="The merchant cannot yet confirm physical condition.")
        self.assertIsNone(unknown["item_used"])
        self.assertEqual(unknown["request_date"], case["request_date"])
        unknown = self.service.resolve_case(case["id"], unknown["case_version"], request_date=None,
                                           resolution_note="The original request date also needs confirmation.")
        self.assertIsNone(unknown["request_date"])
        self.assertIsNone(unknown["item_used"])
        self.assertEqual(self.client.get_capture_calls, 1)
        self.assertEqual(self.generator.calls, 0)

    def test_prepared_or_uncertain_operation_locks_revisions_even_if_case_link_was_interrupted(self):
        for state in ("prepared_unlinked", "prepared_linked", "uncertain"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                client, generator = TestClient(), TestGenerator()
                service = RefundService(directory, client, MERCHANT, EMAIL, generator=generator,
                                        approval_kind="test_operator", approved_by="automated-test")
                case = service.create_case(CAPTURE, "Please refund my unused item.", False, datetime.now(timezone.utc).date().isoformat())
                case = service.review_case(case["id"])
                snapshot = {"capture_id": CAPTURE, "amount_minor": 3900, "currency": "USD",
                            "case_version": case["case_version"], "policy_version": service.policy["version"],
                            "review_hash": case["latest_review"]["review_hash"]}
                operation = service.payments.prepare(snapshot, response("GET", f"/v2/payments/captures/{CAPTURE}", client.capture),
                                                     approval_kind="test_operator", approved_by="automated-test")
                if state == "prepared_linked":
                    with sqlite3.connect(service.db_path) as db:
                        db.execute("UPDATE cases SET operation_id=? WHERE id=?", (operation["id"], case["id"]))
                elif state == "uncertain":
                    service.payments.claim(operation["id"], snapshot)
                    service.payments.record_submission(operation["id"], error={"category": "uncertain", "error_name": "TEST_TIMEOUT"})
                current = service.get_case(case["id"])
                self.assertFalse(current["can_resolve"])
                self.assertEqual(current["operation"]["id"], operation["id"])
                with self.assertRaises(ServiceError) as caught:
                    service.resolve_case(case["id"], case["case_version"], item_used=None, resolution_note="Late evidence arrived.")
                self.assertEqual(caught.exception.code, "approval_exists")
                self.assertEqual(len(service.get_case(case["id"])["revision_history"]), 1)
                self.assertEqual(client.refund_calls, [])

    def test_fabricated_source_conflict_and_model_refund_with_unknown_facts_never_pay(self):
        for problem in ("fabricated_quote", "conflict", "unconfirmed_fact"):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as directory:
                client, generator = TestClient(), TestGenerator()
                generator.bad_quote = problem == "fabricated_quote"
                if problem == "conflict":
                    generator.issues = [{"kind": "conflict", "field": "item_used", "source_ids": ["M1", "M2"], "owner": "customer",
                                         "detail": "The conversation contradicts itself.", "question": "Was the item used?"}]
                if problem == "unconfirmed_fact":
                    generator.report_missing = False
                service = RefundService(directory, client, MERCHANT, EMAIL, generator=generator)
                case = service.create_case(CAPTURE, "Customer: I did not use it.\n\nCustomer: I used it twice.",
                                           None if problem == "unconfirmed_fact" else False,
                                           datetime.now(timezone.utc).date().isoformat())
                case = service.review_case(case["id"])
                self.assertFalse(case["can_approve"], case)
                with self.assertRaises(ServiceError):
                    service.approve(case["id"], case["latest_review"]["review_hash"])
                self.assertEqual(client.refund_calls, [])

    def test_customer_reply_is_composed_from_customer_questions_not_a_model_reply_field(self):
        question = "Could you share the receipt for the other payment?"
        self.generator.recommendation = "request_information"
        self.generator.issues = [{"kind": "conflict", "field": "amount", "source_ids": ["M1", "M2"],
                                  "owner": "customer", "detail": "The customer amount differs from this payment.",
                                  "question": question}]
        case = self.reviewed(customer_message="Customer: Please refund the $49 I paid.\n\nSupport: This payment shows $39.")
        self.assertEqual(case["state"], "needs_information", case)
        evidence = case["latest_review"]["evidence"]
        self.assertIn(question, evidence["reply_draft"])
        path = self.data / "reviews" / case["latest_review"]["id"] / "report.json"
        report = json.loads(path.read_text())
        self.assertNotIn("reply_draft", json.loads(report["model_response_text"]))
        self.assertFalse(case["can_approve"])
        self.assertEqual(self.client.refund_calls, [])

    def test_returning_to_earlier_values_does_not_revive_an_old_version(self):
        case = self.create()
        first = self.service.resolve_case(case["id"], case["case_version"], resolution_note="Merchant clarification.")
        second = self.service.resolve_case(case["id"], first["case_version"], item_used=True)
        third = self.service.resolve_case(case["id"], second["case_version"], item_used=False)
        self.assertEqual(third["item_used"], first["item_used"])
        self.assertEqual(third["resolution_note"], first["resolution_note"])
        self.assertNotEqual(third["case_version"], first["case_version"])
        with self.assertRaises(ServiceError) as caught:
            self.service.resolve_case(case["id"], first["case_version"], item_used=True)
        self.assertEqual(caught.exception.code, "stale_case")
        self.assertEqual(len(third["revision_history"]), 4)

    def test_malformed_model_fields_remain_in_report_without_breaking_public_case_shape(self):
        self.generator.corrupted_fields = {"rationale": {"unexpected": "object"}, "citations": "not an array",
                                           "missing_information": {"item_used": "unknown"}}
        case = self.reviewed()
        self.assertEqual(case["state"], "review_incomplete")
        self.assertIsNone(case["latest_review"]["rationale"])
        self.assertEqual(case["latest_review"]["citations"], [])
        self.assertEqual(case["latest_review"]["missing_information"], [])
        report = json.loads((self.data / "reviews" / case["latest_review"]["id"] / "report.json").read_text())
        self.assertEqual(report["assessment"]["original_model_response"]["rationale"], {"unexpected": "object"})
        self.assertFalse(case["can_approve"])
        self.assertEqual(self.client.refund_calls, [])

    def test_concurrent_resolutions_create_only_one_new_revision(self):
        case = self.create()
        other = self.make_service()
        start = threading.Barrier(2)

        def resolve(service, value):
            start.wait(2)
            try:
                return service.resolve_case(case["id"], case["case_version"], item_used=value,
                                            resolution_note="Merchant clarification."), None
            except ServiceError as exc:
                return None, exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [future.result() for future in [pool.submit(resolve, self.service, True),
                                                       pool.submit(resolve, other, None)]]
        self.assertEqual(sum(row is not None for row, error in outcomes), 1)
        self.assertIn(next(error for row, error in outcomes if error), {"busy", "stale_case"})
        self.assertEqual(len(self.service.get_case(case["id"])["revision_history"]), 2)
        self.assertEqual(self.client.refund_calls, [])

    def test_legacy_snapshot_and_finished_report_remain_readable_after_revision_migration(self):
        data = self.data / "legacy"
        data.mkdir()
        created = datetime.now(timezone.utc).isoformat()
        purchase = datetime.fromisoformat(self.client.capture["create_time"]).date().isoformat()
        case = {"verified_transaction": {"capture_id": CAPTURE, "currency": "USD", "captured_amount_minor": 3900,
                                         "completed_refunds_minor": 0, "purchase_date": purchase,
                                         "request_date": datetime.now(timezone.utc).date().isoformat(), "item_used": False},
                "customer_message": "\n\n".join(f"Old source paragraph {index}." for index in range(13))}
        original = json.dumps(case, sort_keys=True, separators=(",", ":"))
        version = hashlib.sha256(original.encode()).hexdigest()
        capture = {"capture_id": CAPTURE, "amount_minor": 3900, "currency": "USD", "purchase_date": purchase,
                   "merchant_id": MERCHANT, "merchant_email": EMAIL, "provenance": "injected_test_transport", "checked_at": created}
        report = {"case": case, "policy": self.service.policy, "policy_version": self.service.policy["version"],
                  "status": "REVIEW_READY", "error": None, "accepted_recommendation": "refund",
                  "generator_invocations": 1, "runtime_provenance": "injected_test_generator",
                  "model": {"model": MODEL, "digest": DIGEST, "completed_model_response": True},
                  "assessment": {"status": "REVIEW_READY", "errors": [], "accepted_recommendation": "refund",
                                 "original_model_response": {"recommendation": "refund", "missing_information": [],
                                                             "citations": [{"id": "P1", "quote": self.service.policy["clauses"]["P1"]}]}}}
        raw = json.dumps(report).encode()
        report_path = data / "reviews" / "legacy-review" / "report.json"
        report_path.parent.mkdir(parents=True)
        report_path.write_bytes(raw)
        with sqlite3.connect(data / "desk.sqlite3") as db:
            db.executescript("""
                CREATE TABLE cases(id TEXT PRIMARY KEY,capture_id TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,
                    case_version TEXT NOT NULL,case_json TEXT NOT NULL,capture_json TEXT NOT NULL,policy_json TEXT NOT NULL,
                    latest_review_id TEXT,operation_id TEXT,error_json TEXT);
                CREATE TABLE reviews(id TEXT PRIMARY KEY,case_id TEXT NOT NULL,created_at TEXT NOT NULL,finished_at TEXT,
                    status TEXT NOT NULL,report_hash TEXT,error_json TEXT);
            """)
            db.execute("INSERT INTO cases VALUES(?,?,?,?,?,?,?,'legacy-review',NULL,NULL)",
                       ("legacy-case", CAPTURE, created, version, original, json.dumps(capture), json.dumps(self.service.policy)))
            db.execute("INSERT INTO reviews VALUES('legacy-review','legacy-case',?,?,'REVIEW_READY',?,NULL)",
                       (created, created, hashlib.sha256(raw).hexdigest()))
        service = RefundService(data, self.client, MERCHANT, EMAIL, generator=self.generator)
        loaded = service.get_case("legacy-case")
        self.assertEqual(loaded["case_version"], version)
        self.assertEqual(loaded["latest_review"]["status"], "REVIEW_READY")
        self.assertIsNone(loaded["latest_review"]["evidence"])
        self.assertEqual(loaded["sources"], [{"id": "M1", "text": case["customer_message"]}])
        self.assertEqual(loaded["revision_history"], [{"case_version": version, "created_at": created, "resolution_note": None}])
        revised = service.resolve_case("legacy-case", version, customer_message="Customer asks for a refund.",
                                       resolution_note="Merchant supplied a bounded transcription for the new analysis.")
        self.assertIsNone(revised["latest_review"])
        self.assertEqual(len(revised["revision_history"]), 2)
        self.assertEqual(report_path.read_bytes(), raw)
        with sqlite3.connect(service.db_path) as db:
            self.assertEqual(db.execute("SELECT case_json FROM cases WHERE id='legacy-case'").fetchone()[0], original)
        self.assertEqual(self.generator.calls, 0)
        self.assertEqual(self.client.refund_calls, [])

    def test_runtime_selection_is_explicit_and_public_descriptors_exclude_private_data(self):
        self.assertEqual(self.service.ai_runtime, {"provider": "test", "model": MODEL, "location": "test"})
        case = self.reviewed()
        self.assertEqual(case["latest_review"]["runtime_provenance"], "injected_test_generator")
        self.assertEqual(case["latest_review"]["model_calls"], 0)
        self.assertNotIn(EMAIL, json.dumps(self.generator.inputs))
        runtime = self.groq_runtime()
        configured = RefundService(self.data / "groq", TestClient(), MERCHANT, EMAIL, runtime=runtime)
        self.assertEqual(configured.ai_runtime, {"provider": "groq", "model": "openai/gpt-oss-120b", "location": "hosted"})
        public = configured.create_case(CAPTURE, "Customer requests a refund.")
        encoded = json.dumps({"runtime": configured.ai_runtime, "case": public})
        for private in ("test-only-secret-not-real", "test-only-groq.json", EMAIL):
            self.assertNotIn(private, encoded)
        bad = self.data / "arbitrary-runtime"
        with self.assertRaises(ServiceError) as caught:
            RefundService(bad, TestClient(), MERCHANT, EMAIL, runtime={"provider": "arbitrary"})
        self.assertEqual(caught.exception.code, "invalid_runtime")
        self.assertFalse(bad.exists())

    def test_actual_completion_guard_matches_provider_specific_proof_not_test_labels(self):
        configured = RefundService(self.data / "groq", TestClient(), MERCHANT, EMAIL, runtime=self.groq_runtime())
        groq = completion_fixture("groq")
        self.assertTrue(configured._model_completed(groq))
        self.assertFalse(self.service._model_completed(groq))
        self.assertTrue(self.service._model_completed(groq, selected=False))
        local = completion_fixture("local")
        self.assertTrue(self.service._model_completed(local))
        self.assertFalse(configured._model_completed(local))
        self.assertTrue(configured._model_completed(local, selected=False))
        for field, value in (("model", MODEL), ("endpoint", "https://outside.invalid"),
                             ("http_status", 202), ("finish_reason", "length"), ("response_id", None),
                             ("provider", "arbitrary"), ("digest", "a" * 64)):
            invalid = deepcopy(groq)
            invalid["model"][field] = value
            with self.subTest(field=field):
                self.assertFalse(configured._model_completed(invalid))
        for field, value in (("runtime_provenance", "local_ollama"), ("model_calls", 0),
                             ("completed_model_responses", 0), ("runtime_provenance", "injected_test_generator")):
            invalid = deepcopy(groq)
            invalid[field] = value
            with self.subTest(field=field, value=value):
                self.assertFalse(configured._model_completed(invalid))
        injected = deepcopy(local)
        injected.update(runtime_provenance="injected_test_generator", model_calls=0,
                        model_request_attempts=0, completed_model_responses=0)
        self.assertTrue(self.service._model_completed(injected))
        self.assertFalse(configured._model_completed(injected))
        # Displaying historical test output must never make it real-provider proof.
        self.assertTrue(configured._model_completed(injected, selected=False, historical=True))
        self.assertFalse(configured._model_completed(injected, selected=False))

    def test_provider_change_preserves_history_and_prepared_approval_without_silent_inference(self):
        case = self.reviewed()
        path = self.data / "reviews" / case["latest_review"]["id"] / "report.json"
        report = json.loads(path.read_text())
        report.update(completion_fixture("groq"))
        # This explicit stored-receipt fixture tests loading old provider evidence,
        # not provider execution. It lives only in this temporary test database.
        fixture_id = "offline-groq-history"
        fixture_path = self.data / "reviews" / fixture_id / "report.json"
        fixture_path.parent.mkdir()
        raw = json.dumps(report).encode()
        fixture_path.write_bytes(raw)
        review_hash = hashlib.sha256(raw).hexdigest()
        created = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.service.db_path) as db:
            db.execute("INSERT INTO reviews(id,case_id,created_at,finished_at,status,report_hash,case_version) VALUES(?,?,?,?,'REVIEW_READY',?,?)",
                       (fixture_id, case["id"], created, created, review_hash, case["case_version"]))
            db.execute("UPDATE cases SET latest_review_id=? WHERE id=?", (fixture_id, case["id"]))
        historical = self.service.get_case(case["id"])
        self.assertEqual(historical["latest_review"]["status"], "REVIEW_READY")
        self.assertEqual(historical["latest_review"]["runtime_provenance"], "hosted_groq")
        self.assertEqual(historical["latest_review"]["model"]["model"], "openai/gpt-oss-120b")
        self.assertFalse(historical["can_approve"])
        self.assertTrue(historical["can_review"])
        with self.assertRaises(ServiceError):
            self.service.approve(case["id"], review_hash)
        selected = RefundService(self.data, self.client, MERCHANT, EMAIL, runtime=self.groq_runtime(),
                                 approval_kind="test_operator", approved_by="automated-test")
        self.assertTrue(selected.get_case(case["id"])["can_approve"])
        snapshot = {"capture_id": CAPTURE, "amount_minor": 3900, "currency": "USD", "case_version": case["case_version"],
                    "policy_version": selected.policy["version"], "review_hash": review_hash}
        operation = selected.payments.prepare(snapshot, response("GET", f"/v2/payments/captures/{CAPTURE}", self.client.capture),
                                              approval_kind="test_operator", approved_by="automated-test")
        # A pre-existing immutable approval can resume using its supported saved
        # provider proof, even after startup returns to local inference.
        resumed = RefundService(self.data, self.client, MERCHANT, EMAIL,
                                approval_kind="test_operator", approved_by="automated-test")
        before = resumed.get_case(case["id"])
        self.assertTrue(before["can_approve"])
        self.assertFalse(before["can_review"])
        done = resumed.approve(case["id"], review_hash)
        self.assertEqual(done["state"], "verified")
        self.assertEqual(done["operation"]["id"], operation["id"])
        self.assertEqual(done["operation"]["approval"]["snapshot"]["review_hash"], review_hash)
        self.assertEqual(self.generator.calls, 1)
        self.assertEqual(len(self.client.refund_calls), 1)


if __name__ == "__main__":
    unittest.main()
