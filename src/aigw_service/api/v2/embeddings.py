"""Lazy Cloud.ru embeddings and the durable embedding identity."""

import hashlib
import json
from functools import lru_cache
from pathlib import Path

import numpy as np
from langchain_core.embeddings import Embeddings
from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class RetrievalSettings(BaseSettings):
    """Settings evaluated only when a V2 catalog operation needs them."""

    model_config = SettingsConfigDict(env_prefix="V2_", extra="ignore")
    embedding_url: str = "https://foundation-models.api.cloud.ru/v1"
    embedding_model: str = "ai-sage/Giga-Embeddings-instruct-3B"
    embedding_revision: str = "1"
    api_key: SecretStr = Field(default=SecretStr(""), validation_alias=AliasChoices("EMBEDDING_API_KEY", "API_KEY"))
    embedding_batch_size: int = Field(default=32, gt=0)
    embedding_timeout: float = Field(default=60, gt=0)
    embedding_retries: int = Field(default=2, ge=0)
    index_dir: Path = Path("data/faiss")
    retrieval_k: int = Field(default=10, gt=0)

    def fingerprint(self) -> str:
        """Identify vector compatibility without storing secrets or retrieval k."""
        identity = [self.embedding_url.rstrip("/"), self.embedding_model, self.embedding_revision, "raw-float-l2-v1"]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def validate_vectors(vectors: list[list[float]], count: int, dimension: int | None = None) -> np.ndarray:
    """Reject invalid responses before using or persisting normalized vectors."""
    if count == 0:
        return np.empty((0, dimension or 0), dtype="float32")
    array = np.asarray(vectors, dtype="float32")
    if array.ndim != 2 or array.shape[0] != count or array.shape[1] == 0:
        raise ValueError("Invalid embedding count or shape")
    if dimension is not None and array.shape[1] != dimension:
        raise ValueError("Incompatible embedding dimension")
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    if not np.isfinite(array).all() or not np.isfinite(norms).all() or (norms == 0).any():
        raise ValueError("Embeddings must be finite nonzero vectors")
    return array / norms


class CloudEmbeddings(Embeddings):
    """Send raw strings through LangChain and validate each response batch."""

    def __init__(self, settings: RetrievalSettings) -> None:
        """Construct a credentialed client only on first embedding use."""
        self.settings = settings
        self._client: Embeddings | None = None

    def _get_client(self) -> Embeddings:
        """Initialize the LangChain endpoint without tokenizer preprocessing."""
        if self._client is None:
            from langchain_openai import OpenAIEmbeddings

            if not self.settings.api_key.get_secret_value():
                raise ValueError("EMBEDDING_API_KEY is required for V2 embeddings")
            self._client = OpenAIEmbeddings(
                api_key=self.settings.api_key,
                base_url=self.settings.embedding_url,
                model=self.settings.embedding_model,
                check_embedding_ctx_length=False,
                chunk_size=self.settings.embedding_batch_size,
                request_timeout=self.settings.embedding_timeout,
                max_retries=self.settings.embedding_retries,
                model_kwargs={"encoding_format": "float"},
            )
        return self._client

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed nonempty batches; normalized vectors share one dimension."""
        if not texts:
            return []
        return validate_vectors(self._get_client().embed_documents(texts), len(texts)).tolist()

    def embed_query(self, text: str) -> list[float]:
        """Use the same model and normalization for a single mention."""
        return self.embed_documents([text])[0]


@lru_cache(maxsize=1)
def get_embeddings() -> CloudEmbeddings:
    """Defer all credential and settings access until V2 retrieval is used."""
    return CloudEmbeddings(RetrievalSettings())
