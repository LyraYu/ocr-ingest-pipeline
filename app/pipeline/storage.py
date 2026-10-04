"""Raw and normalised zones on local disk.

URIs are stored as `file://data/<zone>/<name>` (docs/DESIGN.md §3.2): relative to the
logical data root, which maps to the configured DATA_DIR at read/write time.
"""

import os
import tempfile
from pathlib import Path

from app.config import get_settings

URI_PREFIX = "file://data/"


def raw_uri(hex_digest: str) -> str:
    return f"{URI_PREFIX}raw/{hex_digest}.json"


def normalised_uri(hex_digest: str, normaliser_version: str) -> str:
    return f"{URI_PREFIX}normalised/{hex_digest}.v{normaliser_version}.json"


def uri_to_path(uri: str) -> Path:
    if not uri.startswith(URI_PREFIX):
        raise ValueError(f"not a data-zone URI: {uri!r}")
    relative = Path(uri.removeprefix(URI_PREFIX))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"data-zone URI escapes the data root: {uri!r}")
    return get_settings().data_dir / relative


def write_bytes(uri: str, data: bytes) -> None:
    """Atomic write (temp file + rename), so a reader never sees a partial file."""
    path = uri_to_path(uri)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_bytes(uri: str) -> bytes:
    return uri_to_path(uri).read_bytes()
