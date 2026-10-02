"""Offline HTTP boundary tests. No PayPal or model requests are made."""

from contextlib import redirect_stderr, redirect_stdout
import http.client
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import FrontendBuildError, MAX_ASSET_BYTES, ReviewServer, main, save_session
from service import ServiceError


class FakeService:
    def __init__(self):
        self.calls = []
        self.policy = {"version": "fixture-policy-v1", "clauses": {"P1": "Read-only test policy."}}
        self.failure = None
        self.block_review = False
        self.review_started = threading.Event()
        self.review_release = threading.Event()

    def list_cases(self):
        if self.failure:
            raise self.failure
        return []

    def get_case(self, case_id):
        return {"id": case_id, "transaction_provenance": "injected_test_transport"}

    def create_case(self, **facts):
        self.calls.append(("create", facts))
        return {"id": "offline-case", **facts, "transaction_provenance": "injected_test_transport"}

    def review_case(self, case_id):
        self.calls.append(("review", case_id))
        if self.block_review:
            self.review_started.set()
            self.review_release.wait(3)
        return self.get_case(case_id)

    def approve(self, case_id, review_hash):
        self.calls.append(("approve", case_id, review_hash))
        return {"id": case_id, "operation": {"state": "pending"}}

    def refresh(self, case_id):
        self.calls.append(("refresh", case_id))
        return self.get_case(case_id)


class AppBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.server = ReviewServer(("127.0.0.1", 0), self.service, approval_mode="test_operator")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.session = self.csrf = None

    def tearDown(self):
        self.service.review_release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        supplied = {"Origin": self.server.origin}
        if self.session:
            supplied["X-Refund-Desk-Session"] = self.session
        if self.csrf:
            supplied["X-Refund-Desk-CSRF"] = self.csrf
        if isinstance(body, dict):
            body = json.dumps(body)
        if body is not None:
            supplied["Content-Type"] = "application/json"
        supplied.update(headers or {})
        connection.request(method, path, body=body, headers=supplied)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def login(self):
        status, headers, body = self.request("POST", "/api/session", {},
                                             {"X-Refund-Desk-Bootstrap": self.server.bootstrap_token})
        self.assertEqual(status, 200)
        self.assertNotIn("Set-Cookie", headers)
        data = json.loads(body)
        self.session, self.csrf = data["session_token"], data["csrf_token"]
        self.assertEqual(data["approval_mode"], "test_operator")

    def test_records_require_origin_scoped_header_session(self):
        self.assertEqual(self.request("GET", "/api/cases")[0], 401)
        self.assertEqual(self.request("GET", "/api/cases/offline-case")[0], 401)
        self.login()
        self.assertEqual(self.request("GET", "/api/cases")[0], 200)
        self.assertEqual(self.request("GET", "/api/cases", headers={
            "X-Refund-Desk-Session": "invalid", "Cookie": "refund_desk_session=" + self.session})[0], 401)

    def test_policy_is_available_with_no_cases_only_after_authentication(self):
        self.assertEqual(self.request("GET", "/api/policy")[0], 401)
        self.login()
        self.assertEqual(json.loads(self.request("GET", "/api/cases")[2]), {"cases": []})
        status, _, body = self.request("GET", "/api/policy")
        self.assertEqual(status, 200)
        returned = json.loads(body)["policy"]
        self.assertEqual(returned, self.service.policy)
        returned["clauses"]["P1"] = "Changed client copy"
        self.assertEqual(self.service.policy["clauses"]["P1"], "Read-only test policy.")
        self.assertEqual(self.request("POST", "/api/policy", {"policy": returned})[0], 404)
        self.assertEqual(self.service.calls, [])

    def test_bootstrap_and_mutations_reject_cross_site_and_wrong_csrf(self):
        self.assertEqual(self.request("POST", "/api/session", {}, {
            "Origin": "https://outside.invalid", "X-Refund-Desk-Bootstrap": self.server.bootstrap_token})[0], 403)
        self.assertEqual(self.request("POST", "/api/session", {}, {"X-Refund-Desk-Bootstrap": "wrong"})[0], 401)
        self.login()
        for headers in ({"Origin": "https://outside.invalid"}, {"Origin": "null"},
                        {"X-Refund-Desk-CSRF": "wrong"}, {"X-Refund-Desk-CSRF": "é"},
                        {"Sec-Fetch-Site": "cross-site"}, {"Host": "attacker.invalid"}):
            with self.subTest(headers=list(headers)):
                self.assertEqual(self.request("POST", "/api/cases/offline-case/review", {}, headers)[0], 403)
        self.assertEqual(self.service.calls, [])

    def test_body_validation_rejects_malformed_oversize_or_duplicate_input(self):
        self.login()
        for body, headers in (("{}", {"Content-Type": "text/plain"}), ("x" * 17000, {}),
                              ('{"x":1,"x":2}', {}), ('{"x":NaN}', {}), ("[]", {}),
                              ('{"x":', {}), ("{}", {"Content-Encoding": "gzip"})):
            with self.subTest(size=len(body), headers=list(headers)):
                self.assertEqual(self.request("POST", "/api/cases/offline-case/review", body, headers)[0], 400)
        self.assertEqual(self.service.calls, [])

    def test_browser_cannot_supply_money_or_transaction_provenance(self):
        self.login()
        facts = {"capture_id": "SYNTHETIC001", "customer_message": "Offline transport test only",
                 "item_used": False, "request_date": "2026-10-02"}
        for field, value in (("amount_minor", 1), ("currency", "USD"), ("transaction_provenance", "paypal_sandbox")):
            with self.subTest(field=field):
                self.assertEqual(self.request("POST", "/api/cases", dict(facts, **{field: value}))[0], 400)
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.request("POST", "/api/cases", facts)[0], 201)
        self.assertEqual(self.service.calls, [("create", facts)])

    def test_approval_requires_true_confirmation_and_exact_review_hash_only(self):
        self.login()
        valid = {"review_hash": "a" * 64, "confirmed": True}
        invalid = [{"review_hash": "a" * 64}, dict(valid, confirmed=False), dict(valid, confirmed=1),
                   dict(valid, confirmed="true"), dict(valid, review_hash="old"),
                   dict(valid, amount_minor=1), dict(valid, approval_kind="human_ui"),
                   dict(valid, approved_by="browser-supplied")]
        for payload in invalid:
            with self.subTest(fields=list(payload)):
                self.assertEqual(self.request("POST", "/api/cases/offline-case/approve", payload)[0], 400)
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.request("POST", "/api/cases/offline-case/approve", valid)[0], 200)
        self.assertEqual(self.service.calls, [("approve", "offline-case", "a" * 64)])

    def test_readback_route_cannot_submit_or_change_approval(self):
        self.login()
        self.assertEqual(self.request("POST", "/api/cases/offline-case/refresh", {"retry": True})[0], 400)
        self.assertEqual(self.request("POST", "/api/cases/offline-case/refresh", {})[0], 200)
        self.assertEqual(self.service.calls, [("refresh", "offline-case")])

    def test_one_active_mutation_keeps_reads_available(self):
        self.login()
        self.service.block_review = True
        responses = []
        first = threading.Thread(target=lambda: responses.append(self.request("POST", "/api/cases/offline-case/review", {})))
        first.start()
        try:
            self.assertTrue(self.service.review_started.wait(2))
            self.assertEqual(self.request("POST", "/api/cases/offline-case/refresh", {})[0], 409)
            self.assertEqual(self.request("GET", "/api/cases/offline-case")[0], 200)
        finally:
            self.service.review_release.set()
            first.join(2)
        self.assertFalse(first.is_alive())
        self.assertEqual(responses[0][0], 200)
        self.assertEqual(self.service.calls, [("review", "offline-case")])

    def test_public_service_errors_but_no_private_exception_details(self):
        self.login()
        self.service.failure = ServiceError("busy", "A saved operation is still running.")
        status, _, body = self.request("GET", "/api/cases")
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"]["message"], "A saved operation is still running.")
        self.service.failure = RuntimeError("PRIVATE_CREDENTIAL_SENTINEL")
        status, _, body = self.request("GET", "/api/cases")
        self.assertEqual(status, 500)
        self.assertNotIn(b"PRIVATE_CREDENTIAL_SENTINEL", body)
        self.assertNotIn(b"Traceback", body)

    def test_static_assets_cannot_escape_or_allow_external_execution(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"PayPal Sandbox", body)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        self.assertEqual(self.request("GET", "/../paypal.py")[0], 404)
        self.assertEqual(self.request("GET", "/", headers={"Host": "outside.invalid"})[0], 403)
        with self.assertRaises(ValueError):
            ReviewServer(("0.0.0.0", 0), self.service)

    def test_checkout_return_accepts_navigation_cleans_query_and_is_stateless(self):
        # This page must work with no session and no callable service at all.
        self.server.service = object()
        navigation = {"Origin": "https://www.sandbox.paypal.com", "Sec-Fetch-Site": "cross-site",
                      "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        status, headers, body = self.request("GET", "/checkout-return?token=CALLBACK_SENTINEL&PayerID=PRIVATE_SENTINEL", headers=navigation)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/checkout-return")
        self.assertNotIn(b"SENTINEL", body)
        status, headers, body = self.request("GET", "/checkout-return", headers=navigation)
        self.assertEqual(status, 200)
        self.assertIn(b"Sandbox checkout returned.", body)
        self.assertIn(b"Run the capture command in your terminal", body)
        self.assertIn(b'href="/"', body)
        self.assertNotIn(b"<script", body)
        self.assertNotIn(self.server.session_token.encode(), body)
        self.assertNotIn(self.server.csrf_token.encode(), body)
        self.assertNotIn(self.server.bootstrap_token.encode(), body)
        self.assertNotIn("Set-Cookie", headers)

    def test_checkout_return_exception_does_not_allow_fetch_iframe_post_or_bad_host(self):
        navigation = {"Origin": "https://www.sandbox.paypal.com", "Sec-Fetch-Site": "cross-site",
                      "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        for path in ("/", "/api/cases", "/api/session", "/style.css", "/app.js"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, headers=navigation)[0], 403)
        for headers in (dict(navigation, **{"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}),
                        dict(navigation, **{"Sec-Fetch-Dest": "iframe"}),
                        dict(navigation, Host="attacker.invalid")):
            with self.subTest(headers=list(headers)):
                self.assertEqual(self.request("GET", "/checkout-return?token=ignored", headers=headers)[0], 403)
        self.assertEqual(self.request("POST", "/checkout-return", {}, navigation)[0], 403)
        self.login()
        self.assertEqual(self.request("POST", "/checkout-return", {})[0], 404)
        self.assertEqual(self.service.calls, [])

    def test_session_file_is_private_and_rejects_unsafe_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            save_session(path, {"url": "synthetic-private-launch"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            path.chmod(0o644)
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                save_session(path, {"url": "replacement"})
            self.assertEqual(path.read_bytes(), before)
            target = Path(directory) / "target.json"
            target.write_text("preserve")
            link = Path(directory) / "symlink.json"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                save_session(link, {"url": "replacement"})
            self.assertEqual(target.read_text(), "preserve")


class FrontendAssetTests(unittest.TestCase):
    request = AppBoundaryTests.request
    login = AppBoundaryTests.login
    tearDown = AppBoundaryTests.tearDown

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="refund-desk-frontend-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "dist"
        (self.root / "assets").mkdir(parents=True)
        self.index = b'<html><head><link rel="stylesheet" href="/assets/index-a1b2c3d4.css"></head><body><div id="root"></div><script type="module" src="/assets/index-a1b2c3d4.js"></script></body></html>'
        (self.root / "index.html").write_bytes(self.index)
        (self.root / "assets" / "index-a1b2c3d4.js").write_bytes(b'export const fixture = "compiled frontend fixture";')
        (self.root / "assets" / "index-a1b2c3d4.css").write_bytes(b':root { color: black; }')
        (self.root / "assets" / "font-a1b2c3d4.woff2").write_bytes(b'wOF2fixture')
        (self.root / "assets" / "logo-a1b2c3d4.svg").write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        self.service = FakeService()
        self.server = ReviewServer(("127.0.0.1", 0), self.service, approval_mode="test_operator", frontend_dir=self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.session = self.csrf = None

    def test_built_index_and_hashed_assets_have_fixed_mime_and_strict_script_policy(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.index)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        directives = [value.strip() for value in headers["Content-Security-Policy"].split(";")]
        self.assertIn("script-src 'self'", directives)
        self.assertEqual(next(value for value in directives if value.startswith("script-src")), "script-src 'self'")
        self.assertIn("style-src 'self' 'unsafe-inline'", directives)
        self.assertIn("font-src 'self'", directives)
        self.assertNotIn("unsafe-eval", headers["Content-Security-Policy"])
        self.assertNotIn(self.server.session_token.encode(), body)
        self.assertNotIn(self.server.bootstrap_token.encode(), body)
        for name, mime in (("index-a1b2c3d4.js", "text/javascript; charset=utf-8"),
                           ("index-a1b2c3d4.css", "text/css; charset=utf-8"),
                           ("font-a1b2c3d4.woff2", "font/woff2"),
                           ("logo-a1b2c3d4.svg", "image/svg+xml")):
            with self.subTest(name=name):
                status, headers, body = self.request("GET", "/assets/" + name + "?cache=fixture")
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], mime)
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertTrue(body)
        self.assertEqual(self.request("GET", "/app.js")[0], 404)

    def test_private_files_traversal_and_symlinks_are_not_public_assets(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "private.js").write_text("PRIVATE_ASSET_SENTINEL")
        (self.root / "browser-session.json").write_text("PRIVATE_ASSET_SENTINEL")
        (self.root / "assets" / "private.json").write_text("PRIVATE_ASSET_SENTINEL")
        (self.root / "assets" / "index-a1b2c3d4.js.map").write_text("PRIVATE_ASSET_SENTINEL")
        (self.root / "assets" / "escape.js").symlink_to(outside / "private.js")
        (self.root / "assets" / "linked").symlink_to(outside, target_is_directory=True)
        for path in ("/browser-session.json", "/assets/private.json", "/assets/index-a1b2c3d4.js.map",
                     "/assets/../browser-session.json", "/assets/../../outside/private.js",
                     "/assets/%2e%2e/%2e%2e/outside/private.js", "/assets/%2Fprivate.js",
                     "/assets/escape.js", "/assets/linked/private.js", "/assets//index-a1b2c3d4.js"):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"PRIVATE_ASSET_SENTINEL", body)

    def test_nonregular_and_oversized_assets_are_rejected_without_blocking(self):
        os.mkfifo(self.root / "assets" / "pipe.js")
        (self.root / "assets" / "large.js").write_bytes(b"x" * (MAX_ASSET_BYTES + 1))
        for name in ("pipe.js", "large.js"):
            self.assertEqual(self.request("GET", "/assets/" + name)[0], 404)

    def test_explicit_build_selection_fails_clearly_without_safe_compiled_entry(self):
        unbuilt = Path(self.temporary.name) / "unbuilt"
        unbuilt.mkdir()
        (unbuilt / "index.html").write_text('<script type="module" src="/src/main.tsx"></script>')
        linked = Path(self.temporary.name) / "linked-build"
        linked.symlink_to(self.root, target_is_directory=True)
        for directory in (unbuilt, linked, Path(self.temporary.name) / "missing"):
            with self.subTest(directory=directory), self.assertRaisesRegex(FrontendBuildError, "npm run build"):
                ReviewServer(("127.0.0.1", 0), self.service, frontend_dir=directory)
        (unbuilt / "index.html").unlink()
        (unbuilt / "index.html").symlink_to(self.root / "index.html")
        with self.assertRaises(FrontendBuildError):
            ReviewServer(("127.0.0.1", 0), self.service, frontend_dir=unbuilt)

    def test_cli_defaults_to_compiled_build_and_missing_build_fails_before_credentials(self):
        arguments = ["--client-file", "unused-test-credentials.json", "--merchant-id", "TESTMERCHANT",
                     "--merchant-email", "merchant@example.test", "--port", "0",
                     "--data-dir", str(Path(self.temporary.name) / "cli-data")]
        with patch("app.DEFAULT_FRONTEND_DIR", self.root), \
                patch("paypal.PayPalClient.from_file", return_value=object()), \
                patch("service.RefundService", return_value=self.service), \
                patch("app.ReviewServer", wraps=ReviewServer) as server, \
                patch.object(ReviewServer, "serve_forever", return_value=None), \
                redirect_stdout(io.StringIO()):
            main(arguments)
        self.assertEqual(server.call_args.kwargs["frontend_dir"], self.root)
        error_output = io.StringIO()
        with patch("app.DEFAULT_FRONTEND_DIR", Path(self.temporary.name) / "missing-default"), \
                patch("paypal.PayPalClient.from_file") as credentials, \
                redirect_stderr(error_output), self.assertRaises(SystemExit) as caught:
            main(arguments)
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("npm run build", error_output.getvalue())
        credentials.assert_not_called()

    def test_compiled_frontend_keeps_api_auth_csrf_and_stateless_callback(self):
        self.assertEqual(self.request("GET", "/api/cases")[0], 401)
        self.assertEqual(self.request("GET", "/assets/index-a1b2c3d4.js", headers={"Host": "outside.invalid"})[0], 403)
        self.login()
        self.assertEqual(self.request("GET", "/api/cases")[0], 200)
        self.assertEqual(self.request("POST", "/api/cases/offline-case/review", {},
                                      {"X-Refund-Desk-CSRF": "wrong"})[0], 403)
        self.server.service = object()
        navigation = {"Origin": "https://www.sandbox.paypal.com", "Sec-Fetch-Site": "cross-site",
                      "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        status, _, body = self.request("GET", "/checkout-return", headers=navigation)
        self.assertEqual(status, 200)
        self.assertIn(b"Run the capture command in your terminal", body)
        self.assertNotIn(self.server.session_token.encode(), body)
        self.assertEqual(self.request("GET", "/style.css")[0], 200)
        self.server.service = self.service
        self.assertEqual(self.service.calls, [])


if __name__ == "__main__":
    unittest.main()
