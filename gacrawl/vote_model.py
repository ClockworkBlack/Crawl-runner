"""How each legislator votes, added up to how the chamber votes (vote_model.json in the Electo bundle).

Built at export from what `gacrawl legis update` and `gacrawl legis legiscan` stored (no requests): every member's Yea
or Nay on every roll call read so far, and each bill's sponsors and committees. legis.ga.gov's roll calls come first;
LegiScan's fill the ones legis.ga.gov did not give or gave with a member list that did not add up to its own tally,
and every roll call both have is compared (`sources`: how often the tallies and the member votes agree, and each
disagreement).

  positions   one left-right number per member, fitted to their recorded votes on the roll calls that divided the
              chamber (a one-dimensional item-response model, the family NOMINATE scores come from). Higher = votes
              more with Republicans. A description of the votes cast, never a label anyone gave themselves.
  vote model  the chance a member votes Yea on a roll call, from their position, party and record, the bill's sponsors
              (their positions, their parties, whether the member is one), its committee, and how each party and the
              member voted on the bill's earlier roll calls
  macro       a roll call's expected Yea count = the sum of its members' chances

Everything is scored forward in time: a vote cast in year Y is predicted from the records dated before Jan. 1 of Y,
plus the bill's own earlier roll calls (public before the later vote is taken). The same questions are answered by
plain rules (always Yea; the member's usual vote; the lead sponsor's party; the member's party on the bill's earlier
vote), and the file says whether the model beat them, so the page never shows a number that a one-line rule matches.

Needs numpy, scipy and scikit-learn (CPU, minutes); without them the export leaves the file as it was.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from pathlib import Path

LEGIS = "ga:legis"
MIN_ROLL_CALLS = 60          # fewer divided roll calls than this: positions only, no test
MIN_MEMBER_VOTES = 10        # a member's position is shown only with at least this many counted votes
CONTESTED = 0.025            # a roll call "divided the chamber" when at least this share voted with the losing side
TRAIN_CAP = 250_000          # rows a vote model is fitted on (whole roll calls, sampled with a fixed seed)
TEST_YEARS = 8               # the newest years scored, each from the years before it
FEATS = ["party_R", "x", "x_in_party", "loyal", "yea_rate", "n_past", "senate", "k_agree", "k_ccr", "k_ca", "k_amend",
         "resolution", "local", "has_prev", "prev_own_party", "prev_other_party", "prev_all", "own_prev_vote",
         "prev_same_chamber", "n_sponsors", "sp_x", "sp_R", "sp_dist", "is_sponsor", "lead_R", "lead_same_party",
         "x_times_gap", "bip", "prev_fit"]
RULES = ["always yes", "their usual vote", "who carries the bill", "their party on the bill's earlier vote",
         "their own earlier vote on the bill, else their party's"]


def kind_of(caption: str) -> str:
    c = (caption or "").lower()
    if "conference" in c or "ccr" in c:
        return "ccr"
    if "constitutional" in c:
        return "ca"
    if "agree" in c or "concur" in c:
        return "agree"
    if re.search(r"amendment\s*#|adoption of (?:floor )?amendment|adopt (?:floor )?amendment", c):
        return "amend"
    return "passage"


# ----------------------------------------------------------------------------- the records
LEGISCAN = "ga:legiscan"
MAX_DISAGREEMENTS = 60


def _num(b: str) -> str:
    return re.sub(r"\s+", "", (b or "").upper())


def pick_roll_calls(store) -> tuple[list[dict], dict]:
    """Every roll call to learn from, one per real roll call: legis.ga.gov's when its member list adds up to its own
    tally (or was stored before that check existed), else LegiScan's copy of the same roll call; a roll call only
    LegiScan has is taken from it. Both sources' copies of one roll call are compared, tally and member by member."""
    def good(m):
        return (m.get("check") or {}).get("ok", True)

    legis_rc, bad_legis = [], []
    for v in store.iter_query("kind='vote' AND source_id=?", (LEGIS,)):
        m = dict(v["meta"], url=v.get("url") or "", src="legis.ga.gov")
        if m.get("substantive") and re.match(r"\d{4}-\d\d-\d\d", m.get("date") or ""):
            (legis_rc if good(m) else bad_legis).append(m)
    ls_rc = []
    for v in store.iter_query("kind='vote' AND source_id=?", (LEGISCAN,)):
        m = dict(v["meta"], url=v.get("url") or "", src="LegiScan")
        if m.get("substantive") and good(m) and re.match(r"\d{4}-\d\d-\d\d", m.get("date") or ""):
            ls_rc.append(m)

    def keys(m):
        k = []
        if m.get("legislation_id"):
            k.append(("id", m["legislation_id"], m.get("chamber"), m.get("date")))
        if m.get("bill"):
            k.append(("no", _num(m["bill"]), m.get("chamber"), m.get("date")))
        return k

    def index(rows):
        ix = defaultdict(list)
        for i, m in enumerate(rows):
            for k in keys(m):
                ix[k].append(i)
        return ix

    def find(m, ix, rows):
        cand = []
        for k in keys(m):
            cand += [i for i in ix.get(k, []) if i not in cand]
        if len(cand) > 1:          # two roll calls on one bill the same day: the one with the same tally
            same = [i for i in cand if (rows[i].get("yea"), rows[i].get("nay")) == (m.get("yea"), m.get("nay"))]
            cand = same[:1] if same else []
        return cand[0] if cand else None

    ix_legis, ix_bad = index(legis_rc), index(bad_legis)
    used = []
    stats = {"legis_roll_calls": len(legis_rc), "legis_failed_check": len(bad_legis), "legiscan_roll_calls": len(ls_rc),
             "compared": 0, "tallies_agree": 0, "member_votes_compared": 0, "member_votes_agree": 0,
             "filled_from_legiscan": 0, "replaced_by_legiscan": 0, "disagreements": []}
    taken = set()
    for m in ls_rc:
        i = find(m, ix_legis, legis_rc)
        if i is not None:
            if i in taken:
                continue
            taken.add(i)
            a = legis_rc[i]
            stats["compared"] += 1
            same_tally = (int(a.get("yea") or 0), int(a.get("nay") or 0)) == (int(m.get("yea") or 0), int(m.get("nay") or 0))
            stats["tallies_agree"] += same_tally
            mine = {x.get("person_id"): x.get("vote") for x in a.get("votes") or [] if x.get("vote") in ("Yea", "Nay")}
            theirs = {x.get("person_id"): x.get("vote") for x in m.get("votes") or [] if x.get("vote") in ("Yea", "Nay")}
            both = [k for k in mine if k in theirs and not str(k).startswith("ls:")]
            diff = [k for k in both if mine[k] != theirs[k]]
            stats["member_votes_compared"] += len(both)
            stats["member_votes_agree"] += len(both) - len(diff)
            if (diff or not same_tally) and len(stats["disagreements"]) < MAX_DISAGREEMENTS:
                names = {x.get("person_id"): x.get("person") for x in a.get("votes") or []}
                stats["disagreements"].append({"bill": a.get("bill"), "date": a.get("date"), "chamber": a.get("chamber"), "u": a.get("url"),
                                               "legiscan_u": m.get("url"), "tally": [[a.get("yea"), a.get("nay")], [m.get("yea"), m.get("nay")]],
                                               "members": [{"name": names.get(k), "legis": mine[k], "legiscan": theirs[k]} for k in diff[:10]]})
            continue
        if find(m, ix_bad, bad_legis) is not None:
            stats["replaced_by_legiscan"] += 1
        else:
            stats["filled_from_legiscan"] += 1
        used.append(m)
    for k in ("tallies_agree", "member_votes_agree"):
        base = stats["compared"] if k == "tallies_agree" else stats["member_votes_compared"]
        stats[k.replace("agree", "agree_share")] = round(stats[k] / base, 4) if base else None
    return legis_rc + used, stats


def load(store):
    """Roll calls with every member's Yea/Nay, and what is known of each bill before any vote on it."""
    import numpy as np

    ids: dict = {}
    names, party_of, chamber_of = {}, {}, {}
    rcs = []
    picked, source_stats = pick_roll_calls(store)
    for m in picked:
        date = m.get("date") or ""
        mem, yes = [], []
        for r in m.get("votes") or []:
            pid = r.get("person_id")
            if pid is None or r.get("vote") not in ("Yea", "Nay") or r.get("party") not in ("R", "D"):
                continue
            i = ids.setdefault(pid, len(ids))
            names[i] = r.get("person") or names.get(i, "")
            party_of[i] = 1.0 if r["party"] == "R" else 0.0     # the newest record read wins (a party change is rare)
            chamber_of[i] = m.get("chamber") or ""
            mem.append(i)
            yes.append(1.0 if r["vote"] == "Yea" else 0.0)
        if len(mem) < 10:
            continue
        rcs.append({"id": m.get("VoteId") or m.get("roll_call_id"), "legid": m.get("legislation_id") or ("no", _num(m.get("bill"))),
                    "date": date, "year": int(date[:4]),
                    "ch": "S" if (m.get("chamber") or "").startswith("S") else "H", "bill": m.get("bill") or "",
                    "session": m.get("session_name") or "", "cap": f"{m.get('vote_name') or ''} {m.get('vote_caption') or ''}".strip(),
                    "kind": kind_of(m.get("vote_caption") or m.get("vote_name") or ""), "url": m.get("url") or "", "src": m.get("src"),
                    "m": np.array(mem, dtype=np.int64), "y": np.array(yes, dtype=np.float32)})
    rcs.sort(key=lambda r: (r["date"], str(r["legid"]), str(r["id"])))
    for j, r in enumerate(rcs):
        r["j"] = j
    n = len(ids)
    party = np.array([party_of[i] for i in range(n)], dtype=np.float64) if n else np.zeros(0)
    bills = {}
    for b in store.iter_query("kind='bill' AND source_id=?", (LEGIS,)):
        m = b["meta"]
        sp = [(ids[s["member_id"]], s.get("sequence") == 1) for s in m.get("sponsors") or [] if s.get("member_id") in ids]
        com = f"{m.get('house_committee') or ''} {m.get('senate_committee') or ''}"
        bills[m.get("legislation_id")] = {"sponsors": sp, "local": 1.0 if re.search(r"\blocal\b", com, re.I) else 0.0,
                                          "resolution": 1.0 if re.match(r"[HS]R\b", m.get("number") or "") else 0.0}
    members = [{"id": pid, "name": names.get(i, ""), "chamber": chamber_of.get(i, "")} for pid, i in sorted(ids.items(), key=lambda t: t[1])]
    return members, party, rcs, bills, source_stats


def contested(r) -> bool:
    s = float(r["y"].mean())
    return CONTESTED <= s <= 1 - CONTESTED


# ----------------------------------------------------------------------------- positions
def fit_positions(rcs, n_members, party, lam=0.5):
    """P(Yea) = sigmoid(a_j + b_j * x_i) over the roll calls that divided the chamber. x is scaled to a standard
    deviation of 1 with Republicans on the positive side; a member with no such vote gets their party's average."""
    import numpy as np
    from scipy.optimize import minimize

    x0 = np.where(party > 0, 1.0, -1.0)
    use = [r for r in rcs if contested(r)]
    if not use:
        return x0.copy(), np.zeros(n_members, dtype=int)
    m = np.concatenate([r["m"] for r in use])
    j = np.concatenate([np.full(len(r["m"]), s) for s, r in enumerate(use)])
    y = np.concatenate([r["y"] for r in use]).astype(np.float64)
    nj = len(use)
    cnt = np.bincount(m, minlength=n_members)
    seen = cnt > 0
    th0 = np.concatenate([x0, np.zeros(nj), np.zeros(nj)])

    def f(th):
        x, a, b = th[:n_members], th[n_members:n_members + nj], th[n_members + nj:]
        p = 1 / (1 + np.exp(-np.clip(a[j] + b[j] * x[m], -30, 30)))
        loss = -(y * np.log(p + 1e-12) + (1 - y) * np.log(1 - p + 1e-12)).sum() + 0.5 * lam * (x ** 2).sum() + 0.05 * ((a ** 2).sum() + (b ** 2).sum())
        g = p - y
        return loss, np.concatenate([np.bincount(m, g * b[j], n_members) + lam * x, np.bincount(j, g, nj) + 0.1 * a,
                                     np.bincount(j, g * x[m], nj) + 0.1 * b])

    r = minimize(f, th0, jac=True, method="L-BFGS-B", options={"maxiter": 400})
    x = r.x[:n_members].copy()
    R, D = seen & (party > 0), seen & (party == 0)
    if R.any() and D.any() and x[R].mean() < x[D].mean():
        x = -x
    x = x / (x[seen].std() or 1.0)
    for pv, sel in ((1.0, R), (0.0, D)):
        x[~seen & (party == pv)] = x[sel].mean() if sel.any() else (1.0 if pv else -1.0)
    return x, cnt


# ----------------------------------------------------------------------------- one row per recorded vote
def _shares(r, party):
    import numpy as np

    out = {}
    for pv in (0.0, 1.0):
        sel = party[r["m"]] == pv
        out[pv] = float(r["y"][sel].mean()) if sel.any() else np.nan
    out["all"] = float(r["y"].mean())
    return out


def build_rows(rcs, party, bills, years):
    """Features for every vote of the given years, each from the records before the vote's year and the bill's
    earlier roll calls. Returns X (float32), y, the roll call index and the member index of each row."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    n = len(party)
    by_bill = defaultdict(list)
    for r in rcs:
        by_bill[r["legid"]].append(r)
    X, Y, RC, MEM = [], [], [], []
    for yr in years:
        past = [r for r in rcs if r["year"] < yr]
        x, _ = fit_positions(past, n, party)
        agree = np.zeros(n); div = np.zeros(n); yeas = np.zeros(n); cnt = np.zeros(n)
        for r in past:
            np.add.at(cnt, r["m"], 1); np.add.at(yeas, r["m"], r["y"])
            if contested(r):
                sh = _shares(r, party)
                own = np.where(party[r["m"]] > 0, sh[1.0], sh[0.0])
                ok = ~np.isnan(own)
                np.add.at(div, r["m"][ok], 1)
                np.add.at(agree, r["m"][ok], ((r["y"][ok] >= 0.5) == (own[ok] >= 0.5)).astype(float))
        loyal = np.where(div > 0, (agree + 4.5) / (div + 5), 0.9)
        yea_rate = (yeas + 4) / (cnt + 5)
        mu, sd = {}, {}
        for pv in (0.0, 1.0):
            v = x[(party == pv) & (cnt > 0)]
            mu[pv] = v.mean() if len(v) else (1.0 if pv else -1.0)
            sd[pv] = v.std() if len(v) > 1 and v.std() > 0 else 1.0
        mu_a = np.where(party > 0, mu[1.0], mu[0.0]); sd_a = np.where(party > 0, sd[1.0], sd[0.0])
        for r in rcs:
            if r["year"] != yr:
                continue
            mem, k = r["m"], len(r["m"])
            pv = party[mem]
            # strictly earlier days only: the order of two roll calls taken the same day is not in the record
            prev = [q for q in by_bill[r["legid"]] if q["date"] < r["date"]]
            last = prev[-1] if prev else None
            b = bills.get(r["legid"]) or {"sponsors": [], "local": np.nan, "resolution": np.nan}
            sp = b["sponsors"]
            sp_x = float(np.mean([x[i] for i, _ in sp])) if sp else np.nan
            sp_R = float(np.mean([party[i] for i, _ in sp])) if sp else np.nan
            lead = next((party[i] for i, is_lead in sp if is_lead), np.nan)
            bip = (1.0 if 0 < sp_R < 1 else 0.0) if sp else np.nan
            po = pt = pa = fit = np.full(k, np.nan)
            own_prev = np.full(k, np.nan)
            same_ch, gap = 0.0, (2 * sp_R - 1) if sp else 0.0
            if last is not None:
                sh = _shares(last, party)
                po = np.where(pv > 0, sh[1.0], sh[0.0]); pt = np.where(pv > 0, sh[0.0], sh[1.0]); pa = np.full(k, sh["all"])
                same_ch = 1.0 if last["ch"] == r["ch"] else 0.0
                if not (np.isnan(sh[1.0]) or np.isnan(sh[0.0])):
                    gap = sh[1.0] - sh[0.0]
                # the earlier roll call as a line on the left-right scale: who sat on which side of it
                if 0 < last["y"].sum() < len(last["y"]):
                    lr = LogisticRegression(C=1.0, max_iter=300).fit(x[last["m"]][:, None], last["y"])
                    fit = lr.predict_proba(x[mem][:, None])[:, 1]
                else:
                    fit = np.full(k, float(np.clip(last["y"].mean(), 0.02, 0.98)))
                mine = {}
                for q in prev:
                    mine.update(zip(q["m"].tolist(), q["y"].tolist()))
                own_prev = np.array([mine.get(i, np.nan) for i in mem.tolist()])
            spset = {i for i, _ in sp}
            cols = [pv, x[mem], (x[mem] - mu_a[mem]) / sd_a[mem], loyal[mem], yea_rate[mem], cnt[mem],
                    np.full(k, float(r["ch"] == "S")), np.full(k, float(r["kind"] == "agree")), np.full(k, float(r["kind"] == "ccr")),
                    np.full(k, float(r["kind"] == "ca")), np.full(k, float(r["kind"] == "amend")), np.full(k, b["resolution"]),
                    np.full(k, b["local"]), np.full(k, 1.0 if last is not None else 0.0), po, pt, pa, own_prev, np.full(k, same_ch),
                    np.full(k, float(len(sp))), np.full(k, sp_x), np.full(k, sp_R),
                    np.abs(x[mem] - sp_x) if sp else np.full(k, np.nan), np.array([1.0 if i in spset else 0.0 for i in mem.tolist()]),
                    np.full(k, lead), (pv == lead).astype(float) if not np.isnan(lead) else np.full(k, np.nan), x[mem] * gap,
                    np.full(k, bip), fit]
            X.append(np.column_stack(cols).astype(np.float32)); Y.append(r["y"]); RC.append(np.full(k, r["j"])); MEM.append(mem)
    if not X:
        return np.zeros((0, len(FEATS)), np.float32), np.zeros(0), np.zeros(0, int), np.zeros(0, int)
    return np.vstack(X), np.concatenate(Y).astype(int), np.concatenate(RC), np.concatenate(MEM)


# ----------------------------------------------------------------------------- the vote model and the rules
class _Model:
    """Boosted trees and a logistic regression, averaged. Columns with nothing in them in the training rows are
    dropped (a member's own earlier vote on a bill is empty until more than one roll call per bill has been read)."""
    def fit(self, X, y, groups, seed=0):
        import numpy as np
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler

        if len(y) > TRAIN_CAP:                                  # whole roll calls, not single votes
            gs = np.unique(groups)
            np.random.default_rng(seed).shuffle(gs)
            keep, total = [], 0
            sizes = dict(zip(*np.unique(groups, return_counts=True)))
            for g in gs:
                if total >= TRAIN_CAP:
                    break
                keep.append(g); total += sizes[g]
            sel = np.isin(groups, keep)
            X, y = X[sel], y[sel]
        X = X.astype(np.float64)
        with np.errstate(invalid="ignore"):
            self.keep = np.flatnonzero((~np.isnan(X)).any(axis=0) & (np.nan_to_num(np.nanstd(X, axis=0)) > 0))
        Z = X[:, self.keep]
        self.trees = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=250, min_samples_leaf=40,
                                                    l2_regularization=1.0, random_state=0).fit(Z, y)
        self.med = np.nan_to_num(np.nanmedian(Z, axis=0))
        self.gaps = np.flatnonzero(np.isnan(Z).any(axis=0))
        self.scale = StandardScaler().fit(self._fill(Z))
        self.line = LogisticRegression(C=0.3, max_iter=2000).fit(self.scale.transform(self._fill(Z)), y)
        return self

    def _fill(self, Z):
        import numpy as np

        return np.hstack([np.where(np.isnan(Z), self.med, Z), np.isnan(Z[:, self.gaps]).astype(float)])

    def chance(self, X):
        Z = X.astype("float64")[:, self.keep]
        return (self.trees.predict_proba(Z)[:, 1] + self.line.predict_proba(self.scale.transform(self._fill(Z)))[:, 1]) / 2


def rules(X):
    import numpy as np

    f = {name: X[:, i].astype(np.float64) for i, name in enumerate(FEATS)}
    party_prev = np.where(np.isnan(f["prev_own_party"]), 1.0, (f["prev_own_party"] >= 0.5).astype(float))
    return {"always yes": np.ones(len(X)), "their usual vote": (f["yea_rate"] >= 0.5).astype(float),
            "who carries the bill": np.where(np.isnan(f["lead_same_party"]), 1.0, f["lead_same_party"]),
            "their party on the bill's earlier vote": party_prev,
            "their own earlier vote on the bill, else their party's": np.where(np.isnan(f["own_prev_vote"]), party_prev, f["own_prev_vote"])}


def score(y, p, groups, n_boot=1000, seed=1):
    """Share right, on Yea and on Nay votes, Brier score, and a 95% range from re-drawing whole roll calls (the votes
    on one roll call move together, so single votes are not re-drawn)."""
    import numpy as np

    p = np.clip(np.asarray(p, dtype=np.float64), 1e-6, 1 - 1e-6)
    hit = ((p >= 0.5) == (y == 1)).astype(float)
    gs, inv = np.unique(groups, return_inverse=True)
    hits, size = np.bincount(inv, hit), np.bincount(inv).astype(float)
    rng = np.random.default_rng(seed)
    draw = rng.integers(0, len(gs), size=(n_boot, len(gs)))
    bs = hits[draw].sum(axis=1) / size[draw].sum(axis=1)
    yes, no = (y == 1), (y == 0)
    out = {"n": int(len(y)), "right": round(float(hit.mean()), 4),
           "range95": [round(float(np.percentile(bs, 2.5)), 4), round(float(np.percentile(bs, 97.5)), 4)],
           "right_on_yes_votes": round(float(hit[yes].mean()), 4) if yes.any() else None,
           "right_on_no_votes": round(float(hit[no].mean()), 4) if no.any() else None,
           "brier": round(float(((p - y) ** 2).mean()), 4)}
    return out


def _table(y, P, X, groups):
    import numpy as np

    out = {"questions": int(len(y)), "roll_calls": int(len(np.unique(groups))), "no_votes": int((y == 0).sum()),
           "model": score(y, P, groups)}
    for name, guess in rules(X).items():
        out[name] = score(y, np.where(guess >= 0.5, 0.97, 0.03), groups)
    best = max(RULES, key=lambda k: out[k]["right"])
    m, b = out["model"], out[best]
    out["best_rule"] = best
    out["verdict"] = ("better than every rule" if m["range95"][0] > b["right"] else
                      "ahead of every rule, within the range chance allows" if m["right"] > b["right"] else
                      "no better than the best rule")
    return out


# ----------------------------------------------------------------------------- export
def build(store, log=print) -> dict:
    import numpy as np

    members, party, rcs, bills, source_stats = load(store)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    divided = [r for r in rcs if contested(r)]
    out = {"v": 1, "built": now,
           "note": "How each legislator votes, added up. `members` gives each one a left-right number fitted to their recorded Yea and "
                   "Nay votes on the roll calls that divided their chamber (higher = votes more with Republicans); it describes votes "
                   "cast, nothing else. `test` predicts each year's votes from the years before it and scores the answers against the "
                   "votes cast, next to plain rules on the same questions. `macro` adds each roll call's chances up to an expected Yea "
                   "count and compares it with the count recorded. Roll calls are legis.ga.gov's own (each `url`).",
           "built_from": {"roll_calls": len(rcs), "divided_roll_calls": len(divided), "votes": int(sum(len(r["m"]) for r in rcs)),
                          "members": len(members), "bills": len({r["legid"] for r in rcs}),
                          "years": sorted({r["year"] for r in rcs})},
           "members": [], "test": None, "macro": None,
           "sources": {**source_stats, "used": {"legis.ga.gov": sum(1 for r in rcs if r.get("src") == "legis.ga.gov"),
                                                "LegiScan": sum(1 for r in rcs if r.get("src") == "LegiScan")},
                       "credit": "Roll calls from legis.ga.gov; any marked LegiScan are from LegiScan (legiscan.com), CC BY 4.0."}}
    if not rcs:
        out["status"] = "no roll calls read yet"
        return out
    x, cnt = fit_positions(rcs, len(members), party)
    # how often each member voted against most of their own party, on the roll calls that divided the chamber
    crossed = np.zeros(len(members)); div = np.zeros(len(members))
    for r in divided:
        sh = _shares(r, party)
        own = np.where(party[r["m"]] > 0, sh[1.0], sh[0.0])
        ok = ~np.isnan(own)
        np.add.at(div, r["m"][ok], 1)
        np.add.at(crossed, r["m"][ok], ((r["y"][ok] >= 0.5) != (own[ok] >= 0.5)).astype(float))
    for i in np.argsort(x):
        if cnt[i] >= MIN_MEMBER_VOTES:
            out["members"].append({"id": members[i]["id"], "name": members[i]["name"], "party": "R" if party[i] else "D",
                                   "chamber": members[i]["chamber"], "position": round(float(x[i]), 3), "votes_counted": int(cnt[i]),
                                   "against_party": round(float(crossed[i] / div[i]), 3) if div[i] else None})
    years = out["built_from"]["years"]
    if len(divided) < MIN_ROLL_CALLS or len(years) < 3:
        out["status"] = f"positions only: {len(divided)} divided roll calls over {len(years)} years is too few to test predictions on"
        return out
    X, y, rc, mem = build_rows(rcs, party, bills, years)
    yr_of = np.array([r["year"] for r in rcs])[rc]
    P = np.full(len(y), np.nan)
    for yr in years[1:][-TEST_YEARS:]:
        tr, te = yr_of < yr, yr_of == yr
        if tr.sum() < 2000 or len(np.unique(y[tr])) < 2 or not te.any():
            continue
        P[te] = _Model().fit(X[tr], y[tr], rc[tr]).chance(X[te])
        log(f"  vote model: {yr} predicted from {int(tr.sum())} earlier votes")
    done = ~np.isnan(P)
    if not done.any():
        out["status"] = "too few earlier votes to predict any year from"
        return out
    has_prev = X[:, FEATS.index("has_prev")] == 1
    test = {"all": _table(y[done], P[done], X[done], rc[done]), "by_year": {}}
    for label, sel in (("first_vote_on_the_bill", done & ~has_prev), ("later_vote_on_the_bill", done & has_prev)):
        if sel.sum() >= 200:
            test[label] = _table(y[sel], P[sel], X[sel], rc[sel])
    for yr in sorted(set(yr_of[done].tolist())):
        sel = done & (yr_of == yr)
        t = _table(y[sel], P[sel], X[sel], rc[sel])
        test["by_year"][str(yr)] = {"roll_calls": t["roll_calls"], "model": t["model"]["right"], "best_rule": t[t["best_rule"]]["right"],
                                    "model_brier": t["model"]["brier"], "best_rule_brier": t[t["best_rule"]]["brier"]}
    # members who voted against most of their own party on a divided roll call: the hardest votes to call
    own = np.where(np.isnan(X[:, FEATS.index("party_R")]), 0, X[:, FEATS.index("party_R")])
    cross = np.zeros(len(y), bool)
    for j in np.unique(rc[done]):
        b = rc == j
        for pv in (0, 1):
            bb = b & (own == pv)
            if bb.sum() >= 5:
                cross[bb] = y[bb] != (y[bb].mean() >= 0.5)
    cs = done & cross
    if cs.any():
        guess = rules(X[cs])[test["all"]["best_rule"]]
        test["against_party_votes"] = {"n": int(cs.sum()), "model_right": round(float(((P[cs] >= 0.5) == (y[cs] == 1)).mean()), 4),
                                       "best_rule_right": round(float((guess == y[cs]).mean()), 4)}
    bands = []
    for lo, hi in ((0, .1), (.1, .3), (.3, .5), (.5, .7), (.7, .9), (.9, 1.01)):
        b = done & (P >= lo) & (P < hi)
        if b.any():
            bands.append({"said": round(float(P[b].mean()), 3), "happened": round(float(y[b].mean()), 3), "n": int(b.sum())})
    test["bands"] = bands
    out["test"] = test
    # macro: each roll call's expected Yea count against the count recorded
    rows = []
    for r in rcs:
        sel = done & (rc == r["j"])
        if not sel.any():
            continue
        k, exp, act = int(sel.sum()), float(P[sel].sum()), int(y[sel].sum())
        pr = party[mem[sel]] > 0
        rows.append({"bill": r["bill"], "session": r["session"], "date": r["date"], "vote": r["cap"][:120], "u": r["url"], "src": r.get("src"), "voting": k,
                     "yea": act, "expected_yea": round(exp, 1), "carried": act > k / 2, "called": exp > k / 2,
                     "first": not bool(has_prev[sel][0]), "divided": contested(r),
                     "R": [int(y[sel][pr].sum()), round(float(P[sel][pr].sum()), 1)],
                     "D": [int(y[sel][~pr].sum()), round(float(P[sel][~pr].sum()), 1)]})
    miss = np.array([abs(m["expected_yea"] - m["yea"]) / m["voting"] for m in rows])
    dv = np.array([m["divided"] for m in rows])
    lost = [m for m in rows if not m["carried"]]
    out["macro"] = {"roll_calls": len(rows), "carried": int(sum(m["carried"] for m in rows)),
                    "outcome_called_right": int(sum(m["carried"] == m["called"] for m in rows)),
                    "failed": len(lost), "failed_called_right": int(sum(not m["called"] for m in lost)),
                    "mean_miss_share": round(float(miss.mean()), 4),
                    "mean_miss_share_divided": round(float(miss[dv].mean()), 4) if dv.any() else None,
                    "largest_misses": sorted(rows, key=lambda m: -abs(m["expected_yea"] - m["yea"]) / m["voting"])[:12],
                    "newest": sorted(rows, key=lambda m: m["date"], reverse=True)[:40]}
    out["status"] = "tested"
    return out


def export_vote_model(store, out_path: Path, log=print) -> dict:
    """Writes vote_model.json; returns counts for the manifest. Without numpy/scipy/scikit-learn nothing is written."""
    try:
        import numpy  # noqa: F401
        import scipy  # noqa: F401
        import sklearn  # noqa: F401
    except ImportError as exc:
        log(f"  vote model: not built ({exc}); pip install numpy scipy scikit-learn")
        return {"built": False, "why": str(exc)}
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = build(store, log)
    Path(out_path).write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    t = (out.get("test") or {}).get("all") or {}
    counts = {"built": True, "status": out.get("status"), **out["built_from"], "members_shown": len(out["members"])}
    counts.pop("years", None)
    if t:
        counts.update({"model_right": t["model"]["right"], "best_rule": t["best_rule"], "best_rule_right": t[t["best_rule"]]["right"],
                       "verdict": t["verdict"]})
    return counts
