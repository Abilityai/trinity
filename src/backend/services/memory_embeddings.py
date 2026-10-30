"""Local text embeddings for retrieval memory (trinity-enterprise#1022).

`fastembed` runs a quantised ONNX model on the CPU — no torch, no service, no
egress. The model is baked into the backend image at build time under
``FASTEMBED_CACHE_PATH`` (outside ``/app``, which the dev compose bind-mounts
over) and read with ``local_files_only``; at runtime nothing is downloaded, and
``HF_HUB_OFFLINE=1`` in the image makes that a property, not a hope.

Construction is the expensive, non-thread-safe part (seconds); inference is
thread-safe. The singleton is built once under a lock — by ``warm()`` from a
background thread at boot when memory is on, or lazily by the first caller —
and callers run ``embed`` off the event loop (``asyncio.to_thread``).

Never raises into a caller: a model that cannot load makes ``embed`` return
``None`` and ``availability()`` say why.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_NAME = os.getenv("TRINITY_MEMORY_EMBEDDING_MODEL", DEFAULT_MODEL)
CACHE_DIR = os.getenv("FASTEMBED_CACHE_PATH") or None
EMBED_DIM = 384
MAX_TEXT_CHARS = 2000   # the model truncates at its token window; this bounds the tokenizer's work

_lock = threading.Lock()
_model = None
_error: Optional[str] = None


def _build():
    """Construct the model once. Sets ``_model`` or ``_error``; never raises."""
    global _model, _error
    if _model is not None or _error is not None:
        return
    with _lock:
        if _model is not None or _error is not None:
            return
        try:
            from fastembed import TextEmbedding  # imported here: optional at import time
            kwargs = {"threads": 1, "local_files_only": True}
            if CACHE_DIR:
                kwargs["cache_dir"] = CACHE_DIR
            _model = TextEmbedding(MODEL_NAME, **kwargs)
            logger.info("[memory] embedding model ready: %s", MODEL_NAME)
        except ImportError as e:
            _error = f"fastembed is not installed ({e})"
            logger.warning("[memory] embeddings unavailable: %s", _error)
        except Exception as e:  # noqa: BLE001 — a missing/corrupt model dir, etc.
            _error = f"embedding model {MODEL_NAME} could not load: {str(e)[:200]}"
            logger.warning("[memory] embeddings unavailable: %s", _error)


def availability() -> Tuple[bool, Optional[str]]:
    """``(ok, reason)`` — builds the model if nothing has yet."""
    _build()
    return (_model is not None, _error)


def is_loaded() -> bool:
    return _model is not None


def last_error() -> Optional[str]:
    """Why the last build failed, or None (also None before any build)."""
    return _error


def warm() -> None:
    """Build the model on a daemon thread so boot never waits for it."""
    threading.Thread(target=_build, name="memory-embeddings-warm", daemon=True).start()


def embed(texts: Sequence[str]) -> Optional[List[List[float]]]:
    """L2-normalised float vectors for ``texts``, or None when unavailable.

    Synchronous and CPU-bound: call it from a worker thread.
    """
    _build()
    if _model is None:
        return None
    try:
        import numpy as np
        clipped = [(t or "")[:MAX_TEXT_CHARS] for t in texts]
        out = []
        for vec in _model.embed(clipped, batch_size=16):
            v = np.asarray(vec, dtype="<f4")
            norm = float(np.linalg.norm(v)) or 1.0
            out.append((v / norm).tolist())
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("[memory] embed failed: %s", str(e)[:200])
        return None


def to_bytes(vector: Sequence[float]) -> bytes:
    import numpy as np
    return np.asarray(vector, dtype="<f4").tobytes()


def from_bytes(blob: bytes):
    import numpy as np
    return np.frombuffer(blob, dtype="<f4")


def reset_for_tests() -> None:
    """Drop the singleton so a test can install a different model/error."""
    global _model, _error
    with _lock:
        _model = None
        _error = None
