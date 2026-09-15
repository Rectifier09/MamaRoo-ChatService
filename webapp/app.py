"""
Password-protected test webapp for MamaRoo-ChatService. A thin proxy: the
browser only ever talks to this service, never to the real chat service --
the real product API key lives only in this process's environment,
injected server-side into the proxied request. See
docs/superpowers/specs/2026-09-15-test-webapp-design.md.

Run with:
    uvicorn app:app --reload --port 8001
"""
from typing import Optional

import httpx
from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import config
from auth import check_auth

app = FastAPI(title="MamaRoo-ChatService Test Webapp")


class ChatRequest(BaseModel):
    message: str
    end_user_id: str
    session_id: Optional[int] = None


@app.get("/health")
def health():
    # Deliberately dependency-free, matching the main service's own
    # pattern -- Railway's healthcheck must succeed independent of login
    # credentials or the real chat service's availability.
    return {"status": "ok"}


@app.get("/", dependencies=[Depends(check_auth)])
def index():
    return FileResponse("static/index.html")


@app.post("/chat", dependencies=[Depends(check_auth)])
async def chat(req: ChatRequest):
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{config.CHATSERVICE_URL}/chat",
            headers={"X-API-Key": config.CHATSERVICE_API_KEY},
            json=req.model_dump(),
        )
    return JSONResponse(content=resp.json(), status_code=resp.status_code)
