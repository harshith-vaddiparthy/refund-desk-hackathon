#!/usr/bin/env python3
"""Local, authenticated merchant interface for sandbox-only Refund Desk."""

import argparse
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading


STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_FRONTEND_DIR = Path(__file__).resolve().parent / "frontend" / "dist"
MAX_BODY = 16 * 1024
CASE_PATH = re.compile(r"/api/cases/([A-Za-z0-9_-]{1,80})(?:/(review|approve|refresh|resolve))?\Z")
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
# Generated Sidebar/Sonner attributes and Radix modal style elements require
# inline CSS. This exception applies only to the selected compiled frontend.
FRONTEND_CSP = CSP.replace("style-src 'self'", "style-src 'self' 'unsafe-inline'") + "; font-src 'self'"
MAX_ASSET_BYTES = 8 * 1024 * 1024
ASSET_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
               ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
               ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".svg": "image/svg+xml", ".gif": "image/gif", ".ico": "image/x-icon"}


class FrontendBuildError(ValueError):
    pass


class FrontendBuild:
    """Serve only a selected compiled index and allowed assets beneath its root."""
    def __init__(self, directory):
        self.root_fd = None
        try:
            self.root_fd = os.open(Path(directory).expanduser(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.index = self._read("index.html")
            scripts = re.findall(rb'<script\b[^>]*\bsrc=["\'](/assets/[^"\']+\.js)["\']', self.index or b"")
            if not self.index or not scripts or any(self.asset(path.decode("ascii")) is None for path in scripts):
                raise ValueError
        except (OSError, ValueError, UnicodeError):
            self.close()
            raise FrontendBuildError("Compiled frontend is missing or unsafe. Run npm ci and npm run build in frontend, then select frontend/dist.") from None

    def close(self):
        if self.root_fd is not None:
            os.close(self.root_fd)
            self.root_fd = None

    def _read(self, relative):
        parts = relative.split("/")
        if any(part in ("", ".", "..") or not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in parts):
            return None
        directory = None
        try:
            directory = os.dup(self.root_fd)
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ASSET_BYTES:
                    return None
                content = source.read(MAX_ASSET_BYTES + 1)
            return content if len(content) <= MAX_ASSET_BYTES else None
        except (OSError, TypeError):
            return None
        finally:
            if directory is not None:
                os.close(directory)

    def asset(self, path):
        if not path.startswith("/assets/"):
            return None
        content_type = ASSET_TYPES.get(Path(path).suffix.lower())
        if content_type is None:
            return None
        content = self._read(path[1:])
        return (content, content_type) if content is not None else None


def token_matches(supplied, expected):
    return isinstance(supplied, str) and secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def finite(_):
        raise ValueError("Non-finite number")
    return json.loads(raw, object_pairs_hook=unique, parse_constant=finite)


class ReviewServer(ThreadingHTTPServer):
    """Inject a RefundService for offline HTTP tests; production uses main()."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service, *, approval_mode="human_ui", frontend_dir=None):
        if address[0] != "127.0.0.1":
            raise ValueError("Refund Desk must bind to 127.0.0.1")
        if approval_mode not in ("human_ui", "test_operator"):
            raise ValueError("Unsupported approval mode")
        self.service = service
        self.approval_mode = approval_mode
        self.frontend = FrontendBuild(frontend_dir) if frontend_dir is not None else None
        self.csp = FRONTEND_CSP if self.frontend is not None else CSP
        self.bootstrap_token = secrets.token_urlsafe(32)
        self.session_token = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self.operation_lock = threading.Lock()
        try:
            super().__init__(address, Handler)
        except Exception:
            if self.frontend is not None:
                self.frontend.close()
            raise

    def server_close(self):
        try:
            super().server_close()
        finally:
            if self.frontend is not None:
                self.frontend.close()

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server_port}"

    @property
    def bootstrap_url(self):
        return self.origin + "/#session=" + self.bootstrap_token

    def handle_error(self, request, client_address):
        # Never send exception details, request data, or credentials to logs.
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "RefundDesk"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.reply(code, {"error": {"code": "http_error", "message": "Request not supported."}})

    def reply(self, status, body, content_type="application/json; charset=utf-8", *, location=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", self.server.csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if location is not None:
            self.send_header("Location", location)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def problem(self, status, code, message):
        self.reply(status, {"error": {"code": code, "message": message}})

    def local_request(self, *, mutation=False, checkout_return=False):
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in allowed:
            self.problem(403, "invalid_host", "Open the local Refund Desk address.")
            return False
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1:
            self.problem(403, "invalid_origin", "This action must come from the local Refund Desk page.")
            return False
        # The sole cross-site exception is a stateless top-level return page.
        # It neither reads a session nor calls the refund service.
        if (checkout_return and self.command == "GET"
                and self.headers.get("Sec-Fetch-Mode") == "navigate"
                and self.headers.get("Sec-Fetch-Dest") == "document"):
            return True
        if origins and origins[0] != "http://" + hosts[0]:
            self.problem(403, "invalid_origin", "This action must come from the local Refund Desk page.")
            return False
        if mutation and len(origins) != 1:
            self.problem(403, "missing_origin", "This action must come from the local Refund Desk page.")
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.problem(403, "cross_site_request", "Cross-site requests are not accepted.")
            return False
        return True

    def authenticated(self):
        tokens = self.headers.get_all("X-Refund-Desk-Session", [])
        valid = len(tokens) == 1 and token_matches(tokens[0], self.server.session_token)
        if not valid:
            self.problem(401, "session_required", "Open the private launch link from your local session file.")
        return valid

    def session(self):
        runtime = getattr(self.server.service, "ai_runtime", None)
        descriptor = ({key: runtime[key] for key in ("provider", "model", "location")}
                      if isinstance(runtime, dict) and all(isinstance(runtime.get(key), str)
                                                         for key in ("provider", "model", "location")) else None)
        return {"session_token": self.server.session_token, "csrf_token": self.server.csrf_token, "environment": "sandbox",
                "approval_mode": self.server.approval_mode, "ai_runtime": descriptor}

    def body(self):
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,7}", lengths[0])):
            raise ValueError("A single Content-Length is required.")
        size = int(lengths[0])
        if size > MAX_BODY:
            raise ValueError("Request exceeds the 16 KiB limit.")
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("Use an application/json request.")
        if self.headers.get("Content-Encoding", "identity") != "identity":
            raise ValueError("Encoded request bodies are not accepted.")
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise ValueError("Incomplete request body.")
        result = strict_json(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("Request body must be an object.")
        return result

    def guarded(self, operation):
        try:
            operation()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            # ServiceError promises a public message. Other exceptions remain private.
            from service import ServiceError
            if isinstance(exc, ServiceError):
                code = exc.code if re.fullmatch(r"[A-Za-z0-9_]{1,80}", str(exc.code)) else "service_error"
                status = 404 if code == "not_found" else 409
                self.problem(status, code, str(exc))
            else:
                self.problem(500, "operation_failed", "The operation did not finish normally. Check the saved case before taking another action.")

    def do_GET(self):
        checkout_return = self.path.split("?", 1)[0] == "/checkout-return"
        if not self.local_request(checkout_return=checkout_return):
            return
        if checkout_return:
            if "?" in self.path:
                self.reply(303, b"", "text/html; charset=utf-8", location="/checkout-return")
            else:
                self.reply(200, (STATIC / "checkout-return.html").read_bytes(), "text/html; charset=utf-8")
            return
        if self.server.frontend is not None:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self.reply(200, self.server.frontend.index, "text/html; charset=utf-8")
                return
            asset = self.server.frontend.asset(path)
            if asset is not None:
                self.reply(200, *asset)
                return
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8")}
        if self.server.frontend is not None:
            # The public checkout-return page still uses this fixed stylesheet.
            assets = {"/style.css": assets["/style.css"]}
        if self.path in assets:
            name, content_type = assets[self.path]
            self.reply(200, (STATIC / name).read_bytes(), content_type)
            return
        if not self.path.startswith("/api/"):
            self.problem(404, "not_found", "Page not found.")
            return
        if not self.authenticated():
            return
        if self.path == "/api/session":
            self.reply(200, self.session())
        elif self.path == "/api/policy":
            self.guarded(lambda: self.reply(200, {"policy": deepcopy(self.server.service.policy)}))
        elif self.path == "/api/cases":
            self.guarded(lambda: self.reply(200, {"cases": self.server.service.list_cases()}))
        else:
            match = CASE_PATH.fullmatch(self.path)
            if not match or match[2]:
                self.problem(404, "not_found", "Record not found.")
            else:
                self.guarded(lambda: self.reply(200, {"case": self.server.service.get_case(match[1])}))

    def do_POST(self):
        if not self.local_request(mutation=True):
            return
        if self.path == "/api/session":
            supplied = self.headers.get("X-Refund-Desk-Bootstrap", "")
            if not token_matches(supplied, self.server.bootstrap_token):
                self.problem(401, "session_required", "The private launch link is missing or expired.")
                return
            try:
                if self.body():
                    raise ValueError("Session request must be empty.")
            except (ValueError, UnicodeError, TimeoutError):
                self.problem(400, "invalid_request", "Session request must be an empty JSON object.")
                return
            self.reply(200, self.session())
            return
        if not self.authenticated():
            return
        if not token_matches(self.headers.get("X-Refund-Desk-CSRF", ""), self.server.csrf_token):
            self.problem(403, "invalid_csrf", "Reload this local page before trying the action.")
            return
        try:
            payload = self.body()
        except (ValueError, UnicodeError, TimeoutError):
            self.problem(400, "invalid_request", "Send a valid JSON object of at most 16 KiB.")
            return
        match = CASE_PATH.fullmatch(self.path)
        if self.path != "/api/cases" and (not match or not match[2]):
            self.problem(404, "not_found", "Action not found.")
            return
        if not self.server.operation_lock.acquire(blocking=False):
            self.problem(409, "busy", "Another operation is still running. Wait for it to finish.")
            return
        def complete():
            try:
                response = self.perform(payload, match)
            finally:
                self.server.operation_lock.release()
            # A received response means the action and its lock cleanup have
            # completed; a fast next request must not race this handler's tail.
            self.reply(*response)
        self.guarded(complete)

    def perform(self, payload, match):
        def invalid(code, message):
            return 400, {"error": {"code": code, "message": message}}

        service = self.server.service
        if self.path == "/api/cases":
            if set(payload) != {"capture_id", "customer_message", "item_used", "request_date"}:
                return invalid("invalid_request", "Supply only capture ID, customer message, item-use status, and request date.")
            if (not isinstance(payload["capture_id"], str) or not re.fullmatch(r"[A-Za-z0-9]{1,64}", payload["capture_id"])
                    or not isinstance(payload["customer_message"], str) or not 1 <= len(payload["customer_message"]) <= 4000
                    or payload["item_used"] is not None and type(payload["item_used"]) is not bool
                    or payload["request_date"] is not None and (not isinstance(payload["request_date"], str) or len(payload["request_date"]) != 10)):
                return invalid("invalid_request", "Check the capture ID, message, item-use status, and request date.")
            return 201, {"case": service.create_case(**payload)}
        case_id, action = match.groups()
        if action == "resolve":
            allowed = {"expected_version", "customer_message", "item_used", "request_date", "resolution_note"}
            if (not set(payload) <= allowed or "expected_version" not in payload
                    or not isinstance(payload["expected_version"], str) or not re.fullmatch(r"[0-9a-f]{64}", payload["expected_version"])
                    or "customer_message" in payload and (not isinstance(payload["customer_message"], str) or not 1 <= len(payload["customer_message"]) <= 4000)
                    or "item_used" in payload and payload["item_used"] is not None and type(payload["item_used"]) is not bool
                    or "request_date" in payload and payload["request_date"] is not None and (not isinstance(payload["request_date"], str) or len(payload["request_date"]) != 10)
                    or "resolution_note" in payload and payload["resolution_note"] is not None and (not isinstance(payload["resolution_note"], str) or len(payload["resolution_note"]) > 2000)):
                return invalid("invalid_resolution", "Supply the current version and only conversation, nullable merchant facts, and resolution explanation.")
            result = service.resolve_case(case_id, **payload)
        elif action == "approve":
            if (set(payload) != {"review_hash", "confirmed"} or payload["confirmed"] is not True
                    or not isinstance(payload["review_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", payload["review_hash"])):
                return invalid("approval_required", "Explicit approval of the current review is required.")
            result = service.approve(case_id, payload["review_hash"])
        elif payload:
            return invalid("invalid_request", "This action takes no browser-supplied facts.")
        elif action == "review":
            result = service.review_case(case_id)
        else:
            result = service.refresh(case_id)
        return 200, {"case": result}


def save_session(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Session file must be an owner-only regular file")
        stream.truncate(0)
        json.dump(value, stream)
        stream.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-file", type=Path, required=True, help="Owner-only PayPal sandbox credential JSON")
    parser.add_argument("--merchant-id", required=True)
    parser.add_argument("--merchant-email", required=True)
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".local/share/refund-desk")
    parser.add_argument("--port", type=int, default=8749)
    parser.add_argument("--test-operator", help="Trusted operator label for automated QA; never human approval")
    parser.add_argument("--ai-provider", choices=("local", "groq"), default="local",
                        help="Explicit AI route; local is the default and no cloud fallback is used")
    parser.add_argument("--groq-client-file", type=Path,
                        help="Owner-only Groq credential JSON; required only when --ai-provider groq is selected")
    parser.add_argument("--frontend-dir", type=Path, default=DEFAULT_FRONTEND_DIR,
                        help="Compiled Vite build directory (default: repository frontend/dist); missing/unsafe builds are rejected")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("Port must be between 0 and 65535.")
    if args.ai_provider == "groq" and args.groq_client_file is None:
        parser.error("--ai-provider groq requires --groq-client-file.")
    if args.ai_provider != "groq" and args.groq_client_file is not None:
        parser.error("--groq-client-file requires explicit --ai-provider groq.")
    try:
        # Fail with build instructions before reading credentials or creating
        # application state. The server later pins its own validated root handle.
        FrontendBuild(args.frontend_dir).close()
        from model_config import select_runtime
        try:
            runtime = select_runtime(args.ai_provider, args.groq_client_file)
        except (ValueError, OSError):
            parser.exit(1, "AI runtime configuration is invalid. Check the selected provider and its owner-only credential file.\n")
        from paypal import PayPalClient
        from service import RefundService
        options = ({"approval_kind": "test_operator", "approved_by": args.test_operator} if args.test_operator else {})
        service = RefundService(args.data_dir, PayPalClient.from_file(args.client_file),
                                args.merchant_id, args.merchant_email, runtime=runtime, **options)
        server = ReviewServer(("127.0.0.1", args.port), service,
                              approval_mode="test_operator" if args.test_operator else "human_ui",
                              frontend_dir=args.frontend_dir)
        session_file = args.data_dir.resolve() / "browser-session.json"
        save_session(session_file, {"url": server.bootstrap_url, "environment": "sandbox",
                                    "approval_mode": server.approval_mode})
    except FrontendBuildError as exc:
        parser.exit(1, str(exc) + "\n")
    except Exception:
        parser.exit(1, "Refund Desk could not start. Check the private sandbox configuration, data-directory permissions, and port.\n")
    print(f"Refund Desk sandbox: {server.origin}", flush=True)
    print(f"Private launch link saved to {session_file}. Keep this file private.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
