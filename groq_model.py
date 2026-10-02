"""Explicit Groq Free client. One bounded request, no retries or local fallback."""
from datetime import date
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

MODEL = "openai/gpt-oss-120b"
ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
DEADLINE_SECONDS = 60
REASONING_EFFORT = "medium"
MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_PROMPT_BYTES = 16000
RATE_HEADERS = ("x-ratelimit-limit-requests", "x-ratelimit-limit-tokens",
                "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens",
                "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens", "retry-after")


def _json(text):
    def unique(pairs):
        output = {}
        for key, value in pairs:
            if key in output:
                raise ValueError("Duplicate JSON key")
            output[key] = value
        return output
    def invalid(_):
        raise ValueError("Non-finite JSON value")
    return json.loads(text, object_pairs_hook=unique, parse_constant=invalid)


def _credential_key(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "r") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError
            text = handle.read(8193)
            if len(text) > 8192:
                raise ValueError
        value = _json(text)
        if value.get("provider") != "groq" or value.get("model") != MODEL or value.get("verified_plan") != "Free":
            raise ValueError
        if value.get("expires_date") and date.fromisoformat(value["expires_date"]) < date.today():
            raise ValueError
        key = value.get("api_key")
        if not isinstance(key, str) or not 16 <= len(key) <= 512 or not key.isascii() or any(c.isspace() for c in key):
            raise ValueError
        return key
    except (OSError, ValueError, TypeError, AttributeError):
        raise ValueError("Groq requires a valid, unexpired, owner-only regular Free credential file for the supported model.") from None


def validate_client_file(path):
    path = Path(path).absolute()
    _credential_key(path)
    return path


def request_body(case, policy):
    # One prompt/schema implementation is shared with the existing local adapter.
    from local_model import _payload
    local = _payload(case, policy, max_prompt_bytes=MAX_PROMPT_BYTES)
    return {"model": MODEL, "messages": local["messages"], "stream": False,
            "include_reasoning": False, "reasoning_effort": REASONING_EFFORT, "temperature": 0, "max_completion_tokens": 2500,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "refund_evidence_brief", "strict": True, "schema": local["format"]}}}


def _identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_./:-]{1,160}", value) else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Redirects are blocked")


def _safe_result(status, headers, decoded):
    result = {"http_status": status, "completed_model_response": False, "response_text": None,
              "response_id": _identifier(decoded.get("id")), "finish_reason": None,
              "model": _identifier(decoded.get("model")), "error": None,
              "runtime": {"request_id": _identifier(headers.get("x-request-id")),
                          "rate_limits": {name: str(headers[name])[:100] for name in RATE_HEADERS if headers.get(name)}}}
    usage = decoded.get("usage") or {}
    result["runtime"]["usage"] = {name: value for name, value in usage.items()
        if name in ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_time", "completion_time", "queue_time", "total_time")
        and type(value) in (int, float)}
    details = usage.get("completion_tokens_details") or {}
    if type(details.get("reasoning_tokens")) is int:
        result["runtime"]["usage"]["reasoning_tokens"] = details["reasoning_tokens"]
    if status != 200:
        error = decoded.get("error") or {}
        code = _identifier(error.get("code")) if isinstance(error, dict) else None
        result["error"] = f"Groq request rejected (HTTP {status}" + (f", {code}" if code else "") + "); no retry was made."
        return result
    choices = decoded.get("choices") or []
    choice = choices[0] if len(choices) == 1 else {}
    message = choice.get("message") or {}
    result["finish_reason"] = _identifier(choice.get("finish_reason"))
    result["reasoning_returned_but_discarded"] = bool(message.get("reasoning") or message.get("reasoning_content"))
    content = message.get("content")
    if result["reasoning_returned_but_discarded"]:
        result["error"] = "Provider returned reasoning despite the disabled setting; response content was not retained."
    elif not isinstance(content, str) or len(content.encode("utf-8")) > 20000:
        result["error"] = "Groq did not return a bounded final answer."
    else:
        result["response_text"] = content
        if result["model"] != MODEL or result["finish_reason"] != "stop" or not result["response_id"]:
            result["error"] = "Groq returned an incomplete response or unexpected model identity."
        else:
            result["completed_model_response"] = True
    return result


def _worker():
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError
        payload = _json(raw.decode("utf-8")); key, body = payload["api_key"], payload["body"]
        if (not isinstance(key, str) or "\r" in key or "\n" in key or body.get("model") != MODEL
                or body.get("include_reasoning") is not False or body.get("stream") is not False
                or body.get("temperature") != 0
                or body.get("reasoning_effort") != REASONING_EFFORT or body.get("max_completion_tokens") != 2500
                or body.get("response_format", {}).get("json_schema", {}).get("strict") is not True):
            raise ValueError
        request = urllib.request.Request(ENDPOINT, method="POST", data=json.dumps(body, allow_nan=False).encode(),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": "RefundDesk/2.0"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        print(json.dumps({"event": "model_request_attempted"}), flush=True)
        try:
            response = opener.open(request, timeout=50)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            encoded = response.read(MAX_RESPONSE_BYTES + 1)
            if len(encoded) > MAX_RESPONSE_BYTES:
                raise ValueError
            result = _safe_result(response.status, response.headers, _json(encoded.decode("utf-8")))
        print(json.dumps({"event": "result", "result": result}), flush=True)
    except Exception as error:
        print(json.dumps({"event": "error", "error_type": type(error).__name__}), flush=True)


def _bounded_http(payload, *, timeout_s=DEADLINE_SECONDS):
    encoded = json.dumps(payload, allow_nan=False).encode()
    if len(encoded) > MAX_REQUEST_BYTES or not 0 < timeout_s <= DEADLINE_SECONDS:
        raise ValueError("Invalid bounded Groq request")
    result = {"model_request_attempted": False, "completed_model_response": False,
              "response_text": None, "error": None}
    try:
        completed = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker"],
            input=encoded, capture_output=True, timeout=timeout_s,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"})
        output = completed.stdout
        if completed.returncode:
            result["error"] = "Groq worker failed; no retry was made."
    except subprocess.TimeoutExpired as error:
        output = error.stdout or b""
        result["error"] = "Groq request deadline exceeded; network worker stopped. Server generation may still finish; no retry was made."
    except OSError:
        output = b""
        result["error"] = "Groq worker could not start; no retry was made."
    if len(output) > 128000:
        return dict(result, error="Groq worker output exceeded its bound.")
    for line in output.splitlines():
        try:
            event = _json(line.decode("utf-8"))
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict):
            result["error"] = "Groq worker returned an invalid protocol event."
            continue
        if event.get("event") == "model_request_attempted":
            result["model_request_attempted"] = True
        elif event.get("event") == "result" and not result["error"]:
            if isinstance(event.get("result"), dict):
                result.update(event["result"])
            else:
                result["error"] = "Groq worker returned an invalid result event."
        elif event.get("event") == "error" and not result["error"]:
            result["error"] = "Groq HTTP worker failed; no retry was made."
    if result["completed_model_response"] and not result["model_request_attempted"]:
        result.update(completed_model_response=False, error="Groq result lacked its request-attempt record.")
    if not result["completed_model_response"] and not result["error"]:
        result["error"] = "No completed Groq final answer was received."
    return result


def generate(case, policy, *, client_file):
    from local_model import ContextLimitError
    started = time.monotonic()
    result = {"provider": "groq", "model": MODEL, "digest": None, "endpoint": ENDPOINT,
              "requested_reasoning_effort": REASONING_EFFORT, "requested_max_completion_tokens": 2500,
              "runtime_provenance": "hosted_groq", "model_request_attempted": False,
              "completed_model_response": False, "response_text": None, "runtime": None, "error": None}
    try:
        body = request_body(case, policy)
    except ContextLimitError:
        result["error"] = "Groq context exceeds the 16000-byte prompt budget; shorten the conversation or resolution note. No request was sent."
    except (ValueError, TypeError, KeyError, AttributeError):
        result["error"] = "Groq request context is invalid; no request was sent."
    if result["error"]:
        result.update(elapsed_seconds=round(time.monotonic() - started, 3), deadline_seconds=DEADLINE_SECONDS)
        return result
    try:
        result.update(_bounded_http({"api_key": _credential_key(client_file), "body": body}))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        result["error"] = "Groq credential file or HTTP request is invalid; no fallback was attempted."
    result.update(elapsed_seconds=round(time.monotonic() - started, 3), deadline_seconds=DEADLINE_SECONDS)
    return result


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("Use an explicitly configured Groq runtime through review.py.")
    _worker()
