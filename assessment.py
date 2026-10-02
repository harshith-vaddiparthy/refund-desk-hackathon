"""Pure evidence and completeness guards for USD refund advice.

Pass the exact policy snapshot used for the model request and its recorded
version. These checks do not independently verify transaction provenance,
interpret arbitrary policy prose, or authorize payment.
"""

from copy import deepcopy
from datetime import date
from evidence import EVIDENCE_FIELDS, invalid_evidence, sources_for_case, validate_evidence


REQUIRED_FACTS = ("captured_amount_minor", "currency", "purchase_date", "request_date", "item_used")
RECOMMENDATIONS = ("refund", "decline", "request_information")
RESPONSE_FIELDS = {"recommendation", "rationale", "citations", "missing_information"} | EVIDENCE_FIELDS
MERCHANT_FACTS = ("request_date", "item_used")
_KNOWN_P2 = "A refund request received more than 30 calendar days after purchase must be declined."


def _deterministic_decline(context, facts, clauses, citations):
    if not context["valid"] or context["missing_verified_facts"] or clauses.get("P2") != _KNOWN_P2:
        return False
    if not isinstance(citations, list) or not any(isinstance(c, dict) and c.get("id") == "P2" and c.get("quote") == _KNOWN_P2 for c in citations):
        return False
    try:
        return (date.fromisoformat(facts["request_date"]) - date.fromisoformat(facts["purchase_date"])).days > 30
    except (TypeError, ValueError, KeyError):
        return False


def preflight(case, policy, *, policy_version):
    """Unknown merchant facts may be analyzed; missing payment facts may not."""
    errors = []
    missing = []

    def reject(code, field, message):
        errors.append({"code": code, "field": field, "message": message})

    facts = case.get("verified_transaction") if isinstance(case, dict) else None
    if not isinstance(facts, dict):
        reject("invalid_verified_facts", "verified_transaction", "Verified transaction facts must be an object.")
        facts = {}
    for field in REQUIRED_FACTS:
        value = facts.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(field)
            if field not in MERCHANT_FACTS or value is not None:
                reject("missing_verified_fact", field, "A required payment fact is missing or a supplied merchant fact is invalid.")
    try:
        sources_for_case(case)
    except (ValueError, AttributeError):
        reject("invalid_sources", "sources", "Conversation and source records must be valid and consistent.")

    captured = facts.get("captured_amount_minor")
    if "captured_amount_minor" not in missing and (type(captured) is not int or captured <= 0):
        reject("invalid_money", "captured_amount_minor", "Captured amount must be a positive integer number of cents.")
    if "currency" not in missing and facts.get("currency") != "USD":
        reject("invalid_currency", "currency", "This assessment supports verified USD captures only.")
    if "item_used" not in missing and type(facts.get("item_used")) is not bool:
        reject("invalid_verified_fact", "item_used", "Item-use status must be a verified boolean.")

    dates = {}
    for field in ("purchase_date", "request_date"):
        if field in missing:
            continue
        value = facts.get(field)
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
            dates[field] = parsed
        except (TypeError, ValueError):
            reject("invalid_verified_fact", field, "Date must use valid YYYY-MM-DD form.")
    if len(dates) == 2 and dates["request_date"] < dates["purchase_date"]:
        reject("contradictory_verified_facts", "request_date", "Request date cannot precede purchase date.")

    refunds = facts.get("completed_refunds_minor")
    if "completed_refunds_minor" in facts:
        if type(refunds) is not int or refunds < 0:
            reject("invalid_money", "completed_refunds_minor", "Completed refunds must be a non-negative integer number of cents.")
        elif type(captured) is int and refunds > captured:
            reject("contradictory_verified_facts", "completed_refunds_minor", "Completed refunds cannot exceed the captured amount.")

    clauses = policy.get("clauses") if isinstance(policy, dict) else None
    version = policy.get("version") if isinstance(policy, dict) else None
    if not isinstance(version, str) or not version.strip():
        reject("invalid_policy", "policy.version", "An explicit policy version is required.")
    if not isinstance(policy_version, str) or not policy_version.strip() or version != policy_version:
        reject("policy_version_mismatch", "policy.version", "Policy does not match the version recorded for this model request.")
    if (not isinstance(clauses, dict) or not clauses
            or any(not isinstance(key, str) or not key.strip()
                   or not isinstance(value, str) or not value.strip() for key, value in clauses.items())):
        reject("invalid_policy", "policy.clauses", "Policy clauses must map non-empty IDs to exact source text.")
    return {"valid": not errors, "errors": errors, "missing_verified_facts": missing,
            "missing_merchant_facts": [field for field in missing if field in MERCHANT_FACTS],
            "policy_version": version}


def assess(case, policy, model_response, *, policy_version):
    """Return guarded advice without modifying or correcting the original answer.

    REVIEW_NEEDS_INFORMATION is completed advice requiring evidence resolution.
    REVIEW_READY is reviewable refund/decline advice. REVIEW_INCOMPLETE exposes
    no accepted recommendation. None of these statuses
    authorizes a refund. ``policy_version`` comes from request metadata, not the
    model's own assertion.
    """
    context = preflight(case, policy, policy_version=policy_version)
    errors = context["errors"]
    missing = context["missing_verified_facts"]
    version = context["policy_version"]
    facts = case.get("verified_transaction", {}) if isinstance(case, dict) else {}
    facts = facts if isinstance(facts, dict) else {}
    captured, refunds = facts.get("captured_amount_minor"), facts.get("completed_refunds_minor")
    clauses = policy.get("clauses", {}) if isinstance(policy, dict) else {}
    clauses = clauses if isinstance(clauses, dict) else {}

    def reject(code, field, message):
        errors.append({"code": code, "field": field, "message": message})

    response = model_response if isinstance(model_response, dict) else {}
    if set(response) != RESPONSE_FIELDS:
        reject("invalid_model_response", "model_response", "Response fields must exactly match the evidence-review contract.")
    recommendation = response.get("recommendation")
    if recommendation not in RECOMMENDATIONS:
        reject("invalid_recommendation", "recommendation", "Recommendation must be refund, decline or request_information.")
    if not isinstance(response.get("rationale"), str) or not response["rationale"].strip() or len(response["rationale"]) > 1024:
        reject("invalid_model_response", "rationale", "A non-empty rationale of at most 1024 characters is required.")

    citations = response.get("citations")
    if not isinstance(citations, list) or not 1 <= len(citations) <= 2:
        reject("invalid_citation", "citations", "One or two exact policy citations are required.")
    else:
        for citation in citations:
            if (not isinstance(citation, dict) or set(citation) != {"id", "quote"}
                    or not isinstance(citation["id"], str) or not isinstance(citation["quote"], str)
                    or len(citation["id"]) > 32 or len(citation["quote"]) > 1000
                    or citation["id"] not in clauses or citation["quote"] != clauses[citation["id"]]):
                reject("invalid_citation", "citations", "Citation ID and quote must exactly match the supplied policy version.")

    requested = response.get("missing_information")
    allowed_missing = {field for field in MERCHANT_FACTS if facts.get(field) is None} | {"intent", "policy", "other"}
    valid_requested = (isinstance(requested, list) and len(requested) <= 6
                       and all(isinstance(item, str) and item in allowed_missing for item in requested)
                       and len(set(requested)) == len(requested))
    if not valid_requested:
        reject("invalid_model_response", "missing_information", "Missing information must identify distinct unresolved fields, not supplied facts.")
    elif requested:
        for field in requested:
            if field in facts and field not in missing and facts[field] is not None:
                reject("contradictory_model_state", field, "The model describes an already supplied verified fact as missing.")
    if recommendation in ("refund", "decline") and (missing or (valid_requested and requested)):
        reject("contradictory_recommendation", "recommendation", "A refund or decline recommendation cannot accompany missing information.")
    if (recommendation == "refund" and type(captured) is int and type(refunds) is int
            and captured > 0 and refunds >= captured):
        reject("contradictory_recommendation", "recommendation", "The verified transaction has no refundable balance.")

    evidence = validate_evidence(case, response)
    errors.extend(evidence["errors"])
    issues = evidence["issues"]
    confirmed_decline = recommendation == "decline" and _deterministic_decline(context, facts, clauses, citations)
    if issues and (recommendation == "refund" or (recommendation == "decline" and not confirmed_decline)):
        reject("unresolved_evidence", "issues", "Advice with unresolved evidence must request information before a refund/decline conclusion.")
    if recommendation == "request_information":
        if not issues:
            reject("missing_resolution_question", "issues", "A completed information request needs at least one concrete evidence question.")
        unknown = set(context["missing_merchant_facts"])
        issue_fields = {issue["field"] for issue in issues}
        if valid_requested and not unknown <= set(requested):
            reject("missing_fact_not_identified", "missing_information", "All unknown merchant confirmations must be identified.")
        if not unknown <= issue_fields:
            reject("missing_resolution_question", "issues", "Each unknown merchant confirmation needs a concrete resolution question.")
    if errors:
        evidence = invalid_evidence(errors, source_references_validated=evidence["source_references_validated"])
    status = ("REVIEW_INCOMPLETE" if errors else
              "REVIEW_NEEDS_INFORMATION" if recommendation == "request_information" else "REVIEW_READY")
    return {
        "status": status,
        "accepted_recommendation": recommendation if not errors else None,
        "evidence": evidence,
        "deterministic_decline_confirmed": confirmed_decline,
        "errors": errors,
        "missing_verified_facts": missing,
        "policy_version": version,
        "original_model_response": deepcopy(model_response),
        "payment_authorized": False,
        "human_approval_required_for_payment": True,
    }
