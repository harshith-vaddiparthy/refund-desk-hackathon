"""Explicit runtime selection for the two implemented model adapters."""
from dataclasses import dataclass, field
from pathlib import Path
import re

from benchmarks.local_ai import BASE as LOCAL_ENDPOINT, DIGEST, MODEL as LOCAL_MODEL

GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"


@dataclass(frozen=True)
class Runtime:
    provider: str = "local"
    client_file: Path | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.provider not in ("local", "groq"):
            raise ValueError("Unsupported AI provider")
        if self.provider == "local" and self.client_file is not None:
            raise ValueError("Local inference does not use a Groq credential file")

    @property
    def provenance(self):
        return "local_ollama" if self.provider == "local" else "hosted_groq"

    def public_descriptor(self):
        return {"provider": "ollama" if self.provider == "local" else "groq",
                "model": LOCAL_MODEL if self.provider == "local" else GROQ_MODEL,
                "location": "local" if self.provider == "local" else "hosted"}

    def generate(self, case, policy):
        if self.provider == "local":
            from local_model import generate
            return dict(generate(case, policy), provider="ollama")
        from groq_model import generate
        return generate(case, policy, client_file=self.client_file)


def select_runtime(provider="local", client_file=None):
    if provider == "local":
        return Runtime(provider, client_file)
    if provider != "groq" or client_file is None:
        raise ValueError("Groq must be explicitly selected with its credential file")
    from groq_model import validate_client_file
    return Runtime("groq", validate_client_file(client_file))


def completed_report_matches(report, runtime=None):
    """Validate actual supported provenance; never read a key for historical reports."""
    if not isinstance(report, dict) or report.get("error") is not None:
        return False
    if any(type(report.get(name)) is not int or report[name] != 1 for name in
           ("generator_invocations", "model_calls", "completed_model_responses")):
        return False
    provenance = report.get("runtime_provenance")
    if provenance not in ("local_ollama", "hosted_groq"):
        return False
    if runtime is not None and (not isinstance(runtime, Runtime) or runtime.provenance != provenance):
        return False
    model = report.get("model")
    if not isinstance(model, dict) or model.get("completed_model_response") is not True:
        return False
    if provenance == "local_ollama":
        return (model.get("model") == LOCAL_MODEL and model.get("digest") == DIGEST
                and model.get("endpoint") == LOCAL_ENDPOINT and model.get("provider") in (None, "ollama"))
    response_id = model.get("response_id")
    return (model.get("provider") == "groq" and model.get("model") == GROQ_MODEL
            and model.get("digest") is None
            and model.get("endpoint") == GROQ_ENDPOINT and model.get("http_status") == 200
            and model.get("finish_reason") == "stop" and isinstance(response_id, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", response_id) is not None)
