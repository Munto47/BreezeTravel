"""A public, display-safe source citation attached to a RAG answer."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl


class Citation(BaseModel):
    source_id: str = Field(description="Stable note/chunk identifier")
    title: str
    url: Optional[HttpUrl] = None
    excerpt: str = Field(max_length=320)
    score: float
    retrieval_sources: list[str] = Field(default_factory=list)
    published_at: Optional[datetime] = None
    retrieved_at: Optional[datetime] = None
    license: Optional[str] = None
    revision: Optional[str] = None
    attribution: Optional[str] = None
    corpus_kind: str = "synthetic"
