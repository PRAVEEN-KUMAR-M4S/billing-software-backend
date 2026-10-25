"""The only file that talks to Gemini. One call, one bill.

Gemini has no Claude-style forced tool call; the equivalent is
`response_mime_type="application/json"` + `response_schema`, which constrains
the reply to `BillDraft`. So the reply arrives as a parsed Pydantic object
instead of text we have to parse.

A bill costs one *successful* call. The one failure worth trying again is a
busy model: `503 UNAVAILABLE — high demand` is the model, not your key, so a
second key in the same project would hit the same wall. A second *model* may
have capacity, so each configured model is tried once. Quota (429) is never
retried on another model — that is what the daily reset is for.

The reply is still untrusted input: it is a draft until `check_bill` and a
human agree with it.

Never store the photo: bills are personal data, so the bytes live only for
the duration of this call.
"""

from google import genai
from google.genai import types

from app.config import (
    EXTRACTION_PROMPT,
    GEMINI_API_KEY,
    GEMINI_FALLBACK_MODELS,
    GEMINI_MODEL,
)
from app.schemas.bill import BillDraft


class ExtractionError(Exception):
    """Anything that stopped us producing a usable draft.

    `status_code` is what the route should answer with, so the caller can tell
    a busy model (503, try again shortly) from spent quota (429, wait for the
    reset) from a real problem (502).
    """

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


# Built on first use, not at import: the app must start without a key so that
# auth, /docs and the checks keep working while the key is being set up.
_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if not GEMINI_API_KEY:
        raise ExtractionError(
            "GEMINI_API_KEY is not set. Add it to .env (get one at "
            "https://aistudio.google.com/apikey) and restart the server."
        )
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


def _candidate_models() -> list[str]:
    """The primary model first, then the configured fallbacks, no duplicates."""
    models: list[str] = []
    for name in [GEMINI_MODEL, *GEMINI_FALLBACK_MODELS]:
        name = (name or "").strip()
        if name and name not in models:
            models.append(name)
    return models


def _upstream_code(exc: Exception) -> int | None:
    """The HTTP status behind an SDK exception, whatever the SDK version."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    text = str(exc).upper()
    if "UNAVAILABLE" in text:
        return 503
    if "RESOURCE_EXHAUSTED" in text:
        return 429
    return None


def _draft_from(response) -> BillDraft:
    """`.parsed` is the documented path; the JSON fallback covers an SDK that
    returns text instead (or an older schema-conversion path)."""
    draft = response.parsed
    if isinstance(draft, BillDraft):
        return draft

    raw = response.text
    if not raw:
        raise ExtractionError("Gemini returned no bill data.")
    try:
        return BillDraft.model_validate_json(raw)
    except ValueError as exc:
        raise ExtractionError("Gemini returned something that is not a bill.") from exc


def extract_bill(photo_bytes: bytes, mime_type: str) -> BillDraft:
    """Send one photo, get one draft. Blocking — call it from a thread."""
    client = _get_client()
    config = types.GenerateContentConfig(
        system_instruction=EXTRACTION_PROMPT,
        response_mime_type="application/json",
        response_schema=BillDraft,
        temperature=0,
    )

    overloaded: list[str] = []
    for model in _candidate_models():
        try:
            response = client.models.generate_content(
                model=model,
                contents=[
                    "Extract this bill.",
                    types.Part.from_bytes(data=photo_bytes, mime_type=mime_type),
                ],
                config=config,
            )
        except Exception as exc:  # network, auth, quota, overload, ...
            code = _upstream_code(exc)
            if code == 503:
                # The model has no capacity. Another key would not change that.
                overloaded.append(model)
                continue
            if code == 429:
                raise ExtractionError(
                    f"{model} is rate-limited or out of free-tier quota "
                    "(429 RESOURCE_EXHAUSTED). Nothing was saved. Per-minute limits "
                    "clear within a minute; the daily quota resets at midnight "
                    "Pacific.",
                    status_code=429,
                ) from exc
            raise ExtractionError(f"Gemini request failed: {exc}") from exc
        return _draft_from(response)

    raise ExtractionError(
        "Gemini is overloaded right now (503 UNAVAILABLE — high demand) on "
        f"{', '.join(overloaded)}. Nothing was saved; try again in a minute.",
        status_code=503,
    )
