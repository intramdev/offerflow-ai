"""Meta Model API provider over Chat Completions (OpenAI-compatible).

Verified against the official guides on 30 Oct 2026: bearer auth with
`LLM_`-prefixed keys, base URL https://api.meta.ai/v1, chat completions
endpoint, `response_format` json_schema structured output, usage
prompt/completion/total tokens, content at choices[0].message.content, and
the `max_completion_tokens` output cap.

Chat Completions is chosen over Responses deliberately: its parameter
shapes are confirmed verbatim, it is stateless (nothing persists
server-side by default), and single-shot extraction needs no multi-turn
reasoning. Revisit if tool use or reasoning replay is ever required.

Paid dispatch requires, every time: the explicit switch, a well-formed
key, a configured model id, an organization with budget headroom, and a
free in-flight slot. Anything missing refuses without touching the
network. SDK retries are disabled; failures surface loudly and release
their reservation. Nothing here silently falls back to fixtures.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)

logger = logging.getLogger(__name__)

BASE_URL_DEFAULT = "https://api.meta.ai/v1"
KEY_PREFIX = "LLM_"

SYSTEM_PROMPT = (
    "You read supplier spreadsheet rows and copy their visible values into "
    "fields. Copy values exactly as shown; never invent, guess, or compute "
    "missing values. Use an empty string for any value that is absent or "
    "unreadable. Return only the requested JSON."
)

FIELDS = (
    "supplier",
    "brand",
    "model",
    "capacity",
    "color",
    "grade",
    "lock",
    "quantity",
    "price",
    "currency",
    "conditions",
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "fields": {
                        "type": "object",
                        "properties": {name: {"type": "string"} for name in FIELDS},
                        "required": list(FIELDS),
                        "additionalProperties": False,
                    },
                },
                "required": ["index", "fields"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["rows"],
    "additionalProperties": False,
}


class ProviderError(Exception):
    """A paid provider call failed or its output was unusable."""


@dataclass(frozen=True)
class MetaConfig:
    organization_id: int
    api_key: str
    model: str
    base_url: str = BASE_URL_DEFAULT
    timeout_seconds: int = 60
    max_input_tokens: int = 8000
    max_output_tokens: int = 2000
    max_in_flight: int = 1


def _default_client(config: MetaConfig):
    return OpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        max_retries=0,
        timeout=config.timeout_seconds,
    )


class MetaProvider:
    """Paid interpretation with reservation accounting. Same interface as
    the fixture provider; never interchangeable with it implicitly."""

    name = "meta"
    is_simulated = False

    def __init__(self, config: MetaConfig, *, client_factory=None) -> None:
        self._config = config
        self._client_factory = client_factory or _default_client

    def interpret_table(self, payload: dict) -> dict:
        from pipeline.spending import BudgetRefused, reconcile, release, reserve

        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise ProviderError("Unusable payload for paid dispatch.")
        user_content = json.dumps({"rows": rows}, separators=(",", ":"))
        estimated_input = len(user_content) // 4 + len(SYSTEM_PROMPT) // 4
        if estimated_input > self._config.max_input_tokens:
            raise ProviderError("Payload exceeds the input cap; refusing dispatch.")
        estimate = estimated_input + self._config.max_output_tokens
        try:
            reservation = reserve(
                self._config.organization_id, estimate, self._config.max_in_flight
            )
        except BudgetRefused as exc:
            raise ProviderError(f"Budget refused dispatch: {exc}") from exc
        try:
            client = self._client_factory(self._config)
            response = client.chat.completions.create(
                model=self._config.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "supplier_rows",
                        "schema": RESPONSE_SCHEMA,
                        "strict": True,
                    },
                },
                max_completion_tokens=self._config.max_output_tokens,
            )
        except AuthenticationError as exc:
            release(reservation)
            raise ProviderError("Provider rejected the API key.") from exc
        except RateLimitError as exc:
            release(reservation)
            raise ProviderError("Provider rate limited the call; not retried.") from exc
        except APITimeoutError as exc:
            release(reservation)
            raise ProviderError("Provider call timed out; not retried.") from exc
        except (APIConnectionError, APIStatusError) as exc:
            release(reservation)
            status = getattr(exc, "status_code", None)
            raise ProviderError(
                f"Provider error{f' ({status})' if status else ''}; not retried."
            ) from exc
        except Exception as exc:
            release(reservation)
            raise ProviderError(f"Unexpected provider failure: {type(exc).__name__}.") from exc
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        if prompt_tokens is None or completion_tokens is None:
            logger.warning("Provider omitted usage; reconciling the full estimate.")
            actual = estimate
            prompt_tokens = prompt_tokens or 0
            completion_tokens = completion_tokens or 0
        else:
            actual = prompt_tokens + completion_tokens
        reconcile(reservation, actual)
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise ProviderError("Provider returned no content.") from exc
        if not content:
            raise ProviderError("Provider returned empty content.")
        try:
            parsed = json.loads(content)
        except ValueError as exc:
            raise ProviderError("Provider returned invalid JSON.") from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get("rows"), list):
            raise ProviderError("Provider returned an unusable envelope.")
        for item in parsed["rows"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("index"), int)
                or not isinstance(item.get("fields"), dict)
            ):
                raise ProviderError("Provider returned a malformed row.")
        return {
            "simulated": False,
            "provider": "meta",
            "model": self._config.model,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "rows": parsed["rows"],
        }
