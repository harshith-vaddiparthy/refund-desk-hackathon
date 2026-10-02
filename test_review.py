"""Offline tests for the local review path; no HTTP calls or model inference."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from benchmarks.local_ai import CASES, DIGEST, MODEL, POLICY
import local_model
from review import main, parse_model_output, review_case, _strict_json
from test_evidence import full_answer


def good_answer():
    return full_answer()


def fixture_result(answer=None):
    return {"model": MODEL, "digest": DIGEST, "response_text": json.dumps(answer or good_answer()),
            "model_request_attempted": True, "completed_model_response": True,
            "runtime": {"prompt_eval_count": 100, "eval_count": 60}, "elapsed_seconds": 0.01,
            "error": None}


class ReviewFlowTests(unittest.TestCase):
    def test_identical_duplicates_are_model_only_and_conflicts_remain_invalid(self):
        value, metadata = parse_model_output('{"source_ids":["M1","M2"],"source_ids":["M1","M2"]}')
        self.assertEqual(value, {"source_ids": ["M1", "M2"]})
        self.assertEqual(metadata["identical_duplicate_count"], 1)
        self.assertTrue(metadata["warnings"])
        for raw in ('{"x":true,"x":1}', '{"x":["M1","M2"],"x":["M2","M1"]}',
                    '{"x":{"a":1},"x":{"a":2}}', '{"x":NaN}', '{"x":1e999}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError): parse_model_output(raw)
        with self.assertRaises(ValueError): _strict_json('{"x":1,"x":1}')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="refund-desk-review-test-")
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "run"
        self.case = deepcopy(CASES[0]["input"])
        self.policy = deepcopy(POLICY)

    def run_review(self, generator, **options):
        return review_case(self.case, self.policy, facts_provenance=options.get("provenance", "synthetic"),
                           output_dir=self.output, generator=generator)

    def test_missing_payment_facts_and_invalid_policy_stop_before_any_model_invocation(self):
        for case, policy in ((CASES[2]["input"], POLICY), (self.case, {"version": "v1", "clauses": {}})):
            with self.subTest(case=case, policy=policy), tempfile.TemporaryDirectory() as temporary:
                generator = Mock()
                report = review_case(case, policy, facts_provenance="synthetic",
                                     output_dir=Path(temporary) / "run", generator=generator)
                generator.assert_not_called()
                self.assertEqual(report["status"], "REVIEW_INCOMPLETE")
                self.assertEqual(report["generator_invocations"], 0)
                self.assertEqual(report["model_calls"], 0)
                self.assertIsNone(report["model_response_text"])
                self.assertFalse(report["preflight"]["valid"])

    def test_unknown_merchant_facts_reach_analysis_and_complete_as_nonpayable_information_request(self):
        self.case["verified_transaction"].update(item_used=None, request_date=None)
        answer = full_answer(self.case)
        answer.update(recommendation="request_information", missing_information=["item_used", "request_date"],
            citations=[{"id": "P3", "quote": POLICY["clauses"]["P3"]}],
            issues=[{"kind": "missing", "field": field, "source_ids": [], "owner": "merchant",
                     "detail": "A merchant confirmation is missing.", "question": f"Can the merchant confirm {field}?"}
                    for field in ("item_used", "request_date")])
        generator = Mock(return_value=fixture_result(answer))
        report = self.run_review(generator)
        generator.assert_called_once()
        self.assertEqual(report["status"], "REVIEW_NEEDS_INFORMATION", report)
        self.assertEqual(report["evidence"]["validation_status"], "valid")
        self.assertEqual(report["runtime_provenance"], "injected_test_generator")
        self.assertEqual(report["model_calls"], 0)
        self.assertFalse(report["payment_authorized"])
        self.assertIsNone(report["case"]["verified_transaction"]["item_used"])

    def test_provider_answer_reaches_guards_with_explicit_test_provenance(self):
        generator = Mock(return_value=fixture_result())
        report = self.run_review(generator, provenance="supplied")
        self.assertEqual(report["status"], "REVIEW_READY", report)
        self.assertEqual(report["assessment"]["original_model_response"], good_answer())
        self.assertEqual(report["accepted_recommendation"], "refund")
        self.assertEqual(report["runtime_provenance"], "injected_test_generator")
        self.assertEqual(report["generator_invocations"], 1)
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["completed_model_responses"], 0)
        self.assertFalse(report["facts_verified_by_this_run"])
        self.assertFalse(report["payment_authorized"])
        self.assertEqual(report["paypal_calls"], 0)
        generator.assert_called_once_with(self.case, self.policy)
        saved = json.loads((self.output / "report.json").read_text())
        self.assertEqual(saved, report)

    def test_guard_rejection_preserves_actual_answer_without_rewriting_it(self):
        answer = dict(good_answer(), recommendation="decline", missing_information=["purchase_date"])
        report = self.run_review(Mock(return_value=fixture_result(answer)))
        self.assertEqual(report["status"], "REVIEW_INCOMPLETE")
        self.assertIsNone(report["accepted_recommendation"])
        self.assertEqual(report["assessment"]["original_model_response"], answer)
        self.assertEqual(json.loads(report["model_response_text"]), answer)
        self.assertTrue(report["assessment"]["errors"])

    def test_invalid_json_is_preserved_and_never_becomes_accepted_advice(self):
        for content in ('{"recommendation":', '{"recommendation":"refund","recommendation":"decline"}'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                result = dict(fixture_result(), response_text=content)
                report = review_case(self.case, self.policy, facts_provenance="synthetic",
                                     output_dir=Path(temporary) / "run", generator=Mock(return_value=result))
                self.assertEqual(report["status"], "REVIEW_INCOMPLETE")
                self.assertIsNone(report["accepted_recommendation"])
                self.assertEqual(report["model_response_text"], content)
                self.assertEqual(report["assessment"]["original_model_response"], content)
                self.assertTrue(report["error"])

    def test_timeout_is_saved_without_retry_or_claimed_model_completion(self):
        generator = Mock(side_effect=TimeoutError)
        report = self.run_review(generator)
        generator.assert_called_once()
        self.assertEqual(report["status"], "REVIEW_INCOMPLETE")
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["completed_model_responses"], 0)
        saved = json.loads((self.output / "report.json").read_text())
        self.assertIn("timed out", saved["error"])
        self.assertIsNotNone(saved["finished_at"])

    def test_existing_output_is_not_overwritten_and_invalid_provenance_makes_no_run(self):
        self.output.mkdir()
        marker = self.output / "report.json"
        marker.write_text("previous evidence")
        generator = Mock()
        with self.assertRaises(FileExistsError):
            self.run_review(generator)
        self.assertEqual(marker.read_text(), "previous evidence")
        with self.assertRaises(ValueError):
            review_case(self.case, self.policy, facts_provenance="paypal_verified",
                        output_dir=self.output / "invalid", generator=generator)
        self.assertFalse((self.output / "invalid").exists())
        generator.assert_not_called()

    def test_synthetic_cli_passes_case_input_without_benchmark_expected_labels(self):
        result = {"status": "REVIEW_READY", "facts_provenance": "synthetic", "model_calls": 0,
                  "completed_model_responses": 0}
        with patch("review.review_case", return_value=result) as review, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--example", "eligible", "--output-dir", str(self.output)]), 0)
        case, policy = review.call_args.args
        self.assertEqual(set(case), {"verified_transaction", "customer_message"})
        self.assertNotIn("expected", case)
        self.assertEqual(policy, POLICY)
        self.assertEqual(review.call_args.kwargs["facts_provenance"], "synthetic")


class LocalModelBoundaryTests(unittest.TestCase):
    def test_unrelated_loaded_model_prevents_a_chat_attempt(self):
        responses = {"/api/tags": {"models": [{"name": MODEL, "digest": DIGEST}]},
                     "/api/ps": {"models": [{"name": "unrelated-embedding-model"}]}}
        with patch("local_model.local_request", side_effect=lambda path, *a, **kw: responses[path]) as request:
            with patch("sys.stdin", io.StringIO(json.dumps({"case": CASES[0]["input"], "policy": POLICY}))):
                output = io.StringIO()
                with redirect_stdout(output): local_model._worker()
        self.assertEqual([call.args[0] for call in request.call_args_list], ["/api/tags", "/api/ps"])
        self.assertNotIn('"event": "model_request_attempted"', output.getvalue())
        self.assertIn("runtime is busy", output.getvalue())

    def test_metadata_and_request_execute_only_inside_bounded_child(self):
        output = (json.dumps({"event": "model_request_attempted"}) + "\n" +
                  json.dumps({"event": "result", "result": fixture_result()}) + "\n").encode()
        with patch("local_model.local_request") as request, patch("local_model.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, output, b"")
            result = local_model.generate(CASES[0]["input"], POLICY)
        request.assert_not_called()
        self.assertEqual(run.call_args.kwargs["timeout"], 180)
        self.assertEqual(set(run.call_args.kwargs["env"]), {"PATH", "LANG"})
        inputs = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(set(inputs["case"]), {"verified_transaction", "customer_message"})
        self.assertTrue(result["model_request_attempted"])
        self.assertTrue(result["completed_model_response"])

    def test_timeout_distinguishes_metadata_only_from_attempted_chat(self):
        for output, attempted in ((b"", False), (b'{"event":"model_request_attempted"}\n', True)):
            with self.subTest(attempted=attempted), patch("local_model.subprocess.run") as run:
                run.side_effect = subprocess.TimeoutExpired("worker", 90, output=output)
                result = local_model.generate(CASES[0]["input"], POLICY)
            run.assert_called_once()
            self.assertEqual(result["model_request_attempted"], attempted)
            self.assertFalse(result["completed_model_response"])
            self.assertIn("Server-side generation may still be finishing", result["error"])

    def test_worker_rejects_wrong_digest_before_chat(self):
        responses = {"/api/tags": {"models": [{"name": MODEL, "digest": "wrong"}]}}
        with patch("local_model.local_request", side_effect=lambda path, *a, **kw: responses[path]) as request:
            with patch("sys.stdin", io.StringIO(json.dumps({"case": CASES[0]["input"], "policy": POLICY}))):
                output = io.StringIO()
                with redirect_stdout(output):
                    local_model._worker()
        self.assertEqual(request.call_args_list[0].args, ("/api/tags",))
        request.assert_called_once()
        self.assertNotIn('"event": "model_request_attempted"', output.getvalue())
        self.assertIn("Pinned local model is missing", output.getvalue())

    def test_worker_uses_fixed_local_model_and_retains_only_visible_answer(self):
        def respond(path, payload=None, **options):
            if path == "/api/tags":
                return {"models": [{"name": MODEL, "digest": DIGEST}]}
            if path == "/api/ps":
                return {"models": []}
            if path == "/api/version":
                return {"version": "test-runtime"}
            self.assertEqual(path, "/api/chat")
            self.assertEqual(payload["model"], MODEL)
            self.assertEqual(payload["keep_alive"], "0s")
            self.assertEqual(payload["options"]["num_thread"], 8)
            self.assertFalse(payload["think"])
            context = json.loads(payload["messages"][-1]["content"])
            self.assertEqual(context["elapsed_days"], 20)
            self.assertEqual(context["unknown_merchant_fields"], [])
            self.assertNotIn("capture_id", context["paypal_facts"])
            self.assertNotIn("expected", context)
            return {"model": MODEL, "done": True, "done_reason": "stop",
                    "message": {"content": json.dumps(good_answer()), "thinking": "hidden marker"},
                    "prompt_eval_count": 100, "eval_count": 60}

        with patch("local_model.local_request", side_effect=respond):
            with patch("sys.stdin", io.StringIO(json.dumps({"case": CASES[0]["input"], "policy": POLICY}))):
                output = io.StringIO()
                with redirect_stdout(output):
                    local_model._worker()
        self.assertNotIn("hidden marker", output.getvalue())
        result = json.loads(output.getvalue().splitlines()[-1])["result"]
        self.assertTrue(result["completed_model_response"])
        self.assertEqual(json.loads(result["response_text"]), good_answer())
        self.assertTrue(result["thinking_present"])
        self.assertTrue(result["error"])


if __name__ == "__main__":
    unittest.main()
