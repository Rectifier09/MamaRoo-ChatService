"""
Builds (or rebuilds) the shared knowledge base in Postgres from a folder of files.
Every run is a full rebuild: it truncates kb_chunks (and the semantic cache, since a
content change can invalidate any previously cached answer) and reloads everything.

Usage:
    python ingest.py                       # ingests ./data/knowledge_base
    python ingest.py --path some/folder    # ingest a different folder
"""
import argparse
from pathlib import Path

from pypdf import PdfReader

import config
import db
from embeddings import embed_texts

SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf"}


def extract_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def load_file_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return extract_pdf_text(path)
    return path.read_text(encoding="utf-8", errors="ignore")


def chunk_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Split text into overlapping chunks, preferring to break on paragraph boundaries."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""

    def flush():
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    for para in paragraphs:
        if len(para) > chunk_size:
            flush()
            start = 0
            while start < len(para):
                end = start + chunk_size
                chunks.append(para[start:end])
                start = end - chunk_overlap
            continue

        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= chunk_size:
            current = candidate
        else:
            flush()
            current = para
    flush()
    return chunks


def ingest_folder(folder: str) -> None:
    folder_path = Path(folder)
    if not folder_path.exists():
        raise FileNotFoundError(f"Knowledge base folder not found: {folder_path.resolve()}")

    files = sorted(p for p in folder_path.rglob("*") if p.suffix.lower() in SUPPORTED_EXTENSIONS)
    if not files:
        print(f"No .txt/.md/.pdf files found under {folder_path.resolve()}")
        return

    rows = []  # (source, chunk_index, content)
    for f in files:
        text = load_file_text(f)
        if not text.strip():
            print(f"  (skipping {f.name} — no extractable text)")
            continue
        chunks = chunk_text(text, config.CHUNK_SIZE, config.CHUNK_OVERLAP)
        for i, chunk in enumerate(chunks):
            rows.append((str(f.relative_to(folder_path)), i, chunk))
        print(f"  {f.name}: {len(chunks)} chunk(s)")

    if not rows:
        print("Nothing to ingest.")
        return

    print(f"Embedding {len(rows)} chunks with '{config.EMBEDDING_MODEL_NAME}'...")
    embeddings = embed_texts([r[2] for r in rows])

    print("Rebuilding kb_chunks...")
    db.execute("TRUNCATE kb_chunks")
    for (source, chunk_index, content), embedding in zip(rows, embeddings):
        db.execute(
            "INSERT INTO kb_chunks (source, chunk_index, content, embedding) VALUES (%s, %s, %s, %s)",
            (source, chunk_index, content, embedding),
        )

    print(f"Done. Indexed {len(rows)} chunks from {len(files)} file(s).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="./data/knowledge_base", help="Folder of .txt/.md/.pdf files")
    args = parser.parse_args()
    ingest_folder(args.path)
