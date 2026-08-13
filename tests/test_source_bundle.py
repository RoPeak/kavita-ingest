from __future__ import annotations

import subprocess
from pathlib import Path
from zipfile import ZipFile

BUNDLE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "create-source-bundle.sh"


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True, text=True
    )


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Bundle Test")
    _git(repository, "config", "user.email", "bundle@example.invalid")
    (repository / ".gitignore").write_text(".env\n.pytest_cache/\ndist/\n", encoding="utf-8")
    (repository / "README.md").write_text("tracked source\n", encoding="utf-8")
    (repository / ".env").write_text("SECRET=must-not-ship\n", encoding="utf-8")
    (repository / ".pytest_cache").mkdir()
    (repository / ".pytest_cache" / "junk").write_text("local cache\n", encoding="utf-8")
    _git(repository, "add", ".gitignore", "README.md")
    _git(repository, "commit", "-q", "-m", "fixture")
    return repository


def test_source_bundle_archives_only_committed_head(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    output = tmp_path / "source.zip"
    result = subprocess.run(
        ["bash", str(BUNDLE_SCRIPT), str(output)],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert output.exists()
    with ZipFile(output) as archive:
        names = archive.namelist()
        assert any(name.endswith("/README.md") for name in names)
        assert not any(name.endswith("/.env") for name in names)
        assert not any("/.git/" in name for name in names)
        assert not any("/.pytest_cache/" in name for name in names)


def test_source_bundle_refuses_dirty_worktree(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    output = tmp_path / "source.zip"
    (repository / "untracked.txt").write_text("not committed\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(BUNDLE_SCRIPT), str(output)],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "working tree is not clean" in result.stderr
    assert not output.exists()
