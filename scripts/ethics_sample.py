"""ethics.yml sample: the Georgia money reader for four minutes on an empty database, and what it read."""
import json, time
from collections import Counter
from gacrawl import config, ethics
from gacrawl.candidates import load_candidates
from gacrawl.store import Store
from gacrawl.money import lines
cfg = config.load()
f = config.make_fetcher(cfg)
s = Store("/tmp/ethics-sample.db")
st = ethics.run(f, s, config.ROOT, load_candidates(config.ROOT, cfg), time.monotonic() + 240)
print(json.dumps(st)[:2000])
by = Counter(); n = 0; matched = 0; tot = 0.0
for L in lines(s):
    n += 1; tot += L["amount"]; by[L["meta"].get("recipient")] += L["amount"]
    matched += bool(L["meta"].get("candidate_id"))
print(f"lines counted {n}, ${tot:,.0f}, matched to an Electo candidate {matched}")
for k, v in by.most_common(15): print(f"  {k}: ${v:,.0f}")
