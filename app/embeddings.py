"""Text embeddings for semantic (vector) search over item descriptions.

Uses fastembed (ONNX runtime, no PyTorch) with a small local model, so no
external API keys are required. The model is downloaded on first use and
cached locally. If it cannot be loaded, vector search degrades gracefully.

Vectors are stored in SQLite as little-endian float32 BLOBs, the binary
format used by the sqlite-vector extension.
"""

import logging
import os
import struct
import time

logger = logging.getLogger("hcrm")

MODEL_NAME = os.environ.get("HCRM_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
EMBEDDING_DIM = 384

# When set, a model problem (e.g. dimension mismatch) aborts startup instead
# of degrading to keyword-only search.
STRICT = os.environ.get("HCRM_EMBED_STRICT", "").strip().lower() in ("1", "true", "yes")

# After a load failure, retry at most this often (a transient network blip
# during the first-run model download must not disable semantic search for
# the lifetime of the process).
RETRY_SECONDS = float(os.environ.get("HCRM_EMBED_RETRY_SECONDS", "60"))

_model = None
_model_failed = False
_model_failed_at: float | None = None


class EmbeddingDimensionError(RuntimeError):
    """The configured model produces vectors of the wrong dimension."""


def _load_model():
    """Load and dimension-check the fastembed model (may raise)."""
    from fastembed import TextEmbedding

    logger.info("Loading embedding model %s (first run downloads it)…", MODEL_NAME)
    model = TextEmbedding(model_name=MODEL_NAME)
    # Reject models whose dimension does not match what sqlite-vector
    # is initialised with — otherwise every embedding written would
    # silently corrupt vector search.
    probe = next(model.embed(["embedding dimension probe"]))
    if len(probe) != EMBEDDING_DIM:
        raise EmbeddingDimensionError(
            f"Model {MODEL_NAME} produces {len(probe)}-dim embeddings, "
            f"but the app is configured for {EMBEDDING_DIM} "
            f"(EMBEDDING_DIM / HCRM_EMBED_MODEL mismatch)"
        )
    return model


def _get_model():
    global _model, _model_failed, _model_failed_at
    if _model is not None:
        return _model
    if _model_failed:
        if _model_failed_at is None or time.monotonic() - _model_failed_at < RETRY_SECONDS:
            return None
        logger.info("Retrying embedding model load after %.0fs cooldown…", RETRY_SECONDS)
        _model_failed = False
    try:
        _model = _load_model()
    except Exception as exc:
        _model_failed = True
        _model_failed_at = time.monotonic()
        if STRICT:
            raise
        # Degrade gracefully (keyword search still works) — but a dimension
        # misconfiguration is an operator error, so log it loudly.
        if isinstance(exc, EmbeddingDimensionError):
            logger.error(
                "Vector search DISABLED: %s. Set HCRM_EMBED_STRICT=1 to fail "
                "startup instead of degrading.", exc,
            )
        else:
            logger.warning("Embedding model unavailable, vector search disabled: %s", exc)
        return None
    return _model


def model_status() -> str:
    """Current model state for /api/healthz — never triggers a load."""
    if _model is not None:
        return "ready"
    if _model_failed:
        return "failed"
    return "not_loaded"


def warm_up() -> None:
    """Load and validate the embedding model at startup.

    With HCRM_EMBED_STRICT=1 this raises and aborts the boot on any model
    problem; otherwise problems are logged and vector search is disabled.
    """
    _get_model()


def embed_text(text: str) -> list[float] | None:
    """Return an embedding vector, or None if the model is unavailable."""
    model = _get_model()
    if model is None or not text or not text.strip():
        return None
    try:
        return [float(x) for x in next(model.embed([text.strip()]))]
    except Exception as exc:
        logger.warning("Embedding failed: %s", exc)
        return None


def item_embedding_text(name: str, description: str) -> str:
    """The text used to embed a catalogue item."""
    return f"{name}. {description or ''}".strip()


def vec_to_blob(vec: list[float]) -> bytes:
    """Pack a float vector as the little-endian float32 BLOB sqlite-vector expects."""
    return struct.pack(f"<{len(vec)}f", *vec)


def embed_text_blob(text: str) -> bytes | None:
    """Embedding as a sqlite-vector BLOB, or None if the model is unavailable."""
    vec = embed_text(text)
    return vec_to_blob(vec) if vec is not None else None
