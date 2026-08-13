#!/usr/bin/env bash
set -Eeuo pipefail

refuse() {
    printf 'REFUSED: %s\n' "$*" >&2
    exit 2
}

command -v git >/dev/null 2>&1 || refuse "git is required"
command -v python3 >/dev/null 2>&1 || refuse "python3 is required"

root="$(git rev-parse --show-toplevel 2>/dev/null)" || refuse "run inside a Git worktree"
cd "$root"

if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
    refuse "working tree is not clean; commit or remove non-ignored changes before bundling"
fi

head_commit="$(git rev-parse --verify HEAD)"
short_commit="$(git rev-parse --short=12 HEAD)"
output="${1:-$root/dist/kavita-ingest-source-$short_commit.zip}"

if [[ "$output" != /* ]]; then
    output="$root/$output"
fi

[[ ! -e "$output" ]] || refuse "output already exists: $output"
mkdir -p "$(dirname "$output")"

temporary="$(mktemp "${output}.tmp.XXXXXX")"
cleanup() {
    rm -f "$temporary"
}
trap cleanup EXIT

git archive \
    --format=zip \
    --prefix="kavita-ingest-$short_commit/" \
    --output="$temporary" \
    "$head_commit"

python3 - "$temporary" <<'PY'
from pathlib import PurePosixPath
from zipfile import ZipFile
import sys

archive = sys.argv[1]
forbidden_components = {
    ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "__pycache__", "build", "dist",
}
bad: list[str] = []
with ZipFile(archive) as bundle:
    names = bundle.namelist()
    if not names:
        raise SystemExit("source bundle is empty")
    for name in names:
        parts = PurePosixPath(name).parts
        leaf = parts[-1] if parts else ""
        if any(part in forbidden_components for part in parts):
            bad.append(name)
            continue
        if leaf == ".env" or (leaf.startswith(".env.") and leaf != ".env.example"):
            bad.append(name)
            continue
        if leaf.casefold().endswith((".sqlite", ".sqlite3", ".log")):
            bad.append(name)
if bad:
    print("REFUSED: forbidden local material found in source archive:", file=sys.stderr)
    for name in bad:
        print(f"  {name}", file=sys.stderr)
    raise SystemExit(2)
PY

mv "$temporary" "$output"
trap - EXIT
printf 'Created safe source bundle: %s\n' "$output"
printf 'Commit: %s\n' "$head_commit"
