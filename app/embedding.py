"""sentence-transformers wrapper (CLAUDE.md §7).

Models are resolved through the Hugging Face cache first (no network), and only
downloaded when absent; the default model is baked into the image. `model_version`
is the hub commit of the snapshot that was loaded, else "unknown".
"""

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# chunk_embeddings.embedding is vector(384); see README "Known limitations".
EMBEDDING_DIMENSION = 384
# Only what sentence-transformers needs; skips ONNX/OpenVINO/TF/Flax variants.
HF_IGNORE_PATTERNS = [
    "onnx/*", "openvino/*", "*.onnx", "*.h5", "*.msgpack", "*.ot", "pytorch_model.bin", "*.tflite",
]
_COMMIT = re.compile(r"[0-9a-f]{40}")


class EmbeddingModelError(RuntimeError):
    pass


def resolve_model(model_name: str) -> tuple[str, str]:
    """(local path, model_version) for a hub id or a local directory."""
    if Path(model_name).is_dir():
        return model_name, "unknown"
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    try:
        path = snapshot_download(model_name, local_files_only=True, ignore_patterns=HF_IGNORE_PATTERNS)
    except LocalEntryNotFoundError:
        log.info("embedding model %s not cached; downloading", model_name)
        path = snapshot_download(model_name, ignore_patterns=HF_IGNORE_PATTERNS)
    revision = Path(path).name
    return path, revision if _COMMIT.fullmatch(revision) else "unknown"


@dataclass
class Embedder:
    model_name: str
    model_version: str
    dimension: int
    _model: object

    def encode(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        """All texts in one call (internally batched); unit-length vectors."""
        if not texts:
            return []
        vectors = self._model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return vectors.tolist()


def load_embedder(model_name: str) -> Embedder:
    from sentence_transformers import SentenceTransformer

    path, version = resolve_model(model_name)
    model = SentenceTransformer(path, device="cpu")
    dimension = model.get_sentence_embedding_dimension()
    if dimension != EMBEDDING_DIMENSION:
        raise EmbeddingModelError(
            f"{model_name} produces {dimension}-dim vectors; chunk_embeddings.embedding is "
            f"vector({EMBEDDING_DIMENSION}) (a new migration is needed for this model)"
        )
    return Embedder(model_name, version, dimension, model)


_cache: dict[str, Embedder] = {}
_lock = threading.Lock()


def get_embedder(model_name: str) -> Embedder:
    """Process-wide cache: each model is loaded once."""
    with _lock:
        if model_name not in _cache:
            _cache[model_name] = load_embedder(model_name)
        return _cache[model_name]


def to_pgvector(vector: list[float]) -> str:
    """Text form accepted by `%s::vector`."""
    return "[" + ",".join(f"{v:.8g}" for v in vector) + "]"
