"""Code version for pipeline_runs.code_version: the git short sha.

Resolution, first hit wins:
1. env CODE_VERSION (e.g. passed as a docker build arg / container env),
2. the file CODE_VERSION at the repository root (written at image build time
   by `python -m app.version`),
3. "unknown".

`python -m app.version [override]` prints the build-time value: the override if
given, else the short sha read directly from .git (no git binary needed), else
"unknown" (e.g. built from a tarball without .git).
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "CODE_VERSION"
UNKNOWN = "unknown"
_SHA = re.compile(r"[0-9a-f]{40}")


def sha_from_git_dir(git_dir: Path) -> str | None:
    """Commit sha of HEAD from a .git directory: detached HEAD, loose ref or packed-refs."""
    try:
        head = (git_dir / "HEAD").read_text().strip()
    except OSError:
        return None
    if _SHA.fullmatch(head):
        return head
    if not head.startswith("ref: "):
        return None
    ref = head.removeprefix("ref: ").strip()
    try:
        sha = (git_dir / ref).read_text().strip()
        if _SHA.fullmatch(sha):
            return sha
    except OSError:
        pass
    try:
        for line in (git_dir / "packed-refs").read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref and _SHA.fullmatch(parts[0]):
                return parts[0]
    except OSError:
        pass
    return None


def build_code_version(override: str | None, git_dir: Path = ROOT / ".git") -> str:
    if override and override.strip() and override.strip() != UNKNOWN:
        return override.strip()
    sha = sha_from_git_dir(git_dir)
    return sha[:7] if sha else UNKNOWN


def runtime_code_version() -> str:
    env = os.environ.get("CODE_VERSION", "").strip()
    if env and env != UNKNOWN:
        return env
    try:
        value = VERSION_FILE.read_text().strip()
        if value:
            return value
    except OSError:
        pass
    return UNKNOWN


if __name__ == "__main__":
    print(build_code_version(sys.argv[1] if len(sys.argv) > 1 else None))
