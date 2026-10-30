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

Large files are dispatched in chunks (bounded rows and input tokens per
call) so no single call can time out or overflow its caps. Each chunk
reserves and reconciles its own tokens; a failed chunk releases its own
reservation and aborts the whole dispatch loudly — earlier reconciled
chunks stay spent, because those tokens really were used.
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


MAX_ROWS_PER_CALL = 32


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

    def interpret_table(self, payload: dict, on_chunk=None) -> dict:
        """Interpret rows in chunks, reporting each completed call.

        on_chunk(position, total, prompt_tokens, completion_tokens) fires
        after a chunk reconciles, so callers can record spend and progress
        live instead of only at the end.
        """
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise ProviderError("Unusable payload for paid dispatch.")
        if not rows:
            return {
                "simulated": False,
                "provider": "meta",
                "model": self._config.model,
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
                "chunk_usages": [],
                "rows": [],
            }
        chunks = self._split_chunks(rows)
        client = self._client_factory(self._config)
        merged: list = []
        chunk_usages: list = []
        prompt_total = 0
        completion_total = 0
        for position, chunk in enumerate(chunks, start=1):
            prompt_tokens, completion_tokens, items = self._dispatch_chunk(
                client, chunk, position, len(chunks)
            )
            if on_chunk is not None:
                on_chunk(
                    position, len(chunks), prompt_tokens, completion_tokens
                )
            prompt_total += prompt_tokens
            completion_total += completion_tokens
            chunk_usages.append(
                {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                }
            )
            merged.extend(items)
        return {
            "simulated": False,
            "provider": "meta",
            "model": self._config.model,
            "usage": {
                "prompt_tokens": prompt_total,
                "completion_tokens": completion_total,
                "total_tokens": prompt_total + completion_total,
            },
            "chunk_usages": chunk_usages,
            "rows": merged,
        }

    def _split_chunks(self, rows: list) -> list:
        """Group rows so every call fits the input cap and a row bound."""
        chunks: list = []
        current: list = []
        current_tokens = len(SYSTEM_PROMPT) // 4
        for row in rows:
            row_tokens = len(json.dumps(row, separators=(",", ":"))) // 4 + 1
            if row_tokens + len(SYSTEM_PROMPT) // 4 > self._config.max_input_tokens:
                raise ProviderError(
                    "A single row exceeds the input cap; refusing dispatch."
                )
            if (
                current
                and (
                    len(current) >= MAX_ROWS_PER_CALL
                    or current_tokens + row_tokens > self._config.max_input_tokens
                )
            ):
                chunks.append(current)
                current = []
                current_tokens = len(SYSTEM_PROMPT) // 4
            current.append(row)
            current_tokens += row_tokens
        if current:
            chunks.append(current)
        return chunks

    def _dispatch_chunk(self, client, chunk: list, position: int, total: int) -> tuple:
        from pipeline.spending import BudgetRefused, reconcile, release, reserve

        user_content = json.dumps({"rows": chunk}, separators=(",", ":"))
        estimated_input = len(user_content) // 4 + len(SYSTEM_PROMPT) // 4
        if estimated_input > self._config.max_input_tokens:
            raise ProviderError(
                f"Chunk {position}/{total} exceeds the input cap; refusing dispatch."
            )
        # The model copies visible values, so output tracks input size.
        estimate = estimated_input + min(
            self._config.max_output_tokens, estimated_input + 256
        )
        try:
            reservation = reserve(
                self._config.organization_id, estimate, self._config.max_in_flight
            )
        except BudgetRefused as exc:
            raise ProviderError(
                f"Budget refused dispatch at chunk {position}/{total}: {exc}"
            ) from exc
        try:
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
            raise ProviderError(
                f"Provider rate limited chunk {position}/{total}; not retried."
            ) from exc
        except APITimeoutError as exc:
            release(reservation)
            raise ProviderError(
                f"Provider call timed out at chunk {position}/{total}; not retried."
            ) from exc
        except (APIConnectionError, APIStatusError) as exc:
            release(reservation)
            status = getattr(exc, "status_code", None)
            raise ProviderError(
                f"Provider error{f' ({status})' if status else ''} "
                f"at chunk {position}/{total}; not retried."
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
            raise ProviderError(
                f"Chunk {position}/{total} returned no content."
            ) from exc
        if not content:
            raise ProviderError(f"Chunk {position}/{total} returned empty content.")
        try:
            parsed = json.loads(content)
        except ValueError as exc:
            raise ProviderError(
                f"Chunk {position}/{total} returned invalid JSON."
            ) from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get("rows"), list):
            raise ProviderError(
                f"Chunk {position}/{total} returned an unusable envelope."
            )
        for item in parsed["rows"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("index"), int)
                or not isinstance(item.get("fields"), dict)
            ):
                raise ProviderError(
                    f"Chunk {position}/{total} returned a malformed row."
                )
        return prompt_tokens, completion_tokens, parsed["rows"]
