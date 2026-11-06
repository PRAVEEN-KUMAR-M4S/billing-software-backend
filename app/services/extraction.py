"""The file that reads one bill with Gemini: one call, one draft.

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

from google.genai import types

from app.config import EXTRACTION_PROMPT
from app.schemas.bill import BillDraft
from app.services.gemini import (
    GeminiError,
    candidate_models,
    get_client,
    upstream_code,
)

# The route-facing name for the shared error. It is the same class, so a missing
# key raised inside the shared client is caught by `except ExtractionError`.
ExtractionError = GeminiError


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
    client = get_client()
    config = types.GenerateContentConfig(
        system_instruction=EXTRACTION_PROMPT,
        response_mime_type="application/json",
        response_schema=BillDraft,
        temperature=0,
    )

    overloaded: list[str] = []
    for model in candidate_models():
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
            code = upstream_code(exc)
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
