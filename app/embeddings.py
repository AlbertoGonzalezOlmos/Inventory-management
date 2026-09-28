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
import threading
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

# Guards the retry hand-off and the publication of _model. Without it the
# cooldown retry is a thundering herd: `_model_failed` used to be cleared before
# the attempt, so every request that arrived after the cooldown started its own
# ~80 MB download/load in its own request thread (PLAN-v2 §8.2 N6).
_lock = threading.Lock()
_retry_thread: threading.Thread | None = None
# Set under _lock when a retry is scheduled, cleared by the worker's finally.
# A separate flag (rather than `_retry_thread.is_alive()`) is load-bearing:
# is_alive() is False between Thread(...) and start(), so a caller landing in
# that window would schedule a second attempt (measured: 3-4 attempts from 20
# concurrent callers, flakily).
_retry_scheduled = False

# Floor for the retry interval: a misconfigured HCRM_EMBED_RETRY_SECONDS=0 must
# not turn a failing model into a hot loop of download attempts.
MIN_RETRY_SECONDS = 1.0


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


def _attempt_load() -> bool:
    """One model load attempt. Returns True on success.

    Raises only when STRICT is set (startup semantics); otherwise the failure is
    recorded and logged. Safe to call from the retry thread.
    """
    global _model, _model_failed, _model_failed_at
    try:
        loaded = _load_model()
    except Exception as exc:
        with _lock:
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
        return False
    # Publish in one step: readers see either the old None or the whole model.
    with _lock:
        _model = loaded
        _model_failed = False
        _model_failed_at = None
    logger.info("Embedding model loaded (retry succeeded).")
    return True


def _retry_worker() -> None:
    global _retry_thread, _retry_scheduled
    try:
        _attempt_load()
    except Exception:  # STRICT only aborts startup; a background retry must not die loudly
        logger.warning("Background embedding retry failed; will retry after the cooldown.")
    finally:
        with _lock:
            _retry_thread = None
            _retry_scheduled = False


def _retry_cooldown() -> float:
    return max(MIN_RETRY_SECONDS, RETRY_SECONDS)


def _cooldown_elapsed() -> bool:
    """True when the last failure is old enough to justify another attempt.

    Callers must hold _lock: deciding eligibility outside the lock and
    scheduling inside it lets a caller that was descheduled between the two
    re-schedule an attempt the moment the previous one finished, collapsing the
    cooldown. Measured: 20 concurrent callers produced 5 sequential load
    attempts instead of 1.
    """
    if not _model_failed:
        return False
    if _model_failed_at is None:
        return True
    return time.monotonic() - _model_failed_at >= _retry_cooldown()


def _schedule_retry() -> bool:
    """Single-flight: start at most one background retry thread per cooldown.

    Returns True if this call started it. Every guard — model already loaded, a
    retry already alive, cooldown not elapsed — is evaluated under the same lock
    as the assignment; `if _retry_thread is None` on its own would be racy.
    """
    global _retry_thread, _retry_scheduled
    with _lock:
        if _model is not None:
            return False
        if _retry_scheduled:
            return False
        if not _cooldown_elapsed():
            return False
        thread = threading.Thread(target=_retry_worker, name="hcrm-embed-retry",
                                  daemon=True)
        _retry_thread = thread
        _retry_scheduled = True
        # Started under the lock: no window in which the flag is set but the
        # thread is not yet alive.
        thread.start()
    return True


def _get_model():
    """The model, or None if it is unavailable.

    Never blocks a request on a retry: after a failure the first caller past the
    cooldown schedules a single daemon thread and everyone returns None
    immediately (keyword search keeps working). The synchronous path remains for
    the very first load, which is what startup's warm_up() validates.
    """
    global _model
    if _model is not None:
        return _model
    with _lock:
        failed = _model_failed
        eligible = _cooldown_elapsed()
    if failed:
        # Never block this request on a download: hand the retry to the single
        # background thread (which re-checks eligibility under the lock) and
        # degrade to keyword search for now.
        if eligible and _schedule_retry():
            logger.info("Embedding model retry scheduled in the background "
                        "(cooldown %.0fs elapsed).", _retry_cooldown())
        return None
    # First load: synchronous, so warm_up() can validate the dimension and
    # STRICT can abort the boot.
    if not _attempt_load():
        return None
    return _model


def model_status() -> str:
    """Current model state for /api/healthz — never triggers a load."""
    if _model is not None:
        return "ready"
    if _model_failed:
        return "failed"
    return "not_loaded"


def retry_in_flight() -> bool:
    """Whether a background retry is scheduled/running (healthz, diagnostics)."""
    with _lock:
        return _retry_scheduled


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
