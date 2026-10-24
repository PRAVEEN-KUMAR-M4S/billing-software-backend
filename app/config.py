"""All settings live here. Values come from the .env file (or real env vars)."""

import os

from dotenv import load_dotenv

load_dotenv()

# In production, put a long random value in .env — never commit .env.
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
ALGORITHM = "HS256"
# Short-lived on purpose: if it leaks, it's useless soon.
# The refresh token is what actually keeps a user logged in.
ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 30

# --- Bill extraction (Gemini) ---------------------------------------------
# The SDK reads nothing on its own: we pass the key explicitly so a missing
# key fails at request time with a clear message instead of at import time.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# Never hardcode a model name in code — model ids change. Override in .env.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

# One upload, one call. 5 MB is plenty for a phone photo of a bill.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png"}

# Money is stored in whole minor units (paise). Display divides by 100.
ALLOWED_CURRENCIES = {"INR"}
DEFAULT_CURRENCY = "INR"
# Rounding on a printed bill is normal; anything bigger is a real mismatch.
TOTAL_TOLERANCE_MINOR = 2

# The prompt is versioned so an extraction can be traced back to the text
# that produced it (the v1 score only means something against v1).
PROMPT_VERSION = "v1"
EXTRACTION_PROMPT = """
Extract this receipt or bill into the provided schema.

- shop: the business name printed on the bill.
- date: the bill date as ISO YYYY-MM-DD. Never guess DD/MM vs MM/DD and never
  invent a year; if it cannot be read unambiguously, use null.
- currency: the ISO 4217 code (for example INR). Use INR unless the bill says otherwise.
- money: whole minor units (paise for INR) as integers. No floats, no decimals,
  no currency symbols. Rs 12.50 is 1250.
- items: one entry per line item, with the printed quantity and price.
- If a value is unreadable, blurry or missing, set it to null and explain why
  in notes.
- Never guess: an honest null is better than a wrong number.
""".strip()
