"""Small sandbox-only PayPal client. Every payment POST is one explicit attempt."""

import base64
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from uuid import UUID


BASE_URL = "https://api-m.sandbox.paypal.com"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
CLEANUP_SECONDS = 2
_ID = r"[A-Za-z0-9]{1,64}"
_ROUTES = {
    "GET": re.compile(rf"/v2/(?:checkout/orders/{_ID}|payments/(?:captures|refunds)/{_ID})\Z"),
    "POST": re.compile(rf"/(?:v1/oauth2/token|v2/checkout/orders(?:/{_ID}/capture)?|v2/payments/captures/{_ID}/refund)\Z"),
}


def parse_usd_cents(value):
    """Parse exact nonnegative USD decimal text; never round a float or fraction."""
    if not isinstance(value, str) or len(value) > 32:
        raise ValueError("USD amount must be decimal text of at most 32 characters")
    match = re.fullmatch(r"([0-9]+)(?:\.([0-9]{1,2}))?", value)
    if not match:
        raise ValueError("USD amount must be nonnegative decimal text with at most two fractional digits")
    return int(match[1]) * 100 + int((match[2] or "").ljust(2, "0"))


def _money(amount_minor):
    if type(amount_minor) is not int or amount_minor <= 0:
        raise ValueError("Amount must be a positive integer number of USD cents")
    value = f"{amount_minor // 100}.{amount_minor % 100:02d}"
    parse_usd_cents(value)
    return {"currency_code": "USD", "value": value}


def _resource_id(value):
    if not isinstance(value, str) or not re.fullmatch(_ID, value):
        raise ValueError("Invalid PayPal resource ID")
    return value


def _request_id(value):
    try:
        parsed = UUID(value) if isinstance(value, str) else None
        valid = parsed is not None and parsed.int != 0 and str(parsed) == value
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Payment POST requires a nonzero canonical UUID supplied by the caller")
    return value


def _strict_json(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("Duplicate JSON keys")
            result[key] = item
        return result

    def finite(_):
        raise ValueError("Non-finite JSON numbers")
    return json.loads(value, object_pairs_hook=unique, parse_constant=finite)


def _safe_identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else None


class PayPalError(RuntimeError):
    """A safe receipt. 'rejected' describes this request, not an earlier attempt."""
    def __init__(self, category, status=None, debug_id=None, error_name=None):
        self.category = category
        self.status = status
        self.debug_id = _safe_identifier(debug_id)
        self.error_name = _safe_identifier(error_name)
        message = f"PayPal sandbox request {category}"
        if status is not None:
            message += f" (HTTP {status})"
        if self.error_name:
            message += f": {self.error_name}"
        super().__init__(message)

    def as_dict(self):
        return {"category": self.category, "status": self.status,
                "debug_id": self.debug_id, "error_name": self.error_name}


class _RedirectBlocked(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise _RedirectBlocked()


def _valid_route(method, path):
    return method in _ROUTES and isinstance(path, str) and _ROUTES[method].fullmatch(path)


def _http_worker():
    """Private pipe protocol; no credentials, response headers or tracebacks are logged."""
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError
        payload = _strict_json(raw.decode("utf-8"))
        method, path = payload["method"], payload["path"]
        if not _valid_route(method, path):
            raise ValueError
        headers = payload["headers"]
        allowed = {"Authorization", "Accept", "Content-Type", "PayPal-Request-Id", "Prefer"}
        if not isinstance(headers, dict) or not set(headers).issubset(allowed):
            raise ValueError
        if any(not isinstance(value, str) or "\r" in value or "\n" in value for value in headers.values()):
            raise ValueError
        if method == "POST" and path != "/v1/oauth2/token":
            _request_id(headers.get("PayPal-Request-Id"))
        body = payload["body"]
        if body is not None and not isinstance(body, str):
            raise ValueError
        timeout = payload["timeout_s"]
        if type(timeout) not in (float, int) or not 1 <= timeout <= 120:
            raise ValueError
        request = urllib.request.Request(BASE_URL + path, method=method, headers=headers,
                                         data=None if body is None else body.encode("utf-8"))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            result = {"status": response.code, "debug_id": _safe_identifier(response.headers.get("PayPal-Debug-Id"))}
            data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            result["response_error"] = "RESPONSE_TOO_LARGE"
        else:
            try:
                result["body"] = data.decode("utf-8")
            except UnicodeError:
                result["response_error"] = "INVALID_RESPONSE_ENCODING"
    except _RedirectBlocked:
        result = {"transport_error": "REDIRECT_BLOCKED"}
    except Exception:
        result = {"transport_error": "HTTP_WORKER_FAILED"}
    sys.stdout.buffer.write(json.dumps(result).encode("utf-8"))
    return 0


def _stop_worker(process):
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=CLEANUP_SECONDS)
    except subprocess.TimeoutExpired:
        raise PayPalError("uncertain", error_name="WORKER_CLEANUP_UNCONFIRMED") from None


def _bounded_http(request, timeout_s):
    deadline = time.monotonic() + timeout_s
    payload = json.dumps(dict(request, timeout_s=timeout_s), allow_nan=False).encode("utf-8")
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("PayPal request exceeds 64 KiB")
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--http-worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={"PATH": os.defpath, "LANG": "C.UTF-8"}, start_new_session=True, close_fds=True)
    cleanup_attempted = False
    try:
        try:
            output, _ = process.communicate(payload, timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            cleanup_attempted = True
            _stop_worker(process)
            raise PayPalError("uncertain", error_name="HTTP_DEADLINE_EXCEEDED") from None
        if process.returncode or len(output) > 7 * MAX_RESPONSE_BYTES:
            raise PayPalError("uncertain", error_name="INVALID_WORKER_RESPONSE")
        try:
            result = _strict_json(output.decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, UnicodeError, RecursionError):
            raise PayPalError("uncertain", error_name="INVALID_WORKER_RESPONSE") from None
    finally:
        try:
            if process.poll() is None and not cleanup_attempted:
                _stop_worker(process)
        finally:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()


class PayPalClient:
    def __init__(self, client_id, client_secret, timeout_s=30, *, transport=None):
        for value in (client_id, client_secret):
            if not isinstance(value, str) or not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
                raise ValueError("A valid sandbox client ID and secret are required")
        if ":" in client_id:
            raise ValueError("Invalid sandbox client ID")
        if type(timeout_s) not in (int, float) or not 1 <= timeout_s <= 120:
            raise ValueError("HTTP deadline must be between 1 and 120 seconds")
        if transport is not None and not callable(transport):
            raise ValueError("Injected transport must be callable")
        self._basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self._secrets = [client_id, client_secret, self._basic]
        self._token = None
        self._expires_at = 0
        self.timeout_s = timeout_s
        self._transport = _bounded_http if transport is None else transport
        self.is_live = transport is None
        self.timeout_semantics = ("Overall HTTP deadline plus at most 2 seconds worker cleanup" if self.is_live else
                                  "Injected in-process test transport; no enforced HTTP deadline")

    def __repr__(self):
        return f"<PayPalClient sandbox authenticated={self._token is not None}>"

    @classmethod
    def from_file(cls, path, **options):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise ValueError("Credential file must be an owner-only regular file")
            raw = source.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("Credential file exceeds the size limit")
        try:
            config = _strict_json(raw.decode("utf-8"))
            if not isinstance(config, dict) or config.get("environment") != "sandbox":
                raise ValueError
            return cls(config["client_id"], config["client_secret"], **options)
        except (KeyError, ValueError, UnicodeError, RecursionError):
            raise ValueError("Invalid sandbox credential file") from None

    def _redact(self, value):
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {self._redact(key): self._redact(item) for key, item in value.items()}
        return value

    def _call(self, method, path, body=None, request_id=None, *, basic=False):
        if not _valid_route(method, path):
            raise ValueError("Unsupported sandbox API route")
        if basic != (path == "/v1/oauth2/token"):
            raise ValueError("Basic credentials are restricted to sandbox OAuth")
        if method == "POST" and not basic:
            request_id = _request_id(request_id)
        if basic:
            authorization = "Basic " + self._basic
            content_type = "application/x-www-form-urlencoded"
        else:
            if self._token is None or time.monotonic() >= self._expires_at:
                self.oauth()
            authorization = "Bearer " + self._token
            content_type = "application/json"
        headers = {"Authorization": authorization, "Accept": "application/json", "Content-Type": content_type}
        if request_id:
            headers.update({"PayPal-Request-Id": request_id, "Prefer": "return=representation"})
        request = {"method": method, "path": path, "headers": headers, "body": body}
        # Validate size before entering any potentially live transport.
        if len(json.dumps(dict(request, timeout_s=self.timeout_s)).encode()) > MAX_REQUEST_BYTES:
            raise ValueError("PayPal request exceeds 64 KiB")
        try:
            response = self._transport(request, self.timeout_s)
        except PayPalError:
            raise
        except Exception:
            raise PayPalError("uncertain", error_name="TRANSPORT_FAILED") from None
        if not isinstance(response, dict):
            raise PayPalError("uncertain", error_name="INVALID_RESPONSE")
        if response.get("transport_error"):
            raise PayPalError("uncertain", error_name=self._redact(response["transport_error"]))
        status_code = response.get("status")
        if type(status_code) is not int or not 100 <= status_code <= 599:
            raise PayPalError("uncertain", error_name="INVALID_RESPONSE")
        debug_id = self._redact(response.get("debug_id"))
        category = "rejected" if 400 <= status_code < 500 and status_code not in (408, 409) else "uncertain"
        raw = response.get("body", "")
        try:
            if response.get("response_error") or not isinstance(raw, str) or len(raw.encode()) > MAX_RESPONSE_BYTES:
                raise ValueError
            data = _strict_json(raw)
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            raise PayPalError(category, status_code, debug_id, "INVALID_RESPONSE") from None
        if not 200 <= status_code < 300:
            raise PayPalError(category, status_code, debug_id or self._redact(data.get("debug_id")),
                              self._redact(data.get("name") or data.get("error")))
        return {"environment": "sandbox", "provenance": "paypal_sandbox" if self.is_live else "injected_test_transport",
                "method": method, "path": path, "request_id": request_id,
                "status": status_code, "debug_id": _safe_identifier(debug_id),
                "data": data if basic else self._redact(data)}

    def oauth(self):
        response = self._call("POST", "/v1/oauth2/token", "grant_type=client_credentials", basic=True)
        data = response["data"]
        token, expiry = data.get("access_token"), data.get("expires_in")
        if (response["status"] != 200 or not isinstance(token, str) or not token
                or any(ord(char) < 33 or ord(char) > 126 for char in token)
                or data.get("token_type") != "Bearer" or type(expiry) is not int or expiry <= 0):
            raise PayPalError("uncertain", response["status"], response["debug_id"], "INVALID_OAUTH_RESPONSE")
        self._token = token
        self._secrets.append(token)
        self._expires_at = time.monotonic() + expiry - min(30, expiry / 10)
        response["data"] = self._redact({key: data.get(key) for key in ("token_type", "expires_in", "scope", "app_id")})
        return response

    def create_order(self, payload, request_id):
        _request_id(request_id)
        if not isinstance(payload, dict) or payload.get("intent") != "CAPTURE":
            raise ValueError("This proof supports CAPTURE orders only")
        units = payload.get("purchase_units")
        if not isinstance(units, list) or len(units) != 1 or not isinstance(units[0], dict):
            raise ValueError("This proof supports exactly one purchase unit")
        amount = units[0].get("amount")
        if (not isinstance(amount, dict) or amount.get("currency_code") != "USD"
                or parse_usd_cents(amount.get("value")) <= 0):
            raise ValueError("Order must specify a positive exact USD amount")
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return self._call("POST", "/v2/checkout/orders", body, request_id)

    def get_order(self, order_id):
        return self._call("GET", f"/v2/checkout/orders/{_resource_id(order_id)}")

    def capture_order(self, order_id, request_id):
        return self._call("POST", f"/v2/checkout/orders/{_resource_id(order_id)}/capture", "{}", request_id)

    def get_capture(self, capture_id):
        return self._call("GET", f"/v2/payments/captures/{_resource_id(capture_id)}")

    def refund_capture(self, capture_id, amount_minor, request_id):
        body = json.dumps({"amount": _money(amount_minor)}, sort_keys=True, separators=(",", ":"))
        return self._call("POST", f"/v2/payments/captures/{_resource_id(capture_id)}/refund", body, request_id)

    def get_refund(self, refund_id):
        return self._call("GET", f"/v2/payments/refunds/{_resource_id(refund_id)}")


if __name__ == "__main__":
    raise SystemExit(_http_worker() if sys.argv[1:] == ["--http-worker"] else 2)
