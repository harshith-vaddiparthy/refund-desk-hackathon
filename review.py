#!/usr/bin/env python3
"""Review one case with local AI and deterministic guards. No payment operations."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

from assessment import assess, preflight
from benchmarks.local_ai import CASES, POLICY
from local_model import MAX_INPUT_BYTES
from evidence import invalid_evidence
from model_config import Runtime, select_runtime


def _strict_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON keys are not accepted")
            result[key] = value
        return result

    def finite(_):
        raise ValueError("Non-finite JSON numbers are not accepted")
    return json.loads(text, object_pairs_hook=unique, parse_constant=finite)


def parse_model_output(text):
    """Accept only meaning-identical duplicate model keys; input parsers stay strict."""
    duplicate_count = 0
    def identical(left, right):
        if type(left) is not type(right):
            return False
        if isinstance(left, dict):
            return left.keys() == right.keys() and all(identical(left[key], right[key]) for key in left)
        if isinstance(left, list):
            return len(left) == len(right) and all(identical(a, b) for a, b in zip(left, right))
        return left == right
    def pairs(items):
        nonlocal duplicate_count
        result = {}
        for key, value in items:
            if key in result:
                if not identical(result[key], value):
                    raise ValueError("Conflicting duplicate model-output keys are not accepted")
                duplicate_count += 1
            else:
                result[key] = value
        return result
    def invalid(_):
        raise ValueError("Non-finite model-output numbers are not accepted")
    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            invalid(value)
        return parsed
    value = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid, parse_float=finite_float)
    warnings = ([{"code": "identical_duplicate_keys", "count": duplicate_count,
                  "message": "Repeated model-output keys had recursively identical values; raw output is retained."}]
                if duplicate_count else [])
    return value, {"status": "parsed", "identical_duplicate_count": duplicate_count, "warnings": warnings}


def _read_json(path):
    with path.open("rb") as source:
        content = source.read(MAX_INPUT_BYTES + 1)
    if len(content) > MAX_INPUT_BYTES:
        raise ValueError("Input file exceeds 64 KiB")
    return _strict_json(content.decode("utf-8"))


def review_case(case, policy, *, facts_provenance, output_dir, generator=None, runtime=None):
    """Save one immutable run; supplied facts are never promoted to PayPal proof.

    ``generator`` is an explicit offline test seam. Its invocations never count
    as actual model requests, regardless of the metadata it returns.
    """
    if facts_provenance not in ("synthetic", "supplied"):
        raise ValueError("facts_provenance must be synthetic or supplied")
    selected_runtime = select_runtime() if runtime is None else runtime
    if not isinstance(selected_runtime, Runtime):
        raise ValueError("An explicit supported model runtime is required")
    case, policy = deepcopy(case), deepcopy(policy)
    encoded_inputs = json.dumps({"case": case, "policy": policy}, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(encoded_inputs) > MAX_INPUT_BYTES:
        raise ValueError("Case and policy exceed the 64 KiB input limit")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    version = policy.get("version") if isinstance(policy, dict) else None
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None,
        "status": "REVIEW_INCOMPLETE", "facts_provenance": facts_provenance,
        "facts_verified_by_this_run": False, "payment_authorized": False, "paypal_calls": 0,
        "runtime_provenance": "injected_test_generator" if generator is not None else selected_runtime.provenance,
        "case": case, "policy": policy, "policy_version": version,
        "inputs_sha256": hashlib.sha256(encoded_inputs).hexdigest(),
        "preflight": None, "generator_invocations": 0, "model_request_attempts": 0,
        "model_calls": 0, "model_call_definition": "Attempted actual requests to the selected model provider; completion is recorded separately",
        "completed_model_responses": 0, "model": None, "model_response_text": None,
        "assessment": None, "accepted_recommendation": None, "error": None,
        "evidence": invalid_evidence([]),
        "model_validation": {"status": "not_parsed", "identical_duplicate_count": None, "warnings": []},
    }

    def save():
        temporary = output / "report.json.tmp"
        temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(output / "report.json")

    save()
    try:
        report["preflight"] = preflight(case, policy, policy_version=version)
        if not report["preflight"]["valid"]:
            report["error"] = "Input facts or policy are incomplete or invalid; no model request was made."
            return report
        client = generator if generator is not None else selected_runtime.generate
        report["generator_invocations"] = 1
        result = client(deepcopy(case), deepcopy(policy))
        if not isinstance(result, dict):
            raise ValueError("Generator returned an invalid result")
        if generator is None:
            report["runtime_provenance"] = result.get("runtime_provenance", selected_runtime.provenance)
            report["model_request_attempts"] = int(result.get("model_request_attempted") is True)
            report["model_calls"] = report["model_request_attempts"]
            report["completed_model_responses"] = int(result.get("completed_model_response") is True)
        report["model"] = {key: result.get(key) for key in
                           ("model", "digest", "endpoint", "runtime_version", "runtime", "thinking_present", "requested_num_threads",
                            "elapsed_seconds", "deadline_seconds", "model_request_attempted", "completed_model_response",
                            "provider", "http_status", "finish_reason", "response_id",
                            "requested_reasoning_effort", "requested_max_completion_tokens")}
        content = result.get("response_text")
        report["model_response_text"] = content
        if result.get("error"):
            report["error"] = str(result["error"])
            return report
        try:
            if not isinstance(content, str):
                raise ValueError("Missing model response text")
            answer, report["model_validation"] = parse_model_output(content)
        except (ValueError, TypeError, RecursionError):
            report["error"] = "Model output was not complete strict JSON; original response retained."
            report["model_validation"] = {"status": "invalid", "identical_duplicate_count": None, "warnings": []}
            answer = content
        report["assessment"] = assess(case, policy, answer, policy_version=version)
        report["evidence"] = deepcopy(report["assessment"]["evidence"])
        if report["error"] is None:
            report["status"] = report["assessment"]["status"]
            report["accepted_recommendation"] = report["assessment"]["accepted_recommendation"]
    except TimeoutError:
        report["error"] = "Model operation timed out; no retry was made."
    except Exception as exc:
        report["error"] = f"Review failed ({type(exc).__name__}); no exception details retained."
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--example", choices=[case["id"] for case in CASES], help="An explicitly synthetic benchmark example")
    inputs.add_argument("--case", type=Path, help="Supplied case JSON; this flow does not verify its transaction provenance")
    parser.add_argument("--policy", type=Path, help="Versioned policy JSON required with --case")
    parser.add_argument("--output-dir", type=Path, required=True, help="A NEW directory for the complete review record")
    args = parser.parse_args(argv)
    if bool(args.case) != bool(args.policy):
        parser.error("Use --case with --policy, or --example without --policy")
    try:
        if args.example:
            selected = next(case for case in CASES if case["id"] == args.example)
            case, policy, provenance = selected["input"], POLICY, "synthetic"
        else:
            case, policy, provenance = _read_json(args.case), _read_json(args.policy), "supplied"
        report = review_case(case, policy, facts_provenance=provenance, output_dir=args.output_dir)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps({"status": report["status"], "facts_provenance": report["facts_provenance"],
                      "model_calls": report["model_calls"], "completed_model_responses": report["completed_model_responses"],
                      "payment_authorized": False, "paypal_calls": 0,
                      "report": str(args.output_dir.resolve() / "report.json")}))
    return 0 if report["status"] in ("REVIEW_READY", "REVIEW_NEEDS_INFORMATION") else 1


if __name__ == "__main__":
    raise SystemExit(main())
