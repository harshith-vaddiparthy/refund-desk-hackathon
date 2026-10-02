"""Single-user Refund Desk coordinator. Browser inputs never select payment facts.

The client, model test seam and approval provenance are trusted construction-time
settings. This module connects existing review and payment modules; it does not
change their evidence or turn a model recommendation into approval by itself.
"""

from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import uuid

from benchmarks.local_ai import POLICY
from evidence import make_sources
from local_model import DIGEST
from model_config import Runtime, completed_report_matches, select_runtime
from payment_state import PaymentStateError, PaymentStore
from paypal import PayPalClient, PayPalError, parse_usd_cents
from review import review_case as run_review


_UNSET = object()


class ServiceError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise ServiceError("invalid_date", "Request date must use valid YYYY-MM-DD form.") from None


class RefundService:
    def __init__(self, data_dir, client, merchant_id, merchant_email, *, generator=None,
                 runtime=None, approval_kind="human_ui", approved_by="local merchant"):
        if getattr(client, "is_live", None) is not False and not isinstance(client, PayPalClient):
            raise ServiceError("invalid_client", "Use the sandbox client or an explicitly marked test client.")
        self.live = client.is_live is True
        if generator is not None and (self.live or not callable(generator)):
            raise ServiceError("invalid_generator", "Injected generators are restricted to explicit test clients.")
        if runtime is not None and not isinstance(runtime, Runtime):
            raise ServiceError("invalid_runtime", "Use an explicitly configured supported AI runtime.")
        self.runtime = runtime if runtime is not None else select_runtime()
        if approval_kind not in ("human_ui", "test_operator"):
            raise ServiceError("invalid_approval_mode", "Approval mode must be configured by the trusted server.")
        for value in (merchant_id, merchant_email, approved_by):
            if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 256:
                raise ServiceError("invalid_configuration", "Merchant and approver identity must be explicit non-empty text.")
        self.client, self.generator = client, generator
        self.merchant_id, self.merchant_email = merchant_id, merchant_email
        self.approval_kind, self.approved_by = approval_kind, approved_by
        self.provenance = "paypal_sandbox" if self.live else "injected_test_transport"
        self.policy = deepcopy(POLICY)
        self.data_dir = Path(data_dir)
        self._private_dir(self.data_dir)
        self.data_dir = self.data_dir.resolve()
        self.reviews_dir = self.data_dir / "reviews"
        self._private_dir(self.reviews_dir)
        self.db_path = self.data_dir / "desk.sqlite3"
        self.lock_path = self.data_dir / ".service.lock"
        for path in (self.db_path, self.lock_path):
            os.close(self._private_file(path))
        self._mutex = threading.Lock()
        self.payments = PaymentStore(self.db_path, merchant_id)
        with closing(self._connect()) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY, capture_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL, case_version TEXT NOT NULL,
                    case_json TEXT NOT NULL, capture_json TEXT NOT NULL, policy_json TEXT NOT NULL,
                    latest_review_id TEXT, operation_id TEXT REFERENCES operations(id), error_json TEXT
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                    created_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
                    report_hash TEXT, error_json TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_review_per_case ON reviews(case_id)
                    WHERE status='reviewing';
                CREATE TRIGGER IF NOT EXISTS case_inputs_immutable
                    BEFORE UPDATE OF capture_id,case_version,case_json,capture_json,policy_json ON cases
                    BEGIN SELECT RAISE(ABORT,'case inputs are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS case_no_delete BEFORE DELETE ON cases
                    BEGIN SELECT RAISE(ABORT,'cases are retained'); END;
                CREATE TRIGGER IF NOT EXISTS finished_review_immutable BEFORE UPDATE ON reviews
                    WHEN OLD.status <> 'reviewing'
                    BEGIN SELECT RAISE(ABORT,'finished reviews are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS review_no_delete BEFORE DELETE ON reviews
                    BEGIN SELECT RAISE(ABORT,'reviews are retained'); END;
                CREATE TABLE IF NOT EXISTS case_revisions (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                    case_version TEXT NOT NULL, previous_version TEXT,
                    case_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(case_id,case_version)
                );
                CREATE TRIGGER IF NOT EXISTS revision_no_update BEFORE UPDATE ON case_revisions
                    BEGIN SELECT RAISE(ABORT,'case revisions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS revision_no_delete BEFORE DELETE ON case_revisions
                    BEGIN SELECT RAISE(ABORT,'case revisions are retained'); END;
            """)
        # Preserve v1 snapshots and finished reports. Only the identity's active
        # pointer changes; revisions themselves are never rewritten.
        with self._transaction() as connection:
            if "active_revision_id" not in {row["name"] for row in connection.execute("PRAGMA table_info(cases)")}:
                connection.execute("ALTER TABLE cases ADD COLUMN active_revision_id TEXT REFERENCES case_revisions(id)")
            if "case_version" not in {row["name"] for row in connection.execute("PRAGMA table_info(reviews)")}:
                connection.execute("ALTER TABLE reviews ADD COLUMN case_version TEXT")
            for row in connection.execute("SELECT id,case_version,case_json,created_at FROM cases WHERE active_revision_id IS NULL").fetchall():
                revision_id = str(uuid.uuid4())
                connection.execute("INSERT INTO case_revisions(id,case_id,case_version,case_json,created_at) VALUES(?,?,?,?,?)",
                                   (revision_id, row["id"], row["case_version"], row["case_json"], row["created_at"]))
                connection.execute("UPDATE cases SET active_revision_id=? WHERE id=?", (revision_id, row["id"]))
        # A free process lock proves there is no coordinator still running the
        # recorded review. Recovery records interruption; it never restarts AI.
        try:
            with self._mutation(), self._transaction() as connection:
                connection.execute("UPDATE reviews SET status='REVIEW_INCOMPLETE',finished_at=?,error_json=? WHERE status='reviewing'",
                                   (_now(), _json({"code": "review_interrupted", "message": "Previous review was interrupted; an explicit new review is required."})))
        except ServiceError as exc:
            if exc.code != "busy":
                raise

    @staticmethod
    def _private_dir(path):
        if path.is_symlink():
            raise ServiceError("unsafe_storage", "Data directories must not be symlinks.")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ServiceError("unsafe_storage", "Data directories must belong to this local user.")
        path.chmod(0o700)

    @staticmethod
    def _private_file(path):
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            os.close(descriptor)
            raise ServiceError("unsafe_storage", "Data files must be regular files owned by this local user.")
        os.fchmod(descriptor, 0o600)
        return descriptor

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _transaction(self):
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    @contextmanager
    def _mutation(self):
        if not self._mutex.acquire(blocking=False):
            raise ServiceError("busy", "Another case action is running; inspect its status before retrying.")
        descriptor = None
        try:
            descriptor = self._private_file(self.lock_path)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ServiceError("busy", "Another case action is running; inspect its status before retrying.") from None
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            self._mutex.release()

    def _busy(self):
        if self._mutex.locked():
            return True
        descriptor = self._private_file(self.lock_path)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                return True
        finally:
            os.close(descriptor)

    def _case(self, case_id):
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            revision = (connection.execute("SELECT case_version,case_json FROM case_revisions WHERE id=? AND case_id=?",
                                           (row["active_revision_id"], case_id)).fetchone()
                        if row is not None and row["active_revision_id"] else None)
            # A crash after prepare but before linking the case still leaves an
            # immutable approval. It must lock revisions and remain resumable.
            unlinked = (connection.execute("SELECT id FROM operations WHERE merchant_id=? AND capture_id=?",
                                           (self.merchant_id, row["capture_id"])).fetchone()
                        if row is not None and not row["operation_id"] else None)
        if row is None:
            raise ServiceError("not_found", "Case was not found.")
        result = dict(row)
        if unlinked is not None:
            result["operation_id"] = unlinked["id"]
        if result["active_revision_id"]:
            if revision is None:
                raise ServiceError("revision_unavailable", "The active immutable case revision is unavailable.")
            result.update(case_version=revision["case_version"], case_json=revision["case_json"])
        for field in ("case", "capture", "policy", "error"):
            value = result.pop(field + "_json")
            result[field] = json.loads(value) if value else None
        return result

    def _revision_history(self, case_id):
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT case_version,created_at,case_json FROM case_revisions WHERE case_id=? ORDER BY rowid",
                                      (case_id,)).fetchall()
            if not rows:
                rows = connection.execute("SELECT case_version,created_at,case_json FROM cases WHERE id=?", (case_id,)).fetchall()
        return [{"case_version": row["case_version"], "created_at": row["created_at"],
                 "resolution_note": json.loads(row["case_json"]).get("resolution_note")} for row in rows]

    def _error(self, case_id, code=None, message=None):
        with self._transaction() as connection:
            connection.execute("UPDATE cases SET error_json=? WHERE id=?",
                               (_json({"code": code, "message": message}) if code else None, case_id))

    def _capture(self, capture_id):
        try:
            response = self.client.get_capture(capture_id)
        except PayPalError:
            raise ServiceError("capture_lookup_failed", "PayPal capture lookup failed; no refund was submitted.") from None
        except Exception:
            raise ServiceError("capture_lookup_failed", "Capture lookup could not be completed; no refund was submitted.") from None
        if (not isinstance(response, dict) or response.get("environment") != "sandbox"
                or response.get("provenance") != self.provenance or response.get("method") != "GET"
                or response.get("path") != f"/v2/payments/captures/{capture_id}" or response.get("status") != 200):
            raise ServiceError("capture_unverified", "Capture response did not establish the expected sandbox lookup.")
        data = response.get("data")
        if not isinstance(data, dict) or data.get("id") != capture_id or data.get("status") != "COMPLETED":
            raise ServiceError("capture_not_refundable", "Only completed, not partially or fully refunded captures are supported.")
        payee = data.get("payee")
        if (not isinstance(payee, dict) or payee.get("merchant_id") != self.merchant_id
                or payee.get("email_address") != self.merchant_email):
            raise ServiceError("merchant_unverified", "Capture must return the exact configured merchant ID and email.")
        amount = data.get("amount")
        try:
            if not isinstance(amount, dict) or amount.get("currency_code") != "USD":
                raise ValueError
            cents = parse_usd_cents(amount.get("value"))
            if not 0 < cents <= 2**63 - 1:
                raise ValueError
            purchased = datetime.fromisoformat(data["create_time"].replace("Z", "+00:00"))
            if purchased.tzinfo is None:
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ServiceError("capture_unverified", "Capture must establish an exact positive USD amount and purchase timestamp.") from None
        # COMPLETED is required above; also reject an explicit prior-refund total
        # if the provider supplies it rather than treating it as unused metadata.
        for breakdown_name in ("seller_receivable_breakdown", "seller_payable_breakdown"):
            breakdown = data.get(breakdown_name)
            prior = breakdown.get("total_refunded_amount") if isinstance(breakdown, dict) else None
            if prior is not None:
                try:
                    if prior.get("currency_code") != "USD" or parse_usd_cents(prior.get("value")) != 0:
                        raise ValueError
                except (AttributeError, ValueError):
                    raise ServiceError("capture_not_refundable", "Prior refunds or uncertain refund totals are outside this full-refund flow.") from None
        normalized = {"capture_id": capture_id, "amount_minor": cents, "currency": "USD",
                      "purchase_date": purchased.astimezone(timezone.utc).date().isoformat(),
                      "merchant_id": self.merchant_id, "merchant_email": self.merchant_email,
                      "provenance": self.provenance, "debug_id": response.get("debug_id"), "checked_at": _now()}
        return response, normalized

    @staticmethod
    def _same_capture(first, second):
        return all(first[key] == second[key] for key in
                   ("capture_id", "amount_minor", "currency", "purchase_date", "merchant_id", "merchant_email", "provenance"))

    @staticmethod
    def _merchant_facts(item_used, request_date, purchase_date=None):
        if item_used is not None and type(item_used) is not bool:
            raise ServiceError("invalid_item_status", "Item-use status must be a merchant-confirmed boolean or unknown.")
        if request_date is not None:
            requested = _date(request_date)
            if requested > datetime.now(timezone.utc).date():
                raise ServiceError("invalid_date", "Request date cannot be in the future.")
            if purchase_date is not None and requested < _date(purchase_date):
                raise ServiceError("invalid_date", "Request date cannot precede the verified purchase date.")

    @staticmethod
    def _sources(customer_message):
        try:
            return make_sources(customer_message)
        except ValueError as exc:
            raise ServiceError("invalid_message", str(exc)) from None

    @property
    def ai_runtime(self):
        descriptor = self.runtime.public_descriptor()
        if self.generator is not None:
            descriptor = {"provider": "test", "model": descriptor["model"], "location": "test"}
        return descriptor

    def create_case(self, capture_id, customer_message, item_used=None, request_date=None):
        if not isinstance(capture_id, str) or not re.fullmatch(r"[A-Za-z0-9]{1,64}", capture_id):
            raise ServiceError("invalid_capture", "A valid PayPal capture ID is required.")
        sources = self._sources(customer_message)
        self._merchant_facts(item_used, request_date)
        with self._mutation():
            with closing(self._connect()) as connection:
                existing = connection.execute("SELECT id FROM cases WHERE capture_id=?", (capture_id,)).fetchone()
            if existing:
                current = self._case(existing["id"])
                if (current["case"]["customer_message"] != customer_message
                        or current["case"]["verified_transaction"]["item_used"] is not item_used
                        or current["case"]["verified_transaction"]["request_date"] != request_date):
                    raise ServiceError("case_exists", "This capture already has a case. Open it to resolve its current facts.")
                case_id = existing["id"]
            else:
                _, captured = self._capture(capture_id)
                self._merchant_facts(item_used, request_date, captured["purchase_date"])
                facts = {"capture_id": capture_id, "currency": "USD", "captured_amount_minor": captured["amount_minor"],
                         "completed_refunds_minor": 0, "purchase_date": captured["purchase_date"],
                         "request_date": request_date, "item_used": item_used}
                case = {"verified_transaction": facts, "customer_message": customer_message,
                        "sources": sources, "resolution_note": None}
                case_id = str(uuid.uuid4())
                revision_id, created_at, version = str(uuid.uuid4()), _now(), _hash(case)
                with self._transaction() as connection:
                    connection.execute("INSERT INTO cases(id,capture_id,created_at,case_version,case_json,capture_json,policy_json) VALUES(?,?,?,?,?,?,?)",
                                       (case_id, capture_id, created_at, version, _json(case), _json(captured), _json(self.policy)))
                    connection.execute("INSERT INTO case_revisions(id,case_id,case_version,case_json,created_at) VALUES(?,?,?,?,?)",
                                       (revision_id, case_id, version, _json(case), created_at))
                    connection.execute("UPDATE cases SET active_revision_id=? WHERE id=?", (revision_id, case_id))
        return self.get_case(case_id)

    def resolve_case(self, case_id, expected_version, *, customer_message=_UNSET,
                     item_used=_UNSET, request_date=_UNSET, resolution_note=_UNSET):
        if not isinstance(expected_version, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_version):
            raise ServiceError("invalid_case_version", "Identify the exact current case version.")
        with self._mutation():
            record = self._case(case_id)
            if record["operation_id"]:
                raise ServiceError("approval_exists", "A payment operation locks its approved case revision.")
            if expected_version != record["case_version"]:
                raise ServiceError("stale_case", "The case changed. Review its current evidence before resolving it.")
            case = deepcopy(record["case"])
            if customer_message is not _UNSET:
                case["customer_message"] = customer_message
            # Materialize sources for legacy snapshots only when a revision is
            # actually written. Old finished reports keep their original input.
            sources = self._sources(case["customer_message"])
            facts = case["verified_transaction"]
            if item_used is not _UNSET:
                facts["item_used"] = item_used
            if request_date is not _UNSET:
                facts["request_date"] = request_date
            self._merchant_facts(facts["item_used"], facts["request_date"], record["capture"]["purchase_date"])
            note = case.get("resolution_note") if resolution_note is _UNSET else resolution_note
            if note is not None and (not isinstance(note, str) or len(note) > 2000):
                raise ServiceError("invalid_resolution", "Resolution explanation must be text of at most 2000 characters.")
            changed = (case["customer_message"] != record["case"]["customer_message"]
                       or facts != record["case"]["verified_transaction"]
                       or note != record["case"].get("resolution_note"))
            if changed:
                if not isinstance(note, str) or not note.strip():
                    raise ServiceError("resolution_required", "Explain the changed facts or conversation before saving a revision.")
                case.update(sources=sources, resolution_note=note)
                # Include lineage so returning to earlier values cannot revive
                # a stale expected_version or an old reviewed snapshot.
                version = _hash({"previous_version": expected_version, "case": case})
                revision_id = str(uuid.uuid4())
                with self._transaction() as connection:
                    connection.execute("INSERT INTO case_revisions(id,case_id,case_version,previous_version,case_json,created_at) VALUES(?,?,?,?,?,?)",
                                       (revision_id, case_id, version, expected_version, _json(case), _now()))
                    changed_row = connection.execute("UPDATE cases SET active_revision_id=?,latest_review_id=NULL,error_json=NULL WHERE id=? AND active_revision_id=? AND operation_id IS NULL",
                                                     (revision_id, case_id, record["active_revision_id"]))
                    if changed_row.rowcount != 1:
                        raise ServiceError("stale_case", "The case changed before the revision was saved.")
        return self.get_case(case_id)

    def _reviews(self, case_id):
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM reviews WHERE case_id=? ORDER BY created_at,id", (case_id,))]

    def _report(self, row):
        if not row["report_hash"]:
            raise ServiceError("review_incomplete", "Review has no complete immutable report.")
        path = self.reviews_dir / row["id"] / "report.json"
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError
                raw = source.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != row["report_hash"]:
                raise ValueError
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (OSError, ValueError, UnicodeError, RecursionError):
            raise ServiceError("review_changed", "Stored review is unavailable or no longer matches its immutable hash.") from None

    def _model_completed(self, report, *, selected=True, historical=False):
        model = report.get("model")
        if (not isinstance(model, dict) or report.get("error") is not None
                or report.get("generator_invocations") != 1 or model.get("completed_model_response") is not True):
            return False
        if report.get("runtime_provenance") == "injected_test_generator":
            if any(report.get(field, 0) != 0 for field in ("model_calls", "model_request_attempts", "completed_model_responses")):
                return False
            if historical:
                return True  # Historical test output stays labelled as test output.
            if self.live or self.generator is None:
                return False
            if not selected:
                return True
            descriptor = self.runtime.public_descriptor()
            return (model.get("model") == descriptor["model"]
                    and (descriptor["provider"] != "ollama" or model.get("digest") == DIGEST))
        return completed_report_matches(report, self.runtime if selected else None)

    def _review_valid(self, record, row, report, *, selected=True):
        assessment = report.get("assessment") or {}
        if not isinstance(assessment, dict):
            return False
        answer = assessment.get("original_model_response") or {}
        if not isinstance(answer, dict):
            return False
        valid = (row["status"] == "REVIEW_READY" and report.get("status") == "REVIEW_READY"
                 and report.get("error") is None and report.get("accepted_recommendation") == "refund"
                 and assessment.get("status") == "REVIEW_READY" and not assessment.get("errors")
                 and assessment.get("accepted_recommendation") == "refund" and answer.get("recommendation") == "refund"
                 and not answer.get("missing_information") and report.get("case") == record["case"]
                 and (row.get("case_version") is None or row["case_version"] == record["case_version"])
                 and report.get("policy") == record["policy"] == self.policy
                 and report.get("policy_version") == self.policy["version"]
                 and self._model_completed(report, selected=selected)
                 and any(citation.get("id") == "P1" and citation.get("quote") == self.policy["clauses"]["P1"]
                         for citation in answer.get("citations", []) if isinstance(citation, dict)))
        if "sources" in record["case"]:
            evidence = report.get("evidence")
            valid = (valid and isinstance(evidence, dict) and evidence.get("validation_status") == "valid"
                     and evidence.get("source_references_validated") is True and not evidence.get("errors")
                     and isinstance(evidence.get("issues"), list) and not evidence["issues"]
                     and not answer.get("issues"))
        return bool(valid)

    @staticmethod
    def _eligible(record):
        facts = record["case"]["verified_transaction"]
        if facts.get("item_used") is not False or facts.get("request_date") is None:
            return False
        try:
            days = (_date(facts["request_date"]) - _date(facts["purchase_date"])).days
            return 0 <= days <= 30
        except ServiceError:
            return False

    def _review_public(self, row):
        result = {"id": row["id"], "created_at": row["created_at"], "finished_at": row["finished_at"],
                  "status": row["status"], "review_hash": row["report_hash"], "accepted_recommendation": None,
                  "case_version": row.get("case_version"), "evidence": None, "model": None,
                  "rationale": None, "citations": [], "missing_information": [], "errors": [],
                  "model_calls": 0, "completed_model_responses": 0, "runtime_provenance": None,
                  "error": json.loads(row["error_json"]) if row["error_json"] else None}
        if row["status"] == "reviewing":
            return result
        try:
            report = self._report(row)
            assessment = report.get("assessment") or {}
            answer = assessment.get("original_model_response")
            answer = answer if isinstance(answer, dict) else {}
            citations = answer.get("citations")
            missing = answer.get("missing_information")
            evidence = report.get("evidence") or assessment.get("evidence")
            result.update(accepted_recommendation=report.get("accepted_recommendation"),
                          rationale=answer.get("rationale") if isinstance(answer.get("rationale"), str) else None,
                          citations=[{"id": c["id"], "quote": c["quote"]} for c in citations
                                     if isinstance(c, dict) and isinstance(c.get("id"), str) and isinstance(c.get("quote"), str)]
                                    if isinstance(citations, list) else [],
                          missing_information=[field for field in missing if isinstance(field, str)] if isinstance(missing, list) else [],
                          errors=assessment.get("errors", []) or (report.get("preflight") or {}).get("errors", []),
                          model_calls=report.get("model_calls", 0),
                          completed_model_responses=report.get("completed_model_responses", 0),
                          runtime_provenance=report.get("runtime_provenance"),
                          evidence=deepcopy(evidence) if isinstance(evidence, dict) else None,
                          model=deepcopy(report.get("model")) if isinstance(report.get("model"), dict) else None,
                          error=result["error"] or ({"code": "review_incomplete", "message": report["error"]} if report.get("error") else None))
            if result["case_version"] is None and isinstance(report.get("case"), dict):
                result["case_version"] = _hash(report["case"])
            if result["status"] in {"REVIEW_READY", "REVIEW_NEEDS_INFORMATION"} and not self._model_completed(report, selected=False, historical=True):
                result.update(status="REVIEW_INCOMPLETE", accepted_recommendation=None,
                              error={"code": "model_unconfirmed", "message": "A completed model response has not been established."})
        except ServiceError as exc:
            result.update(status="REVIEW_INCOMPLETE", error={"code": exc.code, "message": exc.message})
        return result

    def get_case(self, case_id):
        record = self._case(case_id)
        rows = self._reviews(case_id)
        latest = next((row for row in rows if row["id"] == record["latest_review_id"]), None)
        reviews = [self._review_public(row) for row in rows]
        latest_public = next((row for row in reviews if row["id"] == record["latest_review_id"]), None)
        operation = self.payments.get(record["operation_id"]) if record["operation_id"] else None
        ready = False
        runtime_changed = False
        if latest and latest_public and latest_public["status"] in {"REVIEW_READY", "REVIEW_NEEDS_INFORMATION"}:
            try:
                runtime_changed = not self._model_completed(self._report(latest))
            except ServiceError:
                pass
        if latest and latest_public and latest_public["status"] == "REVIEW_READY":
            try:
                ready = self._review_valid(record, latest, self._report(latest), selected=operation is None) and self._eligible(record)
            except ServiceError:
                pass
        busy = self._busy()
        state = operation["state"] if operation else (
            "reviewing" if latest_public and latest_public["status"] == "reviewing" else
            "needs_information" if latest_public and latest_public["status"] == "REVIEW_NEEDS_INFORMATION" else
            "review_ready" if latest_public and latest_public["status"] == "REVIEW_READY" else
            "review_incomplete" if latest_public else "case_open")
        facts = record["case"]["verified_transaction"]
        sources = record["case"].get("sources")
        if sources is None:
            try:
                sources = self._sources(record["case"]["customer_message"])
            except ServiceError:
                # V1 accepted messages exceeding today's paragraph cap. Its
                # original snapshot and finished report must remain readable.
                sources = [{"id": "M1", "text": record["case"]["customer_message"].strip()}]
        events = [{"kind": "case_created", "occurred_at": record["created_at"], "details": {"case_version": record["case_version"]}}]
        history = self._revision_history(case_id)
        events[0]["details"]["case_version"] = history[0]["case_version"]
        events.extend({"kind": "case_resolved", "occurred_at": revision["created_at"],
                       "details": {"case_version": revision["case_version"], "resolution_note": revision["resolution_note"]}}
                      for revision in history[1:])
        events.extend({"kind": "review_completed" if row["finished_at"] else "review_started",
                       "occurred_at": row["finished_at"] or row["created_at"],
                       "details": {"review_id": row["id"], "status": row["status"], "review_hash": row["report_hash"]}} for row in rows)
        events.extend(operation["events"] if operation else [])
        blocked_capture = record["error"] and record["error"]["code"] in {"capture_changed", "capture_not_refundable", "merchant_unverified", "capture_unverified"}
        prepared_approval_matches = operation and operation["state"] == "prepared" and (
            operation["approval"]["approval_kind"] == self.approval_kind
            and operation["approval"]["approved_by"] == self.approved_by)
        return {"id": case_id, "capture_id": record["capture_id"], "state": state,
                "created_at": record["created_at"], "case_version": record["case_version"],
                "customer_message": record["case"]["customer_message"], "item_used": facts["item_used"],
                "sources": deepcopy(sources),
                "resolution_note": record["case"].get("resolution_note"), "revision_history": history,
                "request_date": facts["request_date"], "purchase_date": facts["purchase_date"],
                "amount_minor": facts["captured_amount_minor"], "currency": "USD",
                "transaction_provenance": record["capture"]["provenance"],
                "transaction_verified_by_service": record["capture"]["provenance"] == "paypal_sandbox",
                "physical_facts_provenance": "merchant_supplied", "capture_read_at": record["capture"]["checked_at"],
                "policy": deepcopy(record["policy"]), "latest_review": latest_public, "reviews": reviews,
                "operation": operation, "events": sorted(events, key=lambda event: event["occurred_at"]),
                "approval_kind": self.approval_kind, "approved_by": self.approved_by,
                "can_review": not busy and operation is None and (latest_public is None or latest_public["status"] == "REVIEW_INCOMPLETE" or runtime_changed),
                "can_resolve": not busy and operation is None,
                "can_approve": bool(not busy and ready and not blocked_capture and (operation is None or prepared_approval_matches)),
                "error": record["error"]}

    def list_cases(self):
        with closing(self._connect()) as connection:
            ids = [row["id"] for row in connection.execute("SELECT id FROM cases ORDER BY created_at DESC,id")]
        return [self.get_case(case_id) for case_id in ids]

    def review_case(self, case_id):
        with self._mutation():
            self._review_case_locked(case_id)
        return self.get_case(case_id)

    def _review_case_locked(self, case_id):
        record = self._case(case_id)
        if record["operation_id"]:
            raise ServiceError("approval_exists", "A prepared payment locks its case and review.")
        if record["policy"] != self.policy:
            raise ServiceError("policy_changed", "This immutable case belongs to an earlier policy version.")
        rows = self._reviews(case_id)
        latest = next((row for row in rows if row["id"] == record["latest_review_id"]), None)
        if latest and latest["status"] in {"REVIEW_READY", "REVIEW_NEEDS_INFORMATION"}:
            try:
                report = self._report(latest)
                if (self._model_completed(report) and report.get("case") == record["case"]
                        and (latest.get("case_version") is None or latest["case_version"] == record["case_version"])):
                    return
            except ServiceError:
                pass
        if latest and latest["status"] == "reviewing":
            raise ServiceError("busy", "A review is already recorded as running.")
        review_id = str(uuid.uuid4())
        with self._transaction() as connection:
            connection.execute("INSERT INTO reviews(id,case_id,created_at,status,case_version) VALUES(?,?,?,'reviewing',?)",
                               (review_id, case_id, _now(), record["case_version"]))
            connection.execute("UPDATE cases SET latest_review_id=?,error_json=NULL WHERE id=?", (review_id, case_id))
        report_hash, error, status = None, None, "REVIEW_INCOMPLETE"
        try:
            output = self.reviews_dir / review_id
            report = run_review(record["case"], record["policy"], facts_provenance="supplied", output_dir=output,
                                generator=self.generator, runtime=self.runtime)
            output.chmod(0o700)
            path = output / "report.json"
            os.close(self._private_file(path))
            raw = path.read_bytes()
            report_hash = hashlib.sha256(raw).hexdigest()
            status = report["status"]
        except Exception as exc:
            error = {"code": "review_failed", "message": f"Review could not complete ({type(exc).__name__}); no payment was submitted."}
        with self._transaction() as connection:
            connection.execute("UPDATE reviews SET status=?,finished_at=?,report_hash=?,error_json=? WHERE id=? AND status='reviewing'",
                               (status, _now(), report_hash, _json(error) if error else None, review_id))

    def _readback(self, case_id, operation):
        response, error = None, None
        try:
            response = self.client.get_refund(operation["refund_id"])
        except PayPalError as exc:
            error = exc.as_dict()
        except Exception:
            error = {"category": "uncertain", "error_name": "READBACK_FAILED"}
        try:
            if error:
                self.payments.record_readback(operation["id"], error=error)
            else:
                self.payments.record_readback(operation["id"], response)
        except PaymentStateError:
            self._error(case_id, "readback_conflict", "Latest lookup could not replace the retained refund evidence; no new refund was submitted.")

    def approve(self, case_id, review_hash):
        with self._mutation():
            self._approve_locked(case_id, review_hash)
        return self.get_case(case_id)

    def _approve_locked(self, case_id, review_hash):
        record = self._case(case_id)
        if not isinstance(review_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", review_hash):
            raise ServiceError("invalid_review_hash", "Approve the exact stored review hash.")
        operation = self.payments.get(record["operation_id"]) if record["operation_id"] else None
        if operation and operation["approval"]["snapshot"]["review_hash"] != review_hash:
            raise ServiceError("stale_review", "Approval does not identify the operation's original review.")
        if operation and operation["state"] != "prepared":
            return
        rows = self._reviews(case_id)
        latest = next((row for row in rows if row["id"] == record["latest_review_id"]), None)
        if not latest or latest["report_hash"] != review_hash:
            raise ServiceError("stale_review", "A current complete review must be approved by its exact report hash.")
        report = self._report(latest)
        if not self._review_valid(record, latest, report, selected=operation is None) or not self._eligible(record):
            raise ServiceError("not_approvable", "Only a complete refund recommendation for an unused item within 30 days can proceed.")
        try:
            response, captured = self._capture(record["capture_id"])
            if not self._same_capture(record["capture"], captured):
                raise ServiceError("capture_changed", "Current PayPal facts differ from the reviewed capture; no refund was submitted.")
        except ServiceError as exc:
            self._error(case_id, exc.code, exc.message)
            raise
        snapshot = {"capture_id": record["capture_id"], "amount_minor": captured["amount_minor"], "currency": "USD",
                    "case_version": record["case_version"], "policy_version": record["policy"]["version"], "review_hash": review_hash}
        try:
            operation = self.payments.prepare(snapshot, response, approval_kind=self.approval_kind, approved_by=self.approved_by)
            # Persist the link before claim and before any payment HTTP call.
            with self._transaction() as connection:
                connection.execute("UPDATE cases SET operation_id=?,error_json=NULL WHERE id=?", (operation["id"], case_id))
            claim = self.payments.claim(operation["id"], snapshot)
            if claim["claimed"]:
                try:
                    receipt = self.client.refund_capture(record["capture_id"], captured["amount_minor"], operation["request_id"])
                except PayPalError as exc:
                    operation = self.payments.record_submission(operation["id"], error=exc.as_dict())
                except Exception:
                    operation = self.payments.record_submission(operation["id"], error={"category": "uncertain", "error_name": "REFUND_RESULT_UNKNOWN"})
                else:
                    operation = self.payments.record_submission(operation["id"], receipt)
                if operation["refund_id"] and operation["state"] in {"pending", "uncertain"}:
                    self._readback(case_id, operation)
        except PaymentStateError as exc:
            self._error(case_id, exc.code, str(exc))
            raise ServiceError(exc.code, str(exc)) from None

    def refresh(self, case_id):
        with self._mutation():
            record = self._case(case_id)
            if record["operation_id"]:
                operation = self.payments.get(record["operation_id"])
                if operation["state"] == "submitting":
                    operation = self.payments.record_submission(operation["id"], error={"category": "uncertain", "error_name": "INTERRUPTED_SUBMISSION"})
                if operation["refund_id"] and operation["state"] in {"pending", "uncertain", "verified"}:
                    self._readback(case_id, operation)
                elif operation["state"] == "uncertain":
                    self._error(case_id, "refund_id_unknown", "Refund outcome is uncertain and no refund ID is available. Reconciliation is required; do not submit again.")
            else:
                try:
                    _, captured = self._capture(record["capture_id"])
                    if not self._same_capture(record["capture"], captured):
                        raise ServiceError("capture_changed", "Current PayPal facts differ from the immutable case.")
                    self._error(case_id)
                except ServiceError as exc:
                    self._error(case_id, exc.code, exc.message)
        return self.get_case(case_id)
