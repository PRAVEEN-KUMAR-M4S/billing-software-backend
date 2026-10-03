from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routers import auth
from app.services.db import create_db_and_table


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once when the server starts: create tables if they don't exist.
    await create_db_and_table()
    yield


app = FastAPI(title="Bill Reader API", lifespan=lifespan)
app.include_router(auth.router)
