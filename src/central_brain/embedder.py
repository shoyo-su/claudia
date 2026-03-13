"""VoyageAI embedding wrapper with graceful degradation."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


class VoyageEmbedder:
    """Thin wrapper around VoyageAI SDK for embedding text."""

    def __init__(self, api_key: str | None = None, model: str = "voyage-3.5"):
        import voyageai

        key = api_key or os.environ.get("VOYAGE_API_KEY")
        if not key:
            raise ValueError("VOYAGE_API_KEY not set")
        self.client = voyageai.Client(api_key=key)
        self.model = model
        self.dims = 1024  # voyage-3.5 default

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed up to 128 texts at once."""
        result = self.client.embed(texts, model=self.model, truncation=True)
        return result.embeddings

    def embed_single(self, text: str) -> list[float]:
        return self.embed([text])[0]


def get_embedder(api_key: str | None = None) -> VoyageEmbedder | None:
    """Try to create an embedder, return None if unavailable."""
    try:
        return VoyageEmbedder(api_key=api_key)
    except Exception as e:
        logger.debug("Embedder unavailable: %s", e)
        return None
