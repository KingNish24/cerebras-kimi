"""Knowledge-base store: per-KB directories with parsed docs + chunk index.

Layout under ``data/kbs/<name>/``:

    docs/<file>          original uploads (txt/md/pdf/docx where parseable)
    index.json           {chunks: [{id, doc, page?, text, tokens}]}
    meta.json            {name, created_at, doc_count, ...}

Retrieval is dependency-free BM25-style keyword scoring — good enough for
tutor citations without an embedding service.
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any, List

from ..config import Settings

_CHUNK_SIZE = 1200
_CHUNK_OVERLAP = 150


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9]+", text.lower())


def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    raw = path.read_bytes()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(path)
            return "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception:
            return ""
    if suffix in (".docx",):
        try:
            import docx

            d = docx.Document(str(path))
            return "\n".join(p.text for p in d.paragraphs)
        except Exception:
            return ""
    return raw.decode("utf-8", errors="replace")


def _chunk(text: str) -> list[str]:
    words = text.split()
    chunks, i = [], 0
    while i < len(words):
        chunk = " ".join(words[i : i + _CHUNK_SIZE])
        if chunk.strip():
            chunks.append(chunk)
        i += _CHUNK_SIZE - _CHUNK_OVERLAP
    return chunks or ([text] if text.strip() else [])


class KnowledgeBaseStore:
    def __init__(self, settings: Settings) -> None:
        self._root = settings.data_dir / "kbs"
        self._root.mkdir(parents=True, exist_ok=True)

    def _kb_dir(self, name: str) -> Path:
        safe = "".join(c for c in name if c.isalnum() or c in "-_ ")
        return self._root / safe

    def _meta_path(self, name: str) -> Path:
        return self._kb_dir(name) / "meta.json"

    def _index_path(self, name: str) -> Path:
        return self._kb_dir(name) / "index.json"

    # -- CRUD ---------------------------------------------------------------

    def list(self) -> List[dict[str, Any]]:
        out = []
        for d in sorted(self._root.iterdir()):
            if not d.is_dir():
                continue
            meta = self._read_json(self._meta_path(d.name), {})
            index = self._read_json(self._index_path(d.name), {"chunks": []})
            docs_dir = d / "docs"
            doc_count = len(list(docs_dir.glob("*"))) if docs_dir.exists() else 0
            out.append(
                {
                    "name": meta.get("name", d.name),
                    "id": d.name,
                    "is_default": False,
                    "status": "ready",
                    "path": str(d),
                    "metadata": {
                        "created_at": meta.get("created_at"),
                        "last_updated": meta.get("last_updated"),
                        "last_indexed_count": len(index.get("chunks", [])),
                        "rag_provider": "keyword",
                    },
                    "statistics": {
                        "raw_documents": doc_count,
                        "rag_initialized": bool(index.get("chunks")),
                        "status": "ready",
                    },
                }
            )
        return out

    def create(self, name: str) -> dict[str, Any]:
        kb = self._kb_dir(name)
        (kb / "docs").mkdir(parents=True, exist_ok=True)
        meta = {
            "name": name,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._write_json(self._meta_path(name), meta)
        self._write_json(self._index_path(name), {"chunks": []})
        return {"name": name, "id": name, "status": "ready"}

    def delete(self, name: str) -> bool:
        import shutil

        kb = self._kb_dir(name)
        if kb.exists():
            shutil.rmtree(kb)
            return True
        return False

    def exists(self, name: str) -> bool:
        return self._kb_dir(name).exists()

    # -- documents ------------------------------------------------------------

    def add_document(self, name: str, filename: str, data: bytes) -> dict[str, Any]:
        if not self.exists(name):
            self.create(name)
        kb = self._kb_dir(name)
        safe_name = Path(filename).name or "upload.bin"
        doc_path = kb / "docs" / safe_name
        doc_path.write_bytes(data)
        self.reindex(name)
        return {"filename": safe_name, "size": len(data)}

    def list_documents(self, name: str) -> List[dict[str, Any]]:
        docs = self._kb_dir(name) / "docs"
        if not docs.exists():
            return []
        return [
            {"name": p.name, "size": p.stat().st_size, "suffix": p.suffix}
            for p in sorted(docs.iterdir())
        ]

    def delete_document(self, name: str, filename: str) -> bool:
        path = self._kb_dir(name) / "docs" / Path(filename).name
        if path.exists():
            path.unlink()
            self.reindex(name)
            return True
        return False

    # -- indexing / retrieval ---------------------------------------------------

    def reindex(self, name: str) -> int:
        kb = self._kb_dir(name)
        docs_dir = kb / "docs"
        chunks: list[dict[str, Any]] = []
        if docs_dir.exists():
            for doc in sorted(docs_dir.iterdir()):
                if doc.name.startswith("."):
                    continue
                text = _extract_text(doc)
                if not text.strip():
                    continue
                for i, chunk in enumerate(_chunk(text)):
                    chunks.append(
                        {
                            "id": f"{doc.name}#{i}",
                            "doc": doc.name,
                            "text": chunk,
                            "tokens": _tokenize(chunk),
                        }
                    )
        self._write_json(self._index_path(name), {"chunks": chunks})
        meta = self._read_json(self._meta_path(name), {})
        meta["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._write_json(self._meta_path(name), meta)
        return len(chunks)

    def search(self, name: str, query: str, top_k: int = 5) -> List[dict[str, Any]]:
        index = self._read_json(self._index_path(name), {"chunks": []})
        chunks = index.get("chunks", [])
        if not chunks:
            return []
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        # Document frequency for IDF
        df: dict[str, int] = {}
        for c in chunks:
            for t in set(c["tokens"]):
                df[t] = df.get(t, 0) + 1
        n = len(chunks)
        scored = []
        for c in chunks:
            tf: dict[str, int] = {}
            for t in c["tokens"]:
                tf[t] = tf.get(t, 0) + 1
            score = 0.0
            for qt in q_tokens:
                if qt in tf:
                    idf = math.log(1 + (n - df.get(qt, 0) + 0.5) / (df.get(qt, 0) + 0.5))
                    score += idf * (tf[qt] * 2.2) / (tf[qt] + 1.2)
            if score > 0:
                scored.append((score, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {"id": c["id"], "doc": c["doc"], "text": c["text"], "score": round(s, 3)}
            for s, c in scored[:top_k]
        ]

    def read_source(self, name: str, doc: str) -> str:
        index = self._read_json(self._index_path(name), {"chunks": []})
        parts = [c["text"] for c in index.get("chunks", []) if c.get("doc") == doc]
        return "\n\n".join(parts)[:30000] if parts else ""

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
