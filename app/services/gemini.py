"""The Gemini client, and the two answers every call needs.

Both AI features (reading a bill, writing a monthly tip) need the same things:
a client built lazily so the app starts without a key, the ordered list of
models to try, and a way to tell a busy model (503) from spent quota (429)
from a real failure (502). Keeping that here means a new AI feature is a call,
not another copy of the error handling.
"""

from google import genai

from app.config import GEMINI_API_KEY, GEMINI_FALLBACK_MODELS, GEMINI_MODEL


class GeminiError(Exception):
    """Anything that stopped us getting a usable structured reply.

    `status_code` is what a route should answer with when there is no fallback:
    a busy model (503, retry shortly) differs from spent quota (429, wait for
    the reset) and from a real problem (502).
    """

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


# Built on first use, not at import: the app must start without a key so that
# auth, /docs and the checks keep working while the key is being set up.
_client: genai.Client | None = None


def get_client() -> genai.Client:
    global _client
    if not GEMINI_API_KEY:
        raise GeminiError(
            "GEMINI_API_KEY is not set. Add it to .env (get one at "
            "https://aistudio.google.com/apikey) and restart the server."
        )
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


def candidate_models() -> list[str]:
    """The primary model first, then the configured fallbacks, no duplicates."""
    models: list[str] = []
    for name in [GEMINI_MODEL, *GEMINI_FALLBACK_MODELS]:
        name = (name or "").strip()
        if name and name not in models:
            models.append(name)
    return models


def upstream_code(exc: Exception) -> int | None:
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
