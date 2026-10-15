"""Tests for research/shadow_secondary_venue.py (synthetic data only)."""
import json
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import shadow_decision as sd  # noqa: E402
import shadow_secondary_venue as sv  # noqa: E402

T0 = sd.SHADOW_ELIGIBLE_FROM_T + 792  # a window start (multiple of 900)


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def make_rec(i, rng, o=None):
    T = T0 + 900 * i
    O = round(80000.0 + rng.uniform(-500, 500), 2)
    S = O * (1 + rng.gauss(0, 4e-4))
    tau = 300 + rng.randint(-25, 25)
    sh, sf, sm = rng.uniform(2e-5, 6e-5), 3e-5, max(1e-5, rng.uniform(0, 3e-5))
    settle = S * (1 + rng.gauss(0, 4e-4))
    if o is None:
        o = 1.0 if settle >= O else 0.0
    x = math.log(S / O) / math.sqrt(tau)
    g = {"S": S, "tau_actual": tau, "sigma_h60": sh, "sigma_f3": sf,
         "brier_h60": (_phi(x / sh) - o) ** 2,
         "brier_f3": (_phi(x / sf) - o) ** 2,
         "brier_model": (_phi(x / sm) - o) ** 2, "brier_mkt": 0.1}
    return {"T": T, "O": O, "outcome": o, "settle_price": settle,
            "shadow": {"p_h60": 0.5, "p_f3": 0.5},
            "tau_grid_scores": {"300": g}}


def recs(n, seed=1):
    rng = random.Random(seed)
    return [make_rec(i, rng) for i in range(n)]


def caps_for(rs, flip_every=0):
    """captures: pb(T)=O; pb(T+900) chosen to agree with outcome, except
    every flip_every-th window where it is chosen to disagree."""
    pb = {}
    for k, r in enumerate(rs):
        pb[r["T"]] = r["O"]
    for k, r in enumerate(rs):
        agree = not (flip_every and k % flip_every == 0)
        up = (r["outcome"] == 1.0) == agree
        nxt = r["O"] + (1.0 if up else -1.0)
        if r["T"] + 900 in pb and pb[r["T"] + 900] != round(nxt, 2):
            continue  # next window's pb already fixed by its own O
        pb[r["T"] + 900] = round(nxt, 2)
    return pb


def write_caps(path, pb):
    with open(path, "w") as f:
        for T, v in sorted(pb.items()):
            f.write(json.dumps({"T": T, "published_priceToBeat": v}) + "\n")


def write_scored(path, rs):
    with open(path, "w") as f:
        for r in rs:
            f.write(json.dumps(r) + "\n")


def test_p_recovery_is_exact_and_identity_when_label_unchanged():
    for r in recs(50):
        q = sv.relabel(r, r["outcome"], r["settle_price"])
        for k in ("brier_model", "brier_h60", "brier_f3"):
            assert abs(q["tau_grid_scores"]["300"][k]
                       - r["tau_grid_scores"]["300"][k]) < 1e-15


def test_flipped_label_gives_complement_brier():
    for r in recs(50, seed=2):
        o = r["outcome"]
        q = sv.relabel(r, 1.0 - o, r["settle_price"])
        for k in ("brier_model", "brier_h60", "brier_f3"):
            b = r["tau_grid_scores"]["300"][k]
            p = abs(o - math.sqrt(b))
            assert abs(q["tau_grid_scores"]["300"][k]
                       - (p - (1.0 - o)) ** 2) < 1e-12
        assert q["outcome"] == 1.0 - o
        assert r["outcome"] == o  # original untouched


def test_decide_on_equals_primary_decide2_on_300():
    for seed in (3, 4, 5):
        rs = recs(320, seed=seed)
        ev = sd._eval_recs(rs)
        assert len(ev) == 300
        v1, w1 = sd.decide2(rs)
        v2, w2, _ = sv.decide_on(ev)
        assert (v1, w1) == (v2, w2)


def test_venue_label_rules():
    r = {"T": T0, "O": 100.0}
    assert sv.venue_label(r, {T0: 100.0, T0 + 900: 100.0})[0] == 1.0  # tie
    assert sv.venue_label(r, {T0: 100.0, T0 + 900: 99.99})[0] == 0.0
    assert sv.venue_label(r, {T0: 100.0}) == (None, None, None)
    lv, nxt, proxy = sv.venue_label(r, {T0 + 900: 100.5})
    assert (lv, nxt, proxy) == (1.0, 100.5, True)


def test_check_counts_recompute_and_disagreements():
    rs = recs(40, seed=6)
    ev = sd._eval_recs(rs)
    pb = caps_for(ev, flip_every=5)
    c = sv.check_counts(ev, pb)
    assert c["eval_n"] == c["distinct_T"] == 40
    assert c["h60_match"] == c["f3_match"] == 40
    assert c["labelled"] + c["unlabelled"] == 40
    exp = sum(1 for r in ev
              if sv.venue_label(r, pb)[0] is not None
              and sv.venue_label(r, pb)[0] != r["outcome"])
    assert c["label_disagree"] == exp and exp > 0


def test_check_counts_detects_wrong_label():
    rs = recs(20, seed=7)
    rs[3]["outcome"] = 1.0 - rs[3]["outcome"]  # brier no longer matches
    c = sv.check_counts(sd._eval_recs(rs), {})
    assert c["h60_match"] == 19 and c["f3_match"] == 19
    assert c["unlabelled"] == 20


def test_main_check_mode_prints_counts_only(tmp_path):
    rs = recs(310, seed=8)
    s, k = str(tmp_path / "s.jsonl"), str(tmp_path / "c.jsonl")
    write_scored(s, rs)
    write_caps(k, caps_for(rs))
    assert sv.main(["--scored", s, "--captures", k, "--check"]) == 0


def test_main_refuses_before_primary_latched(tmp_path, monkeypatch):
    rs = recs(310, seed=9)
    s, k = str(tmp_path / "s.jsonl"), str(tmp_path / "c.jsonl")
    write_scored(s, rs)
    write_caps(k, caps_for(rs))
    monkeypatch.setattr(sd, "_verdict_path",
                        lambda: str(tmp_path / "v.json"))
    monkeypatch.setattr(sv, "OUT_PATH", str(tmp_path / "out.json"))
    assert sv.main(["--scored", s, "--captures", k]) == 3
    assert not os.path.exists(str(tmp_path / "out.json"))


def test_main_runs_once_after_latch_and_refuses_overwrite(tmp_path,
                                                          monkeypatch):
    rs = recs(310, seed=10)
    s, k = str(tmp_path / "s.jsonl"), str(tmp_path / "c.jsonl")
    write_scored(s, rs)
    write_caps(k, caps_for(rs, flip_every=7))
    vp = str(tmp_path / "v.json")
    with open(vp, "w") as f:
        json.dump({"verdict": "HOLD (x)", "why": "t"}, f)
    monkeypatch.setattr(sd, "_verdict_path", lambda: vp)
    out = str(tmp_path / "out.json")
    monkeypatch.setattr(sv, "OUT_PATH", out)
    assert sv.main(["--scored", s, "--captures", k]) == 0
    d = json.load(open(out))
    assert d["primary_sha256"] == sv.PRIMARY_SHA256
    assert d["detail"]["n"] == d["counts"]["labelled"] <= 300
    assert sv.main(["--scored", s, "--captures", k]) == 4


def test_refuses_on_primary_hash_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "PRIMARY_SHA256", "0" * 64)
    s, k = str(tmp_path / "s.jsonl"), str(tmp_path / "c.jsonl")
    write_scored(s, recs(5))
    write_caps(k, {})
    assert sv.main(["--scored", s, "--captures", k, "--check"]) == 2
