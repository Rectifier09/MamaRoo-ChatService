"""
Run with:
    uvicorn app:app --reload --port 8000
"""
import secrets
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
import db
import rag_engine
import rate_limit
from auth import get_product

app = FastAPI(title="Chatbot-as-a-Service", version="0.2.0")

# CORS here only controls whether a browser lets its JS *read* the response — it is
# not the access-control layer. The actual authorization (valid key + allowed origin)
# happens in auth.get_product on every request.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    end_user_id: str          # a stable id for this end user — real user id, or a
                               # client-generated UUID stored in localStorage
    session_id: Optional[int] = None  # omit to start a new conversation


class ChatResponse(BaseModel):
    answer: str
    sources: List[str]
    session_id: int
    cached: bool


class CreateProductRequest(BaseModel):
    name: str
    allowed_origins: List[str] = []
    rate_limit_per_minute: Optional[int] = None


class CreateProductResponse(BaseModel):
    id: int
    api_key: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, product: dict = Depends(get_product)):
    rate_limit.check(product["api_key"], product["rate_limit_per_minute"])

    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")

    session_id = req.session_id
    if session_id is None:
        row = db.fetchone(
            "INSERT INTO chat_sessions (product_id, end_user_id) VALUES (%s, %s) RETURNING id",
            (product["id"], req.end_user_id),
        )
        session_id = row["id"]

    history_rows = db.fetchall(
        "SELECT role, content FROM chat_messages WHERE session_id = %s ORDER BY id DESC LIMIT %s",
        (session_id, config.MAX_HISTORY_TURNS * 2),
    )
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(history_rows)]

    try:
        answer, sources, cached = rag_engine.generate_answer(req.message, history=history)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    db.execute(
        "INSERT INTO chat_messages (session_id, role, content) VALUES (%s, 'user', %s)",
        (session_id, req.message),
    )
    db.execute(
        "INSERT INTO chat_messages (session_id, role, content) VALUES (%s, 'assistant', %s)",
        (session_id, answer),
    )

    return ChatResponse(answer=answer, sources=sources, session_id=session_id, cached=cached)


@app.post("/admin/products", response_model=CreateProductResponse)
def create_product(req: CreateProductRequest, x_admin_key: str = Header(...)):
    if not config.ADMIN_API_KEY or x_admin_key != config.ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin key")

    new_key = "pk_" + secrets.token_urlsafe(24)
    row = db.fetchone(
        """
        INSERT INTO products (name, api_key, allowed_origins, rate_limit_per_minute)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (
            req.name,
            new_key,
            req.allowed_origins,
            req.rate_limit_per_minute or config.DEFAULT_RATE_LIMIT_PER_MINUTE,
        ),
    )
    return CreateProductResponse(id=row["id"], api_key=new_key)
