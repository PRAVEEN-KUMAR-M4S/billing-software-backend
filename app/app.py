from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import account, auth, bills, insights
from app.services.db import create_db_and_table


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once when the server starts: create tables if they don't exist.
    await create_db_and_table()
    yield

app = FastAPI(title="Bill Reader API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8080",
        "https://bill-scanner-3gvkqddh1-praveens-projects-fdb192bc.vercel.app",
        "https://bill-scanner-app-omega.vercel.app/"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router)
app.include_router(account.router)
app.include_router(bills.router)
app.include_router(insights.router)
