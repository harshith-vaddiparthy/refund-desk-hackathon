"""Source IDs and exact-quotation validation for supplied conversation evidence."""
from copy import deepcopy
import re

CLAIM_KINDS = ("intent", "item_condition", "amount", "timeline", "promise", "other")
ISSUE_FIELDS = ("item_used", "request_date", "amount", "intent", "policy", "other")
EVIDENCE_FIELDS = {"summary", "claims", "issues"}
MAX_CLAIMS = 12
MAX_ISSUES = 6


def make_sources(customer_message):
    """Deterministic paragraph IDs scoped to the immutable case snapshot."""
    if not isinstance(customer_message, str) or not customer_message.strip():
        raise ValueError("Conversation must be non-empty text.")
    if len(customer_message) > 4000:
        raise ValueError("Conversation exceeds 4000 characters.")
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", customer_message.strip()) if part.strip()]
    if len(paragraphs) > 12:
        raise ValueError("Conversation exceeds 12 paragraphs.")
    return [{"id": f"M{index}", "text": part} for index, part in enumerate(paragraphs, 1)]


def sources_for_case(case):
    sources = make_sources(case.get("customer_message"))
    if "sources" in case and case["sources"] != sources:
        raise ValueError("Source IDs and text must match the supplied conversation paragraphs.")
    return sources


def schema_for(case, policy):
    """Constrain references and unknown fields from inputs, never evaluation labels."""
    source_ids = [source["id"] for source in sources_for_case(case)]
    facts = case["verified_transaction"]
    unknown = [field for field in ("item_used", "request_date") if facts.get(field) is None]
    clauses = policy.get("clauses") if isinstance(policy, dict) else None
    if (not isinstance(clauses, dict) or not clauses or
            any(not isinstance(key, str) or not 1 <= len(key) <= 32 or not isinstance(value, str)
                or not 1 <= len(value) <= 1000 for key, value in clauses.items())):
        raise ValueError("Policy citations require bounded clause IDs and exact text.")
    text = lambda size: {"type": "string", "minLength": 1, "maxLength": size}
    return {"type": "object", "additionalProperties": False,
        "required": ["summary", "claims", "issues", "recommendation", "rationale", "citations", "missing_information"],
        "properties": {
            "summary": text(512), "rationale": text(1024),
            "recommendation": {"type": "string", "enum": ["refund", "decline", "request_information"]},
            "missing_information": {"type": "array", "maxItems": 6,
                "items": {"type": "string", "enum": unknown + ["intent", "policy", "other"]}},
            "claims": {"type": "array", "minItems": 1, "maxItems": MAX_CLAIMS,
                "items": {"type": "object", "additionalProperties": False,
                    "required": ["kind", "source_id", "quote"],
                    "properties": {"kind": {"type": "string", "enum": list(CLAIM_KINDS)},
                        "source_id": {"type": "string", "enum": source_ids},
                        "quote": {"type": "string", "enum": [source["text"] for source in sources_for_case(case)]}}}},
            "issues": {"type": "array", "maxItems": MAX_ISSUES,
                "items": {"type": "object", "additionalProperties": False,
                    "required": ["kind", "field", "source_ids", "detail", "question", "owner"],
                    "properties": {"kind": {"type": "string", "enum": ["missing", "conflict"]},
                        "field": {"type": "string", "enum": list(ISSUE_FIELDS)},
                        "source_ids": {"type": "array", "maxItems": 4,
                            "items": {"type": "string", "enum": source_ids}},
                        "detail": text(512), "question": text(512),
                        "owner": {"type": "string", "enum": ["customer", "merchant"]}}}},
            "citations": {"type": "array", "minItems": 1, "maxItems": 2,
                "description": "Exact policy-clause citations; conversation M IDs belong only in claims/issues.",
                # Groq cannot disambiguate anyOf branches with two enum discriminators.
                # Both fields stay source-bound; assess() still verifies their exact pairing.
                "items": {"type": "object", "additionalProperties": False, "required": ["id", "quote"],
                    "properties": {"id": {"type": "string", "enum": list(clauses)},
                                   "quote": {"type": "string", "enum": list(clauses.values())}}}}}}


def invalid_evidence(errors, *, source_references_validated=False):
    return {"summary": "", "claims": [], "issues": [], "reply_draft": "",
            "reply_draft_source": None,
            "validation_status": "invalid", "errors": deepcopy(errors),
            "source_references_validated": source_references_validated,
            "semantic_truth_verified": False}


def compose_preapproval_reply(recommendation, issues):
    """Application wording for this unapproved action; never model payment-status prose."""
    if recommendation == "request_information":
        questions = [issue["question"] for issue in issues if issue["owner"] == "customer"]
        if questions:
            return "Before a merchant decision, please clarify:\n" + "\n".join(f"- {question}" for question in questions)
        return "Your request awaits merchant review."
    return "Your request is ready for a merchant decision. No refund has been approved or sent."


def validate_evidence(case, response):
    """Exact references and bounded shape only; this does not prove meaning or truth."""
    errors = []
    def reject(code, field, message):
        errors.append({"code": code, "field": field, "message": message})
    def bounded(value, limit):
        return isinstance(value, str) and bool(value.strip()) and len(value) <= limit
    try:
        sources = {source["id"]: source["text"] for source in sources_for_case(case)}
    except (ValueError, AttributeError):
        reject("invalid_sources", "sources", "Conversation source records are invalid.")
        return invalid_evidence(errors)
    if not isinstance(response, dict):
        reject("invalid_evidence", "evidence", "Evidence must be a structured model response.")
        return invalid_evidence(errors)
    for field, limit in (("summary", 512),):
        if not bounded(response.get(field), limit):
            reject("invalid_evidence_text", field, f"{field} must be non-empty and at most {limit} characters.")
    claims = response.get("claims")
    if not isinstance(claims, list) or not 1 <= len(claims) <= MAX_CLAIMS:
        reject("invalid_claims", "claims", "One to twelve attributed claims are required.")
        claims = []
    for index, claim in enumerate(claims):
        field = f"claims[{index}]"
        if not isinstance(claim, dict) or set(claim) != {"kind", "source_id", "quote"}:
            reject("invalid_claim", field, "A claim must contain only kind, source_id and the full source quote.")
            continue
        if claim.get("kind") not in CLAIM_KINDS:
            reject("invalid_claim", field, "Claim kind is invalid.")
        source_id, quote = claim.get("source_id"), claim.get("quote")
        if not isinstance(source_id, str) or source_id not in sources or not bounded(quote, 4000) or quote != sources[source_id]:
            reject("unsupported_source_quote", field, "Claim quotation must equal its full named conversation source paragraph.")
    issues = response.get("issues")
    if not isinstance(issues, list) or len(issues) > MAX_ISSUES:
        reject("invalid_issues", "issues", "At most six missing/conflicting evidence issues are allowed.")
        issues = []
    facts = case.get("verified_transaction", {})
    for index, issue in enumerate(issues):
        field = f"issues[{index}]"
        if not isinstance(issue, dict) or set(issue) != {"kind", "field", "source_ids", "detail", "question", "owner"}:
            reject("invalid_issue", field, "Issue fields do not match the evidence contract.")
            continue
        ids = issue.get("source_ids")
        if (issue.get("kind") not in ("missing", "conflict") or issue.get("field") not in ISSUE_FIELDS
                or issue.get("owner") not in ("customer", "merchant")
                or not bounded(issue.get("detail"), 512) or not bounded(issue.get("question"), 512)):
            reject("invalid_issue", field, "Issue kind, field, explanation or question is invalid.")
        if (not isinstance(ids, list) or len(ids) > 4 or any(not isinstance(i, str) or i not in sources for i in ids)
                or len(set(ids)) != len(ids)):
            reject("unsupported_issue_source", field, "Issue references must name distinct existing sources.")
        elif issue.get("kind") == "conflict" and not ids:
            reject("unsupported_conflict", field, "A conflict must identify its supporting conversation sources.")
        if issue.get("kind") == "missing":
            name = issue.get("field")
            if (name in ("item_used", "request_date") and facts.get(name) is not None) or (name == "amount" and facts.get("captured_amount_minor") is not None):
                reject("known_fact_reported_missing", field, "An already supplied fact must not be reported missing.")
    if errors:
        return invalid_evidence(errors)
    return {"summary": response["summary"], "claims": [dict(claim, id=f"C{i}") for i, claim in enumerate(claims, 1)],
            "issues": [dict(issue, id=f"I{i}") for i, issue in enumerate(issues, 1)],
            "reply_draft": compose_preapproval_reply(response.get("recommendation"), issues),
            "reply_draft_source": "application_template",
            "validation_status": "valid", "errors": [], "source_references_validated": True,
            "semantic_truth_verified": False}
