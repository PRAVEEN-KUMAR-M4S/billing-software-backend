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
