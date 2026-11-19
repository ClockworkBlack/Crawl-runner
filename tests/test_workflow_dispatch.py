"""Every `gh workflow run` in the workflows names its repository (-R).

Without -R, gh finds the repository from the git checkout it runs in; a step that runs where there is none fails
with "not a git repository" (sync-code's "Keep the crawl going", Nov 19, 2026: the crawl chain stopped after a failed
run because the step meant to restart it could not dispatch anything)."""
import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def test_every_dispatch_names_its_repository():
    bad = []
    for p in sorted(WORKFLOWS.glob("*.yml")):
        for n, line in enumerate(p.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for m in re.finditer(r"gh workflow run\b[^;&|\n]*", line):
                if not re.search(r"(?:^|\s)(?:-R|--repo)(?:\s|=)", m.group(0)):
                    bad.append(f"{p.name}:{n}: {m.group(0).strip()}")
    assert not bad, "gh workflow run without -R:\n" + "\n".join(bad)
