from pathlib import Path

from app.version import build_code_version, sha_from_git_dir

SHA = "1f88552c0ffee0000000000000000000000000ab"


def _git(tmp_path: Path, head: str, ref: str | None = None, packed: str | None = None) -> Path:
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text(head)
    if ref:
        (git / "refs" / "heads" / "main").write_text(ref + "\n")
    if packed:
        (git / "packed-refs").write_text(packed)
    return git


def test_loose_ref(tmp_path):
    assert sha_from_git_dir(_git(tmp_path, "ref: refs/heads/main\n", ref=SHA)) == SHA


def test_packed_ref(tmp_path):
    git = _git(tmp_path, "ref: refs/heads/main\n", packed=f"# pack-refs\n{SHA} refs/heads/main\n")
    assert build_code_version(None, git) == SHA[:7]


def test_detached_head(tmp_path):
    assert build_code_version("", _git(tmp_path, SHA)) == "1f88552"


def test_override_wins_and_missing_git_is_unknown(tmp_path):
    assert build_code_version("abc1234", _git(tmp_path, SHA)) == "abc1234"
    assert build_code_version(None, tmp_path / "nope") == "unknown"
    assert build_code_version("unknown", tmp_path / "nope") == "unknown"
