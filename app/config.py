"""All settings live here. Values come from the .env file (or real env vars)."""

import os
from decimal import Decimal

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

# Money in the API is rupees with two decimals (Decimal). The expenses table
# stores the same value as integer paise; only the confirm route converts.
ALLOWED_CURRENCIES = {"INR"}
DEFAULT_CURRENCY = "INR"
# When a bill prints a Round Off line, the maths has to close to the paise.
TOTAL_TOLERANCE = Decimal("0.02")
# When it prints none, the cashier may still have rounded the net total to the
# nearest rupee (127.49 paid as 128.00). One rupee is the most that can hide.
CASHIER_ROUNDING = Decimal("1.00")

# The prompt is versioned so an extraction can be traced back to the text
# that produced it (the v1 score only means something against v1).
PROMPT_VERSION = "v4"
EXTRACTION_PROMPT = """
Extract this receipt or bill into the provided schema.

- shop: the business name printed on the bill.
- date: the bill date as ISO YYYY-MM-DD. Never guess DD/MM vs MM/DD and never
  invent a year; if it cannot be read unambiguously, use null.
- currency: the ISO 4217 code (for example INR). Use INR unless the bill says otherwise.
- money: rupees as decimal numbers with two places — 128.00, 127.49, 12.50.
  Never paise, never an integer multiplied by 100, never a currency symbol.
  Rs 128.00 is 128.00 (not 12800). Rs 127.49 is 127.49 (not 12749).

Which printed number is the money (most mistakes happen here):
- A shop or pharmacy line prints several numbers: MRP, a rate, a discount %, a
  GST %, and finally an amount. amount is the last of them — the money actually
  charged for that whole line.
- MRP, rate and percentages are never amount. Do not apply a discount or GST
  yourself: they are already inside the printed amount.
- qty is information only. Money never multiplies by qty: amount is the whole
  line. A line whose MRP column says 15.00 and whose Amount column says 127.49
  (15% off, 5% GST already applied) is amount 127.49 — never 15.00 and never
  150.00 (MRP x qty).
- Pack size and strength belong to the name. "NONURD LC TAB 10's", "10 TAB"
  and "500 MG" are not quantities.
- A line that prints "2 x 45.00" and then 90.00 has qty 2 and amount 90.00.

- total: the printed bill total / net payable, in rupees.
- round_off: only when the bill actually prints a Round Off, rounding or
  adjustment line — its signed amount (127.49 with "+0.51" gives 0.51, with
  "-0.50" gives -0.50). If the net total is simply rounded to the nearest rupee
  with no such line, leave this null. Never turn that line into an item.
- items: one entry per charged line item, in the order printed.
- Discounts, taxes and the bottom summary (gross, discount, CGST, SGST) are not
  items: what was printed goes in notes.
- Never adjust item amounts to force them to equal the bill total.
- If a value is unreadable, blurry or missing, set it to null and explain why
  in notes.
- Never guess: an honest null is better than a wrong number.
""".strip()
