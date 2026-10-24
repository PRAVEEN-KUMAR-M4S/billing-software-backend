"""The only file that talks to Gemini. One call, one bill.

Gemini has no Claude-style forced tool call; the equivalent is
`response_mime_type="application/json"` + `response_schema`, which constrains
the reply to `BillDraft`. So the reply arrives as a parsed Pydantic object
instead of text we have to parse.

The reply is still untrusted input: it is a draft until `check_bill` and a
human agree with it.

Never store the photo: bills are personal data, so the bytes live only for
the duration of this call.
"""

from google import genai
from google.genai import types

from app.config import EXTRACTION_PROMPT, GEMINI_API_KEY, GEMINI_MODEL
from app.schemas.bill import BillDraft


class ExtractionError(Exception):
    """Anything that stopped us producing a usable draft (mapped to a 502)."""


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


def extract_bill(photo_bytes: bytes, mime_type: str) -> BillDraft:
    """Send one photo, get one draft. Blocking — call it from a thread."""
    config = types.GenerateContentConfig(
        system_instruction=EXTRACTION_PROMPT,
        response_mime_type="application/json",
        response_schema=BillDraft,
        temperature=0,
    )

    try:
        response = _get_client().models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                "Extract this bill.",
                types.Part.from_bytes(data=photo_bytes, mime_type=mime_type),
            ],
            config=config,
        )
    except ExtractionError:
        raise
    except Exception as exc:  # network, auth, quota, model name, ...
        raise ExtractionError(f"Gemini request failed: {exc}") from exc

    # `.parsed` is the documented path; the JSON fallback covers an SDK that
    # returns text instead (or an older schema-conversion path).
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
