"""SQLite tests with authored PayPal-shaped fixtures; no payment or HTTP calls."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid

from payment_state import PaymentStateError, PaymentStore


MERCHANT = "MERCHANT-TEST"
SNAPSHOT = {"capture_id": "CAPTURE-TEST", "amount_minor": 3900, "currency": "USD",
            "case_version": "case-v1", "policy_version": "policy-v1", "review_hash": "a" * 64}


def wrapper(method, path, data, *, request_id=None, status=200):
    return {"environment": "sandbox", "provenance": "injected_test_transport",
            "method": method, "path": path, "status": status,
            "debug_id": "AUTHORED-TEST-DEBUG", "request_id": request_id, "data": data}


def capture():
    return wrapper("GET", "/v2/payments/captures/CAPTURE-TEST",
                   {"id": "CAPTURE-TEST", "status": "COMPLETED",
                    "amount": {"value": "39.00", "currency_code": "USD"}})


def refund(operation, *, method="GET", status="COMPLETED"):
    path = "/v2/payments/refunds/REFUND-TEST" if method == "GET" else "/v2/payments/captures/CAPTURE-TEST/refund"
    return wrapper(method, path, {"id": "REFUND-TEST", "status": status,
                   "amount": {"value": "39.00", "currency_code": "USD"},
                   "links": [{"rel": "up", "href": "https://api-m.sandbox.paypal.com/v2/payments/captures/CAPTURE-TEST"}]},
                   request_id=operation["request_id"] if method == "POST" else None,
                   status=200 if method == "GET" else 201)


class PaymentStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="refund-desk-state-test-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "payments.sqlite3"
        self.store = PaymentStore(self.path, MERCHANT)

    def prepare(self):
        return self.store.prepare(SNAPSHOT, capture(), approval_kind="test_operator", approved_by="test-operator")

    def pending(self):
        operation = self.prepare()
        self.assertTrue(self.store.claim(operation["id"], SNAPSHOT)["claimed"])
        return self.store.record_submission(operation["id"], refund(operation, method="POST"))

    def test_duplicate_preparation_preserves_approval_and_stable_request_id(self):
        first = self.prepare()
        second = self.prepare()
        self.assertEqual(first, second)
        self.assertEqual(str(uuid.UUID(first["request_id"])), first["request_id"])
        self.assertEqual(first["approval"]["approval_kind"], "test_operator")
        self.assertEqual(first["events"][0]["details"]["merchant_evidence"], "not_returned")
        with self.assertRaises(PaymentStateError):
            self.store.prepare(SNAPSHOT, capture(), approval_kind="human_ui", approved_by="test-operator")

    def test_two_independent_connections_can_claim_only_once(self):
        operation = self.prepare()
        barrier = threading.Barrier(2)

        def claim():
            store = PaymentStore(self.path, MERCHANT)
            barrier.wait(timeout=3)
            return store.claim(operation["id"], SNAPSHOT)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result() for future in [pool.submit(claim), pool.submit(claim)]]
        self.assertEqual(sum(result["claimed"] for result in results), 1)
        persisted = PaymentStore(self.path, MERCHANT).get(operation["id"])
        self.assertEqual(persisted["state"], "submitting")
        self.assertEqual(sum(event["kind"] == "submission_claimed" for event in persisted["events"]), 1)

    def test_restart_and_ambiguous_response_never_grant_another_claim(self):
        operation = self.prepare()
        self.store.claim(operation["id"], SNAPSHOT)
        restarted = PaymentStore(self.path, MERCHANT)
        self.assertFalse(restarted.claim(operation["id"], SNAPSHOT)["claimed"])
        uncertain = restarted.record_submission(operation["id"], error={"category": "uncertain", "error_name": "CLIENT_INTERRUPTED"})
        self.assertEqual(uncertain["state"], "uncertain")
        self.assertFalse(restarted.claim(operation["id"], SNAPSHOT)["claimed"])
        unresolved = restarted.record_readback(operation["id"], refund(operation))
        self.assertEqual(unresolved["state"], "uncertain")
        self.assertIsNone(unresolved["refund_id"])
        self.assertEqual(unresolved["request_id"], operation["request_id"])

    def test_stale_amount_case_policy_or_review_cannot_be_claimed(self):
        operation = self.prepare()
        for key, value in (("amount_minor", 3901), ("case_version", "case-v2"),
                           ("policy_version", "policy-v2"), ("review_hash", "b" * 64),
                           ("capture_id", "OTHER-CAPTURE")):
            with self.subTest(key=key), self.assertRaises(PaymentStateError) as caught:
                self.store.claim(operation["id"], dict(SNAPSHOT, **{key: value}))
            self.assertEqual(caught.exception.code, "stale_approval")
        self.assertEqual(self.store.get(operation["id"])["state"], "prepared")

    def test_post_completed_is_only_pending_and_exact_get_verifies(self):
        operation = self.pending()
        self.assertEqual(operation["state"], "pending")
        self.assertEqual(operation["provider_status"], "COMPLETED")
        self.assertIsNone(operation["verified_at"])
        verified = self.store.record_readback(operation["id"], refund(operation))
        self.assertEqual(verified["state"], "verified")
        self.assertEqual(verified["verification_provenance"], "injected_test_transport")
        self.assertIsNotNone(verified["verified_at"])
        self.assertEqual(verified["events"][-1]["details"]["merchant_evidence"], "not_returned")
        again = self.store.record_readback(operation["id"], refund(operation))
        self.assertEqual(again, verified)
        self.assertFalse(self.store.claim(operation["id"], SNAPSHOT)["claimed"])

    def test_wrong_method_refund_capture_amount_currency_or_payee_cannot_verify(self):
        for problem in ("method", "refund_id", "capture", "amount", "currency", "payee", "missing_link", "environment", "provenance"):
            with self.subTest(problem=problem):
                # Each fixture has a separate database; real operations stay immutable.
                self.store = PaymentStore(Path(self.temporary.name) / (problem + ".sqlite3"), MERCHANT)
                operation = self.pending()
                readback = refund(operation)
                if problem == "method":
                    readback["method"] = "POST"
                elif problem == "refund_id":
                    readback["data"]["id"] = "OTHER-REFUND"
                elif problem == "capture":
                    readback["data"]["links"][0]["href"] = "https://api-m.sandbox.paypal.com/v2/payments/captures/OTHER-CAPTURE"
                elif problem == "amount":
                    readback["data"]["amount"]["value"] = "38.99"
                elif problem == "currency":
                    readback["data"]["amount"]["currency_code"] = "EUR"
                elif problem == "payee":
                    readback["data"]["payee"] = {"merchant_id": "OTHER-MERCHANT"}
                elif problem == "missing_link":
                    readback["data"]["links"] = []
                elif problem == "environment":
                    readback["environment"] = "live"
                else:
                    readback["provenance"] = "paypal_sandbox"
                result = self.store.record_readback(operation["id"], readback)
                self.assertEqual(result["state"], "uncertain")
                self.assertIsNone(result["verified_at"])

    def test_pending_definite_failure_and_readback_error_remain_distinct(self):
        operation = self.pending()
        pending = self.store.record_readback(operation["id"], refund(operation, status="PENDING"))
        self.assertEqual(pending["state"], "pending")
        uncertain = self.store.record_readback(operation["id"], error={"category": "rejected", "status": 404, "error_name": "NOT_FOUND"})
        self.assertEqual(uncertain["state"], "uncertain")
        failed = self.store.record_readback(operation["id"], refund(operation, status="FAILED"))
        self.assertEqual(failed["state"], "failed")
        self.assertFalse(self.store.claim(operation["id"], SNAPSHOT)["claimed"])

    def test_definitive_post_rejection_is_failed_without_retry(self):
        operation = self.prepare()
        self.store.claim(operation["id"], SNAPSHOT)
        result = self.store.record_submission(operation["id"], error={"category": "rejected", "status": 422, "error_name": "REFUND_NOT_ALLOWED"})
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(result["refund_id"])
        self.assertFalse(self.store.claim(operation["id"], SNAPSHOT)["claimed"])

    def test_later_rejection_does_not_resolve_an_uncertain_submission(self):
        operation = self.prepare()
        self.store.claim(operation["id"], SNAPSHOT)
        self.store.record_submission(operation["id"], error={
            "category": "uncertain", "error_name": "HTTP_DEADLINE_EXCEEDED"})
        result = self.store.record_submission(operation["id"], error={
            "category": "rejected", "status": 422, "error_name": "REFUND_NOT_ALLOWED"})
        self.assertEqual(result["state"], "uncertain")
        self.assertIsNone(result["refund_id"])
        self.assertEqual(result["request_id"], operation["request_id"])
        self.assertEqual(self.store.get(operation["id"])["state"], "uncertain")
        self.assertFalse(self.store.claim(operation["id"], SNAPSHOT)["claimed"])
        self.assertEqual(result["events"][-1]["details"]["category"], "rejected")

    def test_mismatched_post_retains_known_refund_id_for_independent_readback(self):
        operation = self.prepare()
        self.store.claim(operation["id"], SNAPSHOT)
        response = refund(operation, method="POST")
        response["data"]["amount"]["currency_code"] = "EUR"
        uncertain = self.store.record_submission(operation["id"], response)
        self.assertEqual(uncertain["state"], "uncertain")
        self.assertEqual(uncertain["refund_id"], "REFUND-TEST")
        self.assertFalse(self.store.claim(operation["id"], SNAPSHOT)["claimed"])
        verified = self.store.record_readback(operation["id"], refund(operation))
        self.assertEqual(verified["state"], "verified")

    def test_refund_id_collision_and_cross_merchant_access_fail_closed(self):
        first = self.pending()
        other_snapshot = dict(SNAPSHOT, capture_id="SECOND-CAPTURE")
        other_capture = capture()
        other_capture["path"] = "/v2/payments/captures/SECOND-CAPTURE"
        other_capture["data"]["id"] = "SECOND-CAPTURE"
        second = self.store.prepare(other_snapshot, other_capture, approval_kind="test_operator", approved_by="test-operator")
        self.store.claim(second["id"], other_snapshot)
        wrong_refund = refund(second, method="POST")
        wrong_refund["path"] = "/v2/payments/captures/SECOND-CAPTURE/refund"
        result = self.store.record_submission(second["id"], wrong_refund)
        self.assertEqual(result["state"], "uncertain")
        self.assertIsNone(result["refund_id"])
        self.assertEqual(self.store.get(first["id"])["refund_id"], "REFUND-TEST")
        with self.assertRaises(PaymentStateError) as caught:
            PaymentStore(self.path, "OTHER-MERCHANT").get(first["id"])
        self.assertEqual(caught.exception.code, "not_found")
        with self.assertRaises(PaymentStateError):
            PaymentStore(":memory:", MERCHANT)

    def test_approval_and_event_records_are_immutable(self):
        operation = self.prepare()
        with sqlite3.connect(self.path) as connection:
            for sql in ("UPDATE approvals SET approval_kind='human_ui'", "DELETE FROM approvals",
                        "UPDATE events SET kind='changed'", "DELETE FROM events"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(sql)
        self.assertEqual(self.store.get(operation["id"]), operation)

    def test_invalid_capture_money_and_request_id_fail_closed(self):
        for amount in (True, 39.0, "3900", 0, -1):
            with self.subTest(amount=amount), self.assertRaises(PaymentStateError):
                self.store.prepare(dict(SNAPSHOT, amount_minor=amount), capture(), approval_kind="test_operator", approved_by="test")
        bad_capture = capture()
        bad_capture["data"]["payee"] = {"merchant_id": "OTHER-MERCHANT"}
        with self.assertRaises(PaymentStateError):
            self.store.prepare(SNAPSHOT, bad_capture, approval_kind="test_operator", approved_by="test")
        operation = self.prepare()
        self.store.claim(operation["id"], SNAPSHOT)
        response = refund(operation, method="POST")
        response["request_id"] = str(uuid.uuid4())
        result = self.store.record_submission(operation["id"], response)
        self.assertEqual(result["state"], "uncertain")
        self.assertIsNone(result["refund_id"])


if __name__ == "__main__":
    unittest.main()
