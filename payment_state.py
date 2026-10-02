"""Durable sandbox refund state. This module makes no HTTP or payment calls.

Only trusted server code may choose approval_kind/approved_by or supply PayPal
response wrappers. REVIEW_READY and browser-provided fields are not approval.
Unknown submissions are never automatically retried or returned to prepared.
"""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
import uuid

from paypal import parse_usd_cents


SNAPSHOT_FIELDS = {"capture_id", "amount_minor", "currency", "case_version", "policy_version", "review_hash"}


class PaymentStateError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value, field):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256:
        raise PaymentStateError("invalid_input", f"{field} must be non-empty text of at most 256 characters.")
    return value


def _snapshot(value):
    if not isinstance(value, dict) or set(value) != SNAPSHOT_FIELDS:
        raise PaymentStateError("invalid_snapshot", "Approval snapshot must contain only the six defined binding fields.")
    result = dict(value)
    for field in ("capture_id", "case_version", "policy_version", "review_hash"):
        _text(result[field], field)
    if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", result["capture_id"]):
        raise PaymentStateError("invalid_snapshot", "Capture ID has unsupported characters.")
    if type(result["amount_minor"]) is not int or not 0 < result["amount_minor"] <= 2**63 - 1:
        raise PaymentStateError("invalid_snapshot", "Approved amount must be a positive integer number of cents.")
    if result["currency"] != "USD":
        raise PaymentStateError("invalid_snapshot", "Only USD is supported.")
    if not re.fullmatch(r"[0-9a-f]{64}", result["review_hash"]):
        raise PaymentStateError("invalid_snapshot", "Review hash must be a lowercase SHA-256 digest.")
    return result


def _resource(response, method, path, request_id=None):
    if (not isinstance(response, dict) or response.get("environment") != "sandbox"
            or response.get("provenance") not in ("paypal_sandbox", "injected_test_transport")
            or response.get("method") != method or response.get("path") != path
            or type(response.get("status")) is not int or not 200 <= response["status"] < 300
            or (method == "GET" and response["status"] != 200)
            or (request_id is not None and response.get("request_id") != request_id)
            or not isinstance(response.get("data"), dict)):
        raise PaymentStateError("invalid_readback", "Response does not match the expected sandbox request.")
    return response["data"]


def _amount(data):
    amount = data.get("amount")
    if not isinstance(amount, dict) or amount.get("currency_code") != "USD":
        raise PaymentStateError("amount_mismatch", "PayPal resource does not contain a USD amount.")
    try:
        return parse_usd_cents(amount.get("value"))
    except ValueError:
        raise PaymentStateError("amount_mismatch", "PayPal resource amount is not an exact supported decimal.") from None


def _payee(data, merchant_id):
    payee = data.get("payee")
    returned = payee.get("merchant_id") if isinstance(payee, dict) else None
    if returned is not None and returned != merchant_id:
        raise PaymentStateError("merchant_mismatch", "Returned payee does not match the configured merchant.")
    return "matched_returned_payee" if returned is not None else "not_returned"


def _safe_error(error):
    if not isinstance(error, dict) or error.get("category") not in ("rejected", "uncertain"):
        raise PaymentStateError("invalid_error", "Use the PayPal client's safe rejected/uncertain error record.")
    return {key: error.get(key) for key in ("category", "status", "debug_id", "error_name")}


class PaymentStore:
    def __init__(self, path, merchant_id):
        self.path = Path(path)
        if str(self.path) == ":memory:":
            raise PaymentStateError("invalid_database", "Payment operations require a persistent database file.")
        self.merchant_id = _text(merchant_id, "merchant_id")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL, binding_hash TEXT NOT NULL,
                    approval_kind TEXT NOT NULL CHECK(approval_kind IN ('test_operator','human_ui')),
                    approved_by TEXT NOT NULL, approved_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, capture_id TEXT NOT NULL,
                    approval_id TEXT NOT NULL UNIQUE REFERENCES approvals(id),
                    request_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK(state IN ('prepared','submitting','uncertain','pending','verified','failed')),
                    refund_id TEXT UNIQUE, provider_status TEXT, last_error TEXT,
                    capture_provenance TEXT NOT NULL, verification_provenance TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    submitted_at TEXT, verified_at TEXT,
                    UNIQUE(merchant_id,capture_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL REFERENCES operations(id),
                    kind TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL,
                    occurred_at TEXT NOT NULL, details_json TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS approvals_no_update BEFORE UPDATE ON approvals
                    BEGIN SELECT RAISE(ABORT,'approvals are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS approvals_no_delete BEFORE DELETE ON approvals
                    BEGIN SELECT RAISE(ABORT,'approvals are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
            """)

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _transaction(self):
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    def _load(self, connection, operation_id):
        row = connection.execute("SELECT * FROM operations WHERE id=? AND merchant_id=?",
                                 (operation_id, self.merchant_id)).fetchone()
        if row is None:
            raise PaymentStateError("not_found", "Operation was not found for this merchant.")
        result = dict(row)
        approval = dict(connection.execute("SELECT * FROM approvals WHERE id=?", (row["approval_id"],)).fetchone())
        approval["snapshot"] = json.loads(approval.pop("snapshot_json"))
        result["approval"] = approval
        result["events"] = []
        for event in connection.execute("SELECT * FROM events WHERE operation_id=? ORDER BY id", (operation_id,)):
            item = dict(event)
            item["details"] = json.loads(item.pop("details_json"))
            result["events"].append(item)
        return result

    def _event(self, connection, operation_id, kind, before, after, details):
        connection.execute("INSERT INTO events(operation_id,kind,from_state,to_state,occurred_at,details_json) VALUES(?,?,?,?,?,?)",
                           (operation_id, kind, before, after, _now(), _json(details)))

    def get(self, operation_id):
        with closing(self._connect()) as connection:
            return self._load(connection, operation_id)

    def prepare(self, snapshot, capture_response, *, approval_kind, approved_by):
        """Trusted server approval only; caller must not copy provenance from a request body."""
        snapshot = _snapshot(snapshot)
        if approval_kind not in ("test_operator", "human_ui"):
            raise PaymentStateError("invalid_approval", "An explicit test_operator or human_ui approval is required.")
        approved_by = _text(approved_by, "approved_by")
        binding_hash = hashlib.sha256(_json({"merchant_id": self.merchant_id, **snapshot}).encode()).hexdigest()
        with self._transaction() as connection:
            existing = connection.execute("SELECT id FROM operations WHERE merchant_id=? AND capture_id=?",
                                          (self.merchant_id, snapshot["capture_id"])).fetchone()
            if existing:
                operation = self._load(connection, existing["id"])
                approval = operation["approval"]
                if (approval["binding_hash"] != binding_hash or approval["approval_kind"] != approval_kind
                        or approval["approved_by"] != approved_by):
                    raise PaymentStateError("approval_conflict", "This capture already has a different immutable approval.")
                return operation
            capture = _resource(capture_response, "GET", f"/v2/payments/captures/{snapshot['capture_id']}")
            if (capture.get("id") != snapshot["capture_id"] or capture.get("status") != "COMPLETED"
                    or _amount(capture) != snapshot["amount_minor"]):
                raise PaymentStateError("capture_mismatch", "Capture must be completed and match the exact approved full amount.")
            merchant_evidence = _payee(capture, self.merchant_id)
            operation_id, approval_id, request_id = (str(uuid.uuid4()) for _ in range(3))
            now = _now()
            connection.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?)",
                               (approval_id, self.merchant_id, _json(snapshot), binding_hash, approval_kind, approved_by, now))
            connection.execute("INSERT INTO operations(id,merchant_id,capture_id,approval_id,request_id,state,capture_provenance,created_at,updated_at) VALUES(?,?,?,?,?,'prepared',?,?,?)",
                               (operation_id, self.merchant_id, snapshot["capture_id"], approval_id, request_id, capture_response["provenance"], now, now))
            self._event(connection, operation_id, "approved", None, "prepared",
                        {"approval_kind": approval_kind, "approved_by": approved_by,
                         "capture_debug_id": capture_response.get("debug_id"), "merchant_evidence": merchant_evidence})
            return self._load(connection, operation_id)

    def claim(self, operation_id, current_snapshot):
        """Commit one claim before HTTP. Caller supplies current case/policy/payment facts."""
        current_snapshot = _snapshot(current_snapshot)
        with self._transaction() as connection:
            operation = self._load(connection, operation_id)
            if current_snapshot != operation["approval"]["snapshot"]:
                raise PaymentStateError("stale_approval", "Current facts or versions no longer match the approval.")
            if operation["state"] != "prepared":
                return {"claimed": False, "operation": operation}
            now = _now()
            changed = connection.execute("UPDATE operations SET state='submitting',submitted_at=?,updated_at=? WHERE id=? AND state='prepared'",
                                         (now, now, operation_id)).rowcount
            if changed != 1:
                raise PaymentStateError("claim_conflict", "Operation could not be claimed.")
            self._event(connection, operation_id, "submission_claimed", "prepared", "submitting", {})
            return {"claimed": True, "operation": self._load(connection, operation_id)}

    def _transition(self, connection, operation, state, kind, details, *, refund_id=None, provider_status=None, error=None):
        connection.execute("UPDATE operations SET state=?,refund_id=COALESCE(?,refund_id),provider_status=COALESCE(?,provider_status),last_error=?,updated_at=?,verified_at=?,verification_provenance=? WHERE id=?",
                           (state, refund_id, provider_status, error, _now(), _now() if state == "verified" else None,
                            details.get("provenance") if state == "verified" else None, operation["id"]))
        self._event(connection, operation["id"], kind, operation["state"], state, details)
        return self._load(connection, operation["id"])

    def record_submission(self, operation_id, response=None, *, error=None):
        """A POST receipt never verifies a refund. An interrupted claim is uncertain."""
        if (response is None) == (error is None):
            raise PaymentStateError("invalid_submission", "Provide either a response or a safe error record.")
        with self._transaction() as connection:
            operation = self._load(connection, operation_id)
            if operation["state"] not in {"submitting", "uncertain"}:
                raise PaymentStateError("invalid_state", "Only an outstanding claimed submission can be recorded.")
            if error is not None:
                details = _safe_error(error)
                # A later rejection says nothing definitive about an earlier
                # ambiguous attempt. Only the original outstanding request can fail here.
                state = ("failed" if operation["state"] == "submitting"
                         and details["category"] == "rejected" else "uncertain")
                return self._transition(connection, operation, state, "submission_error", details,
                                        error=details["error_name"] or "Submission outcome is uncertain.")
            refund_id = provider_status = None
            try:
                data = _resource(response, "POST", f"/v2/payments/captures/{operation['capture_id']}/refund", operation["request_id"])
                if response["provenance"] != operation["capture_provenance"]:
                    raise PaymentStateError("provenance_mismatch", "Submission provenance differs from the capture evidence.")
                received_id = _text(data.get("id"), "refund_id")
                if operation["refund_id"] is not None and received_id != operation["refund_id"]:
                    raise PaymentStateError("refund_mismatch", "Response names a different refund.")
                other = connection.execute("SELECT id FROM operations WHERE refund_id=? AND id<>?",
                                           (received_id, operation_id)).fetchone()
                if other:
                    raise PaymentStateError("refund_conflict", "Refund ID is already associated with another operation.")
                refund_id = received_id
                provider_status = _text(data.get("status"), "provider_status")
                if "amount" in data and _amount(data) != operation["approval"]["snapshot"]["amount_minor"]:
                    return self._transition(connection, operation, "uncertain", "submission_amount_mismatch",
                                            {"debug_id": response.get("debug_id")}, refund_id=refund_id,
                                            provider_status=provider_status, error="POST amount does not match approval.")
            except PaymentStateError as exc:
                return self._transition(connection, operation, "uncertain", "invalid_submission_response",
                                        {"code": exc.code}, refund_id=refund_id, provider_status=provider_status, error=str(exc))
            return self._transition(connection, operation, "pending", "submission_received",
                                    {"debug_id": response.get("debug_id")}, refund_id=refund_id,
                                    provider_status=provider_status)

    def record_readback(self, operation_id, response=None, *, error=None):
        """Verify only a separate matching sandbox GET; never replay the POST."""
        if (response is None) == (error is None):
            raise PaymentStateError("invalid_readback", "Provide either a GET response or a safe error record.")
        with self._transaction() as connection:
            operation = self._load(connection, operation_id)
            if operation["state"] not in {"pending", "uncertain", "verified"}:
                raise PaymentStateError("invalid_state", "There is no recorded refund awaiting readback.")
            if operation["refund_id"] is None:
                return self._transition(connection, operation, "uncertain", "refund_id_unknown", {},
                                        error="Refund ID is unknown; explicit reconciliation is required. No POST retry is permitted.")
            if error is not None:
                details = _safe_error(error)
                if operation["state"] == "verified":
                    raise PaymentStateError("terminal_state", "Do not replace existing verification with a failed lookup.")
                return self._transition(connection, operation, "uncertain", "readback_error", details,
                                        error="Independent refund readback failed; refund completion is unverified.")
            try:
                data = _resource(response, "GET", f"/v2/payments/refunds/{operation['refund_id']}")
                if response["provenance"] != operation["capture_provenance"]:
                    raise PaymentStateError("provenance_mismatch", "Readback provenance differs from the capture evidence.")
                snapshot = operation["approval"]["snapshot"]
                if data.get("id") != operation["refund_id"] or _amount(data) != snapshot["amount_minor"]:
                    raise PaymentStateError("refund_mismatch", "Refund ID or exact amount differs from approval.")
                merchant_evidence = _payee(data, self.merchant_id)
                captures = []
                for link in data.get("links", []):
                    if not isinstance(link, dict) or link.get("rel") != "up":
                        continue
                    if not isinstance(link.get("href"), str):
                        continue
                    url = urlsplit(link["href"])
                    if (url.scheme == "https" and url.hostname in {"api-m.paypal.com", "api.paypal.com", "api-m.sandbox.paypal.com", "api.sandbox.paypal.com"}
                            and not url.query and not url.fragment and not url.username and not url.password):
                        match = re.fullmatch(r"/v2/payments/captures/([A-Za-z0-9-]+)", url.path)
                        if match:
                            captures.append(match.group(1))
                if captures != [operation["capture_id"]]:
                    raise PaymentStateError("capture_association_unverified", "Independent refund details do not uniquely identify the approved capture.")
                provider_status = data.get("status")
                if provider_status == "COMPLETED":
                    state = "verified"
                elif provider_status == "PENDING":
                    state = "pending"
                elif provider_status in {"FAILED", "CANCELLED"}:
                    state = "failed"
                else:
                    raise PaymentStateError("unknown_provider_status", "Refund completion status is not recognized.")
            except (PaymentStateError, TypeError, ValueError) as exc:
                if operation["state"] == "verified":
                    raise PaymentStateError("terminal_state", "Conflicting readback cannot replace the recorded verification.") from None
                code = exc.code if isinstance(exc, PaymentStateError) else "invalid_readback"
                return self._transition(connection, operation, "uncertain", "readback_mismatch",
                                        {"code": code}, error="Independent refund readback did not match the approved operation.")
            if operation["state"] == "verified":
                if state != "verified":
                    raise PaymentStateError("terminal_state", "Conflicting status cannot replace the recorded verification.")
                return operation
            return self._transition(connection, operation, state, "refund_readback",
                                    {"debug_id": response.get("debug_id"), "merchant_evidence": merchant_evidence,
                                     "amount_minor": snapshot["amount_minor"], "currency": "USD",
                                     "capture_id": operation["capture_id"], "provenance": response["provenance"]}, provider_status=provider_status)
