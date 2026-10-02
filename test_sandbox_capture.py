"""Offline setup-helper checks. No PayPal requests or credentials."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest

from paypal import PayPalError
from sandbox_capture import capture, create


EMAIL = "synthetic-merchant@example.invalid"


class FakeClient:
    def __init__(self):
        self.create_calls = []
        self.capture_calls = []
        self.order_status = "APPROVED"
        self.capture_error = None
        self.capture_amount = "39.00"

    def create_order(self, payload, request_id):
        self.create_calls.append((deepcopy(payload), request_id))
        return {"provenance": "injected_test_transport", "data": {"id": "ORDER123", "links": [{
            "rel": "payer-action", "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER123"}]}}

    def get_order(self, order_id):
        return {"provenance": "injected_test_transport", "data": {"id": order_id, "status": self.order_status,
            "purchase_units": [{"amount": {"currency_code": "USD", "value": "39.00"},
                                "payee": {"email_address": EMAIL}}]}}

    def capture_order(self, order_id, request_id):
        self.capture_calls.append((order_id, request_id))
        if self.capture_error:
            raise self.capture_error
        return {"provenance": "injected_test_transport", "data": {"id": order_id,
            "purchase_units": [{"payments": {"captures": [{"id": "CAPTURE123"}]}}]}}

    def get_capture(self, capture_id):
        return {"provenance": "injected_test_transport", "data": {"id": capture_id, "status": "COMPLETED",
            "amount": {"currency_code": "USD", "value": self.capture_amount},
            "payee": {"email_address": EMAIL, "merchant_id": "MERCHANT123"},
            "supplementary_data": {"related_ids": {"order_id": "ORDER123"}}}}


class SandboxCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "purchase"
        self.client = FakeClient()
        create(self.client, self.directory, EMAIL, "http://127.0.0.1:8749/")

    def record(self):
        return json.loads((self.directory / "purchase.json").read_text())

    def test_single_capture_is_read_back_and_phase_cannot_be_replayed(self):
        initial = self.record()
        result = capture(self.client, self.directory)
        self.assertEqual(result["stage"], "capture_verified")
        self.assertEqual(result["capture_id"], "CAPTURE123")
        self.assertEqual(self.client.capture_calls, [("ORDER123", initial["capture_request_id"])])
        self.assertNotEqual(initial["order_request_id"], initial["capture_request_id"])
        with self.assertRaises(ValueError):
            capture(self.client, self.directory)
        self.assertEqual(len(self.client.capture_calls), 1)
        self.assertEqual(self.record()["capture_readback"]["provenance"], "injected_test_transport")

    def test_not_yet_approved_order_makes_no_capture_and_can_be_rechecked(self):
        self.client.order_status = "CREATED"
        with self.assertRaises(ValueError):
            capture(self.client, self.directory)
        self.assertEqual(self.client.capture_calls, [])
        self.assertEqual(self.record()["stage"], "awaiting_buyer")
        self.client.order_status = "APPROVED"
        self.assertEqual(capture(self.client, self.directory)["stage"], "capture_verified")

    def test_uncertain_capture_preserves_request_and_blocks_another_post(self):
        initial = self.record()
        self.client.capture_error = PayPalError("uncertain", error_name="HTTP_DEADLINE_EXCEEDED")
        with self.assertRaises(PayPalError):
            capture(self.client, self.directory)
        self.assertEqual(self.record()["stage"], "capture_uncertain")
        self.assertEqual(self.record()["capture_request_id"], initial["capture_request_id"])
        with self.assertRaises(ValueError):
            capture(self.client, self.directory)
        self.assertEqual(len(self.client.capture_calls), 1)

    def test_wrong_readback_keeps_capture_id_without_claiming_verification(self):
        self.client.capture_amount = "38.00"
        with self.assertRaises(ValueError):
            capture(self.client, self.directory)
        self.assertEqual(self.record()["stage"], "capture_pending_verification")
        self.assertEqual(self.record()["capture_id"], "CAPTURE123")
        with self.assertRaises(ValueError):
            capture(self.client, self.directory)
        self.assertEqual(len(self.client.capture_calls), 1)

    def test_concurrent_capture_invocations_send_only_one_post(self):
        entered, release = threading.Event(), threading.Event()
        original = self.client.get_order

        def held_get(order_id):
            entered.set()
            if not release.wait(timeout=3):
                raise AssertionError("Test did not release order lookup")
            return original(order_id)
        self.client.get_order = held_get
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(capture, self.client, self.directory)
            try:
                self.assertTrue(entered.wait(timeout=2))
                with self.assertRaises(BlockingIOError):
                    pool.submit(capture, self.client, self.directory).result(timeout=2)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=2)["stage"], "capture_verified")
        self.assertEqual(len(self.client.capture_calls), 1)


if __name__ == "__main__":
    unittest.main()
