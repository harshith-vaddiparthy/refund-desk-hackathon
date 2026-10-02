"""Offline Groq boundary/provenance tests. No external requests or real keys."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from benchmarks.local_ai import BASE, CASES, DIGEST, MODEL
import groq_model
from model_config import Runtime, completed_report_matches, select_runtime
from review import review_case
from test_evidence import full_answer


def response():
    return {"id": "chatcmpl-fixture", "model": groq_model.MODEL,
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(full_answer())}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200,
                      "completion_tokens_details": {"reasoning_tokens": 20}}}


class GroqBoundaryTests(unittest.TestCase):
    def test_advertised_english_input_fits_groq_and_oversize_has_specific_context_error(self):
        from benchmarks.local_ai import POLICY
        from evidence import make_sources
        from local_model import ContextLimitError, _payload
        case = deepcopy(CASES[0]['input'])
        case.update(customer_message='word '*800, resolution_note='note '*400)
        case['sources'] = make_sources(case['customer_message'])
        body = groq_model.request_body(case, POLICY)
        context = json.loads(body['messages'][-1]['content'])
        self.assertEqual(context['resolution_note'], case['resolution_note'])
        self.assertEqual(context['sources'], case['sources'])
        with self.assertRaises(ContextLimitError): _payload(case, POLICY)
        case.update(customer_message='界'*4000, resolution_note='界'*2000)
        case['sources'] = make_sources(case['customer_message'])
        with patch('groq_model._credential_key') as key, patch('groq_model._bounded_http') as network:
            result = groq_model.generate(case, POLICY, client_file=self.client)
        key.assert_not_called(); network.assert_not_called()
        self.assertIn('Groq context exceeds', result['error'])
        self.assertFalse(result['model_request_attempted'])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.client = self.base / 'groq.json'
        self.secret = 'fixture-only-not-a-real-api-key'
        self.client.write_text(json.dumps({'provider': 'groq', 'model': groq_model.MODEL,
            'api_key': self.secret, 'verified_plan': 'Free', 'expires_date': '2099-01-01'}))
        self.client.chmod(0o600)

    def test_factory_checks_file_without_http_and_descriptor_or_repr_has_no_secret(self):
        with patch('groq_model._bounded_http') as network:
            runtime = select_runtime('groq', self.client)
        network.assert_not_called()
        self.assertEqual(runtime.public_descriptor(), {'provider': 'groq', 'model': groq_model.MODEL, 'location': 'hosted'})
        self.assertNotIn(self.secret, repr(runtime))
        self.assertNotIn(str(self.client), repr(runtime))
        self.assertEqual(select_runtime().provider, 'local')
        with self.assertRaises(ValueError): select_runtime('local', self.client)
        with self.assertRaises(ValueError): select_runtime('groq')
        self.client.chmod(0o644)
        with self.assertRaises(ValueError): select_runtime('groq', self.client)

    def test_symlink_fifo_and_wrong_provider_metadata_are_rejected(self):
        link = self.base / 'link'; link.symlink_to(self.client)
        with self.assertRaises(ValueError): select_runtime('groq', link)
        fifo = self.base / 'fifo'; os.mkfifo(fifo, 0o600)
        started = time.monotonic()
        with self.assertRaises(ValueError): select_runtime('groq', fifo)
        self.assertLess(time.monotonic() - started, 1)
        value = json.loads(self.client.read_text()); value['model'] = 'unverified-model'
        self.client.write_text(json.dumps(value))
        with self.assertRaises(ValueError): select_runtime('groq', self.client)

    def test_final_text_usage_identity_and_reasoning_omission(self):
        value = groq_model._safe_result(200, {}, response())
        self.assertTrue(value['completed_model_response'])
        self.assertEqual(value['response_id'], 'chatcmpl-fixture')
        self.assertEqual(value['runtime']['usage']['reasoning_tokens'], 20)
        body = response(); body['choices'][0]['message']['reasoning'] = 'PRIVATE_REASONING_FIXTURE'
        rejected = groq_model._safe_result(200, {}, body)
        self.assertFalse(rejected['completed_model_response'])
        self.assertNotIn('PRIVATE_REASONING_FIXTURE', json.dumps(rejected))
        self.assertIsNone(rejected['response_text'])

    def test_truncation_wrong_model_and_api_rejection_are_not_completion(self):
        for field, value in (('model', 'unverified-model'), ('model', None), ('id', None)):
            body = response(); body[field] = value
            self.assertFalse(groq_model._safe_result(200, {}, body)['completed_model_response'])
        body = response(); body['choices'][0]['finish_reason'] = 'length'
        self.assertFalse(groq_model._safe_result(200, {}, body)['completed_model_response'])
        error = groq_model._safe_result(429, {'retry-after': '3'}, {'error': {'code': 'rate_limit_exceeded', 'message': self.secret}})
        self.assertFalse(error['completed_model_response'])
        self.assertNotIn(self.secret, json.dumps(error))
        self.assertEqual(error['runtime']['rate_limits']['retry-after'], '3')

    def test_request_is_explicit_strict_and_reasoning_disabled(self):
        from benchmarks.local_ai import POLICY
        body = groq_model.request_body(CASES[0]['input'], POLICY)
        self.assertEqual(body['model'], groq_model.MODEL)
        self.assertFalse(body['include_reasoning'])
        self.assertFalse(body['stream'])
        self.assertEqual(body['reasoning_effort'], 'medium')
        self.assertEqual(body['temperature'], 0)
        self.assertTrue(body['response_format']['json_schema']['strict'])
        self.assertNotIn('reasoning_format', body)
        self.assertNotIn('tools', body)
        self.assertNotIn('capture_id', body['messages'][-1]['content'])

    def test_native_timeout_kills_worker_from_pool_thread_and_secrets_are_stdin_only(self):
        original = subprocess.Popen
        children, calls = [], []
        def stalled(argv, **kwargs):
            calls.append((argv, kwargs))
            child = original([sys.executable, '-c', 'import sys,time;sys.stdin.buffer.read();time.sleep(20)'], **kwargs)
            children.append(child)
            return child
        started = time.monotonic()
        with patch('groq_model.subprocess.Popen', side_effect=stalled):
            with ThreadPoolExecutor(max_workers=1) as pool:
                value = pool.submit(groq_model._bounded_http, {'api_key': self.secret, 'body': {}}, timeout_s=.1).result(timeout=3)
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(value['completed_model_response'])
        self.assertIn('deadline', value['error'])
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertNotIn(self.secret, repr(calls))
        self.assertEqual(len(calls), 1)

    def test_redirects_blocked_and_worker_error_never_logs_secret(self):
        with self.assertRaises(ValueError): groq_model._NoRedirect().redirect_request(None, None, 302, None, None, 'https://elsewhere.invalid')
        payload = {'api_key': self.secret, 'body': {'model': groq_model.MODEL, 'include_reasoning': False,
            'stream': False, 'temperature': 0, 'reasoning_effort': 'medium', 'max_completion_tokens': 2500,
            'response_format': {'json_schema': {'strict': True}}}}
        output = io.StringIO()
        with patch('sys.stdin', io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))):
            with patch('groq_model.urllib.request.build_opener', side_effect=RuntimeError(self.secret)):
                with redirect_stdout(output): groq_model._worker()
        self.assertNotIn(self.secret, output.getvalue())

    def test_route_failure_never_falls_back_to_local(self):
        runtime = select_runtime('groq', self.client)
        with patch('groq_model.generate', return_value={'error': 'fixture rejection'}) as selected:
            with patch('local_model.generate') as local:
                self.assertEqual(runtime.generate({}, {}), {'error': 'fixture rejection'})
        selected.assert_called_once(); local.assert_not_called()

    def test_actual_report_identity_can_be_read_historically_without_any_key(self):
        result = groq_model._safe_result(200, {}, response())
        result.update(provider='groq', endpoint=groq_model.ENDPOINT)
        report = {'error': None, 'generator_invocations': 1, 'model_calls': 1, 'completed_model_responses': 1,
                  'runtime_provenance': 'hosted_groq', 'model': result}
        with patch('groq_model._credential_key', side_effect=AssertionError('No key access')):
            self.assertTrue(completed_report_matches(report))
            self.assertFalse(completed_report_matches(report, Runtime()))
            self.assertTrue(completed_report_matches(report, Runtime('groq')))
        for field, value in (('response_id', None), ('http_status', 400), ('endpoint', 'https://other.invalid'),
                             ('model', 'unverified'), ('digest', 'invented-weight-proof')):
            mutated = deepcopy(report); mutated['model'][field] = value
            self.assertFalse(completed_report_matches(mutated))
        report['runtime_provenance'] = 'injected_test_generator'; report['model_calls'] = 0
        self.assertFalse(completed_report_matches(report))

    def test_groq_configuration_plus_injected_generator_stays_explicitly_test_only(self):
        from benchmarks.local_ai import POLICY
        runtime = select_runtime('groq', self.client)
        result = groq_model._safe_result(200, {}, response())
        result.update(provider='groq', endpoint=groq_model.ENDPOINT, runtime_provenance='hosted_groq', model_request_attempted=True)
        report = review_case(CASES[0]['input'], POLICY, facts_provenance='synthetic', output_dir=self.base/'run',
                             runtime=runtime, generator=lambda *_: result)
        self.assertEqual(report['runtime_provenance'], 'injected_test_generator')
        self.assertEqual(report['model_calls'], 0)
        self.assertFalse(completed_report_matches(report, runtime))


if __name__ == '__main__':
    unittest.main()
