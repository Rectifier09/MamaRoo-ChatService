"""
Password-protected test webapp for MamaRoo-ChatService. A thin proxy: the
browser only ever talks to this service, never to the real chat service --
the real product API key lives only in this process's environment,
injected server-side into the proxied request. See
docs/superpowers/specs/2026-09-15-test-webapp-design.md.

Run with:
    uvicorn app:app --reload --port 8001
"""
import json
from pathlib import Path
from typing import Optional

import httpx
from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import config
from auth import check_auth

app = FastAPI(
    title="MamaRoo-ChatService Test Webapp",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


class ChatRequest(BaseModel):
    message: str = Field(..., max_length=4000)
    end_user_id: str = Field(..., max_length=200)
    session_id: Optional[int] = None


@app.get("/health")
def health():
    # Deliberately dependency-free, matching the main service's own
    # pattern -- Railway's healthcheck must succeed independent of login
    # credentials or the real chat service's availability.
    return {"status": "ok"}


@app.get("/", dependencies=[Depends(check_auth)])
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/chat", dependencies=[Depends(check_auth)])
async def chat(req: ChatRequest):
    if not config.CHATSERVICE_URL or not config.CHATSERVICE_API_KEY:
        return JSONResponse(
            content={"detail": "Webapp is not configured (missing upstream URL or API key)"},
            status_code=503,
        )

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{config.CHATSERVICE_URL}/chat",
                headers={"X-API-Key": config.CHATSERVICE_API_KEY},
                json=req.model_dump(),
            )
    except httpx.TimeoutException:
        return JSONResponse(
            content={"detail": "Upstream chat service timed out"},
            status_code=504,
        )
    except httpx.RequestError:
        return JSONResponse(
            content={"detail": "Could not reach the upstream chat service"},
            status_code=502,
        )

    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(
            content={"detail": "Upstream returned a non-JSON response"},
            status_code=resp.status_code,
        )

    return JSONResponse(content=body, status_code=resp.status_code)
