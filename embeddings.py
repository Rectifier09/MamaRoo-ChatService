"""Local embedding model, shared by ingestion and query-time retrieval."""
from sentence_transformers import SentenceTransformer

import config

_model = None


def get_embedder() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(config.EMBEDDING_MODEL_NAME)
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    return get_embedder().encode(texts, show_progress_bar=False, convert_to_numpy=True).tolist()
