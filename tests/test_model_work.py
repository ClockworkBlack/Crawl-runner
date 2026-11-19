"""model-work.yml imports Electo's people before it reads the Rumor Mill.

Without state/electo_people.json the job knew only config/candidates.yaml's people: from Nov 15 to 18, 655 of 657
questions to the model gave the person's name alone, so whether a page is about them or a namesake (same_person,
gacrawl/rumors_ai.py) was judged without their office or county."""
from pathlib import Path

import yaml

WF = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "model-work.yml"


def _steps():
    return yaml.safe_load(WF.read_text())["jobs"]["write"]["steps"]


def test_electo_people_come_before_the_rumor_readings():
    steps = _steps()
    runs = [str(s.get("run") or "") for s in steps]
    people = [i for i, r in enumerate(runs) if "scripts/electo_people.sh" in r]
    rumors = [i for i, r in enumerate(runs) if "gacrawl rumors-ai" in r]
    assert len(people) == 1 and len(rumors) == 1
    assert people[0] < rumors[0]
    assert steps[rumors[0]]["name"].startswith("Rumor Mill pages")
    install = [i for i, r in enumerate(runs) if "pip install -q -e ." in r]
    assert install and install[0] < people[0]          # `gacrawl electo import` needs the package installed


def test_electo_people_step_has_a_token_and_never_stops_the_job():
    s = next(s for s in _steps() if "scripts/electo_people.sh" in str(s.get("run") or ""))
    env = s.get("env") or {}
    assert "secrets.DATA_TOKEN" in str(env.get("ELECTO_TOKEN"))   # the public runner's token can read Electo
    # a failed import leaves the readings as they were (name and Georgia alone), not the donor and bill work undone
    assert "||" in s["run"] and "::warning" in s["run"]
