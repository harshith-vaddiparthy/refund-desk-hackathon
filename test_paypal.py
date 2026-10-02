"""Offline PayPal client boundaries. Only synthetic credentials and responses."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import paypal
from paypal import PayPalClient, PayPalError, parse_usd_cents


CLIENT = "synthetic-client-id"
SECRET = "synthetic-client-secret"
TOKEN = "test-access-token-" + SECRET
REQUEST_ID = "b1bd1ca0-51d0-4d87-a497-e8c1b4a7289f"
AUTH = {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3600,
        "scope": "https://uri.paypal.com/services/payments/refund", "app_id": "APP-TEST"}
REFUND = {"id": "REFUND123", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "39.00"}}
REAL_POPEN = subprocess.Popen


def response(status, data):
    return {"status": status, "debug_id": "abc123def4567", "body": json.dumps(data)}


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((deepcopy(request), timeout))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


class PayPalTests(unittest.TestCase):
    def test_falsey_transport_never_falls_through_to_real_http(self):
        class FalseyTransport(Transport):
            def __bool__(self):
                return False

        with patch("paypal._bounded_http", side_effect=AssertionError("Unexpected real HTTP")) as live:
            for invalid in (False, 0, "", []):
                with self.subTest(value=invalid), self.assertRaises(ValueError):
                    PayPalClient(CLIENT, SECRET, transport=invalid)
            injected = FalseyTransport(response(200, AUTH))
            client = PayPalClient(CLIENT, SECRET, transport=injected)
            self.assertFalse(client.is_live)
            self.assertEqual(client.oauth()["provenance"], "injected_test_transport")
            self.assertEqual(len(injected.calls), 1)
            live.assert_not_called()

    def test_exact_money(self):
        for value, expected in [("39.00", 3900), ("39", 3900), ("39.1", 3910), ("0.01", 1), ("0", 0)]:
            self.assertEqual(parse_usd_cents(value), expected)
        for value in [39, 39.0, True, None, "1e2", "1.001", "-1.00", "+1", "NaN", " 39.00", "", "9" * 33]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_usd_cents(value)

    def test_oauth_is_private_and_token_is_reused_without_corruption(self):
        transport = Transport(response(200, AUTH), response(200, REFUND))
        client = PayPalClient(CLIENT, SECRET, transport=transport)
        receipt = client.oauth()
        self.assertEqual(receipt["data"]["expires_in"], 3600)
        self.assertNotIn("access_token", receipt["data"])
        for secret in (CLIENT, SECRET, TOKEN):
            self.assertNotIn(secret, json.dumps(receipt))
            self.assertNotIn(secret, repr(client))
        result = client.get_refund("REFUND123")
        self.assertEqual(result["data"], REFUND)
        self.assertEqual(transport.calls[0][0]["path"], "/v1/oauth2/token")
        self.assertEqual(transport.calls[0][0]["body"], "grant_type=client_credentials")
        self.assertTrue(transport.calls[0][0]["headers"]["Authorization"].startswith("Basic "))
        self.assertEqual(transport.calls[1][0]["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(result["method"], "GET")
        self.assertEqual(result["path"], "/v2/payments/refunds/REFUND123")
        self.assertEqual(result["provenance"], "injected_test_transport")

    def test_payment_requests_keep_caller_uuid_and_exact_amount_with_no_retry(self):
        transport = Transport(response(200, AUTH), response(201, REFUND), response(200, REFUND))
        client = PayPalClient(CLIENT, SECRET, transport=transport)
        first = client.refund_capture("CAPTURE123", 3900, REQUEST_ID)
        second = client.refund_capture("CAPTURE123", 3900, REQUEST_ID)
        self.assertEqual(first["status"], 201)
        self.assertEqual(second["status"], 200)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(transport.calls[1], transport.calls[2])
        request = transport.calls[1][0]
        self.assertEqual(request["headers"]["PayPal-Request-Id"], REQUEST_ID)
        self.assertEqual(request["headers"]["Prefer"], "return=representation")
        self.assertEqual(json.loads(request["body"]), {"amount": {"currency_code": "USD", "value": "39.00"}})
        self.assertEqual(first["request_id"], REQUEST_ID)

    def test_order_capture_and_reads_preserve_documented_resource_fields(self):
        capture = {"id": "CAPTURE123", "amount": {"currency_code": "USD", "value": "39.00"},
                   "status": "COMPLETED", "payee": {"merchant_id": "MERCHANT123"},
                   "supplementary_data": {"related_ids": {"order_id": "ORDER123"}}}
        order = {"id": "ORDER123", "status": "APPROVED"}
        transport = Transport(response(200, AUTH), response(201, order), response(200, order),
                              response(201, order), response(200, capture))
        client = PayPalClient(CLIENT, SECRET, transport=transport)
        payload = {"intent": "CAPTURE", "purchase_units": [{"amount": {"currency_code": "USD", "value": "39.00"}}]}
        client.create_order(payload, REQUEST_ID)
        client.get_order("ORDER123")
        client.capture_order("ORDER123", REQUEST_ID)
        result = client.get_capture("CAPTURE123")
        self.assertEqual(result["data"], capture)
        self.assertEqual([call[0]["path"] for call in transport.calls[1:]],
                         ["/v2/checkout/orders", "/v2/checkout/orders/ORDER123",
                          "/v2/checkout/orders/ORDER123/capture", "/v2/payments/captures/CAPTURE123"])
        self.assertEqual(transport.calls[3][0]["body"], "{}")
        self.assertNotIn("merchant_verified", result)

    def test_invalid_ids_amounts_and_orders_fail_before_authentication(self):
        transport = Transport()
        client = PayPalClient(CLIENT, SECRET, transport=transport)
        invalid = [lambda: client.get_capture("../secrets"),
                   lambda: client.get_order("https://api.paypal.com/ORDER"),
                   lambda: client.capture_order("ORDER123", None),
                   lambda: client.refund_capture("CAPTURE123", 3900, "new-id"),
                   lambda: client.refund_capture("CAPTURE123", 39.00, REQUEST_ID),
                   lambda: client.refund_capture("CAPTURE123", 0, REQUEST_ID),
                   lambda: client.create_order({"intent": "AUTHORIZE"}, REQUEST_ID)]
        for action in invalid:
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(transport.calls, [])

    def test_definitive_rejections_and_ambiguous_statuses_never_retry(self):
        for status, category in [(400, "rejected"), (401, "rejected"), (403, "rejected"),
                                 (422, "rejected"), (429, "rejected"), (408, "uncertain"),
                                 (409, "uncertain"), (500, "uncertain"), (503, "uncertain")]:
            with self.subTest(status=status):
                transport = Transport(response(200, AUTH), response(status, {"name": "TEST_REJECTION"}))
                client = PayPalClient(CLIENT, SECRET, transport=transport)
                with self.assertRaises(PayPalError) as caught:
                    client.refund_capture("CAPTURE123", 3900, REQUEST_ID)
                self.assertEqual(caught.exception.as_dict(), {"category": category, "status": status,
                    "debug_id": "abc123def4567", "error_name": "TEST_REJECTION"})
                self.assertEqual(len(transport.calls), 2)

    def test_transport_and_invalid_success_responses_remain_uncertain_and_secret_safe(self):
        cases = [TimeoutError(SECRET), {"transport_error": "REDIRECT_BLOCKED"},
                 {"status": 201, "body": "not JSON"},
                 {"status": 201, "body": "x" * (paypal.MAX_RESPONSE_BYTES + 1)},
                 response(201, {"id": float("nan")}),
                 {"status": 201, "body": '{"id":"one","id":"two"}'}]
        for failed in cases:
            with self.subTest(kind=type(failed).__name__):
                transport = Transport(response(200, AUTH), failed)
                client = PayPalClient(CLIENT, SECRET, transport=transport)
                with self.assertRaises(PayPalError) as caught:
                    client.refund_capture("CAPTURE123", 3900, REQUEST_ID)
                self.assertEqual(caught.exception.category, "uncertain")
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertEqual(len(transport.calls), 2)
        transport = Transport(response(200, AUTH), {"status": 422, "debug_id": SECRET,
                              "body": json.dumps({"name": SECRET, "message": TOKEN})})
        with self.assertRaises(PayPalError) as caught:
            PayPalClient(CLIENT, SECRET, transport=transport).get_capture("CAPTURE123")
        self.assertNotIn(SECRET, json.dumps(caught.exception.as_dict()))
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_private_file_requires_sandbox_owner_only_and_never_reads_real_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.json"
            path.write_text(json.dumps({"environment": "sandbox", "client_id": CLIENT, "client_secret": SECRET}))
            path.chmod(0o600)
            client = PayPalClient.from_file(path, transport=Transport())
            self.assertFalse(client.is_live)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                PayPalClient.from_file(path)
            path.chmod(0o600)
            path.write_text(json.dumps({"environment": "production", "client_id": CLIENT, "client_secret": SECRET}))
            with self.assertRaises(ValueError):
                PayPalClient.from_file(path)
            linked = Path(temporary) / "linked.json"
            linked.symlink_to(path)
            with self.assertRaises(OSError):
                PayPalClient.from_file(linked)

    def test_worker_uses_fixed_host_blocks_proxies_redirects_and_exposes_no_headers(self):
        packet = {"method": "GET", "path": "/v2/payments/captures/CAPTURE123",
                  "headers": {"Authorization": "Bearer synthetic"}, "body": None, "timeout_s": 2}
        result = Mock()
        result.code = 200
        result.headers = {"PayPal-Debug-Id": "abc123", "Authorization": "must-not-be-returned"}
        result.read.return_value = json.dumps(REFUND).encode()
        result.__enter__ = Mock(return_value=result)
        result.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = result
        output = io.BytesIO()
        stdin = Mock(buffer=io.BytesIO(json.dumps(packet).encode()))
        stdout = Mock(buffer=output)
        with patch("paypal.sys.stdin", stdin), patch("paypal.sys.stdout", stdout), \
                patch("paypal.urllib.request.build_opener", return_value=opener) as builder:
            self.assertEqual(paypal._http_worker(), 0)
        sent = opener.open.call_args.args[0]
        self.assertEqual(sent.full_url, paypal.BASE_URL + packet["path"])
        self.assertEqual(builder.call_args.args[0].proxies, {})
        self.assertIsInstance(builder.call_args.args[1], paypal._NoRedirect)
        with self.assertRaises(paypal._RedirectBlocked):
            builder.call_args.args[1].redirect_request(None, None, 302, None, {}, "https://api.paypal.com/")
        self.assertNotIn("must-not-be-returned", output.getvalue().decode())
        self.assertEqual(json.loads(output.getvalue())["status"], 200)

    def test_overall_deadline_kills_and_reaps_worker_from_thread_pool(self):
        workers = []

        def spawn(argv, **options):
            self.assertNotIn(SECRET, repr(argv))
            self.assertEqual(set(options["env"]), {"PATH", "LANG"})
            self.assertEqual(options["stderr"], subprocess.DEVNULL)
            process = REAL_POPEN([sys.executable, "-I", "-c", "import sys,time; sys.stdin.buffer.read(); time.sleep(20)"], **options)
            workers.append(process)
            return process
        client = PayPalClient(CLIENT, SECRET, timeout_s=1)
        started = time.monotonic()
        with patch("paypal.subprocess.Popen", side_effect=spawn), ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(PayPalError) as caught:
                pool.submit(client.oauth).result(timeout=4)
        self.assertEqual(caught.exception.category, "uncertain")
        self.assertEqual(caught.exception.error_name, "HTTP_DEADLINE_EXCEEDED")
        self.assertLess(time.monotonic() - started, 3.5)
        self.assertEqual(workers[0].returncode, -signal.SIGKILL)
        self.assertTrue(workers[0].stdin.closed)
        self.assertTrue(workers[0].stdout.closed)
        with self.assertRaises(ChildProcessError):
            os.waitpid(workers[0].pid, os.WNOHANG)


if __name__ == "__main__":
    unittest.main()
