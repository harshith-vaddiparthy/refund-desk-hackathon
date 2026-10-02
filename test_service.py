"""Focused coordinator integration tests with explicit test clients; no network."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from local_model import DIGEST, MODEL
from paypal import PayPalError
from service import RefundService, ServiceError


MERCHANT = "MERCHANT123"
EMAIL = "merchant@example.test"
CAPTURE = "CAPTURE123"


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

    def __call__(self, case, policy):
        self.calls += 1
        answer = {"recommendation": self.recommendation, "rationale": "Test response citing the fixed policy.",
                  "citations": [{"id": "P1", "quote": policy["clauses"]["P1"]}], "missing_information": []}
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
        return self.service.create_case(CAPTURE, "My item is unused. Please refund it.", options.get("item_used", False), options.get("request_date"))

    def reviewed(self, **options):
        case = self.create(**options)
        return self.service.review_case(case["id"])

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
        with self.assertRaises(ServiceError):
            self.service.create_case(CAPTURE, "Refund request", None)

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
                row = service.create_case(CAPTURE, "Please refund", problem == "used")
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


if __name__ == "__main__":
    unittest.main()
