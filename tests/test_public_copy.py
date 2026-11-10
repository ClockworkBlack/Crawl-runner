"""The public runner's code copy (sync-code.yml) refuses to publish when a copied file contains private text, and
then every runner job stays on old code. This runs the same check here, so such a file fails the tests before it is
pushed. The patterns and the excluded paths are read from sync-code.yml itself, so the two can't drift apart."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _copied_files() -> list[str]:
    y = (ROOT / ".github" / "workflows" / "sync-code.yml").read_text()
    excl = re.findall(r"--exclude (\S+)", y)
    names = {p.rstrip("/") for p in excl if p.endswith("/") and "/" not in p.rstrip("/")}   # a folder name anywhere
    paths = [p.rstrip("/") for p in excl if p not in names and p.rstrip("/") not in names]  # a path (folder or file)
    try:
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, timeout=60,
                                 check=True).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        tracked = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]
    out = []
    for f in tracked:
        parts = f.split("/")
        if names & set(parts[:-1]) or any(f == p or f.startswith(p + "/") for p in paths):
            continue
        if parts[-1] == "sync-code.yml":          # the guard skips its own workflow (--exclude=sync-code.yml)
            continue
        out.append(f)
    return out


def test_nothing_the_public_copy_would_refuse():
    y = (ROOT / ".github" / "workflows" / "sync-code.yml").read_text()
    pattern = re.compile(re.search(r'grep -rIlE "([^"]+)" pub', y).group(1))
    files = _copied_files()
    assert len(files) > 50 and not any(f.startswith(("data/", "imports/", "state/")) for f in files)
    bad = []
    for f in files:
        p = ROOT / f
        if not p.is_file():          # not checked out here (a sparse checkout)
            continue
        raw = p.read_bytes()
        if b"\0" in raw[:8000]:      # grep -I skips binary files
            continue
        if pattern.search(raw.decode("utf-8", errors="replace")):
            bad.append(f)
    assert not bad, f"the public runner's code copy would refuse these files (private text): {bad}"
