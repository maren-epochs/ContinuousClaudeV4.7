"""Characterization tests for tools/viz/recommend.py (VAL-613 refactor guard).

Pins the exact output of recommend() for every job, infer_job(), and
profile_frame() over a deterministic grid of profiles / DataFrames.  The
expected digests were captured on the pre-refactor HEAD; any behavior change
in the job rules or the pandas bridge changes a digest.

Run: py -3.13 tools/viz/test_recommend_characterization.py
Regenerate (only for an intended behavior change):
     py -3.13 tools/viz/test_recommend_characterization.py --print-digests
"""

import hashlib
import importlib.util
import itertools
import json
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
MODULE = PROJECT / "tools" / "viz" / "recommend.py"

_spec = importlib.util.spec_from_file_location("viz_recommend_char", MODULE)
rec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rec)

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

N_ROWS = (1, 3, 100)
TEMPORAL = (None, 2, 50, "nocard")
CATS = (
    (),
    (1,),
    (2,),
    (5,),
    (9,),
    (1, 3),
    (2, 12),
    (5, 3),
    (9, 12),
    (2, 3),
    (5, 12),
    (9, 3),
    (1, 12),
    (5, 3, 4),
)
FLAGS = ((False, None), (True, None), (False, True), (False, False))
NAMES = ("plain", "flow")
GEO = (None, 5, 12)
MEASURES = ((), (3,), (3, 3), (3, 6), (3, None), (None, None), (3, 3, 3))


def _profile(n_rows, temporal, cats, flags, names, geo, measures):
    """One synthetic profile dict from the grid coordinates."""
    cols = []
    if temporal is not None:
        card = None if temporal == "nocard" else temporal
        cols.append({"name": "date", "kind": "temporal", "cardinality": card})
    cat_names = ("source", "target", "stage") if names == "flow" else ("a", "b", "c")
    for i, card in enumerate(cats):
        c = {"name": cat_names[i], "kind": "categorical", "cardinality": card}
        if i == 0:
            ordered, is_series = flags
            if ordered:
                c["ordered"] = True
            if is_series is not None:
                c["is_series"] = is_series
        cols.append(c)
    if geo is not None:
        cols.append({"name": "region", "kind": "geo", "cardinality": geo})
    for i, scale in enumerate(measures):
        c = {"name": f"m{i}", "kind": "numeric", "cardinality": 50}
        if scale is not None:
            c["scale"] = scale
        cols.append(c)
    p = {"n_rows": n_rows, "columns": cols}
    if names == "plain":  # the flow variant omits "measures" (derived from kinds)
        p["measures"] = [f"m{i}" for i in range(len(measures))]
    return p


def profiles():
    """Every grid profile, in a fixed order."""
    for coords in itertools.product(
        N_ROWS, TEMPORAL, CATS, FLAGS, NAMES, GEO, MEASURES
    ):
        yield _profile(*coords)


def _digest(obj):
    """sha256 of the canonical JSON of obj."""
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def recommend_digests():
    """{job: digest of recommend(p, job) over the grid} + infer_job digest."""
    grid = list(profiles())
    out = {"_count": len(grid), "infer_job": _digest([rec.infer_job(p) for p in grid])}
    out["inferred"] = _digest([rec.recommend(p) for p in grid])
    for job in rec.JOBS:
        out[job] = _digest([rec.recommend(p, job) for p in grid])
    return out


def frames():
    """DataFrames exercising every _kind_of / profile_frame branch."""
    n = 40
    dates = pd.date_range("2024-01-01", periods=n // 4, freq="D").repeat(4)
    iso = [d.strftime("%Y-%m-%d") for d in dates]
    long_text = [f"free text comment number {i} with words" for i in range(n)]
    yield pd.DataFrame(
        {
            "date": dates,
            "symbol": ["A", "B", "C", "D"] * (n // 4),
            "price": [float(i) * 1.5 for i in range(n)],
            "volume": [i * 1000 for i in range(n)],
            "flag": [i % 2 == 0 for i in range(n)],
        }
    )
    yield pd.DataFrame(
        {
            "year": list(range(2000, 2000 + n)),
            "weather": ["sun", "rain"] * (n // 2),
            "temp": [None] * n,
            "notes": long_text,
        }
    )
    yield pd.DataFrame(
        {
            "when": iso,
            "description": ["x"] * n,
            "isoish": iso,
            "blob": long_text,
            "empty": [None] * n,
            "country": ["US", "FR"] * (n // 2),
            "zero": [0] * n,
            "neg": [-(10**i) for i in range(n // 8)] * 8,
        }
    )
    yield pd.DataFrame(
        {
            "period": pd.period_range("2020-01", periods=n, freq="M"),
            "size": pd.Categorical(
                ["S", "M", "L", "M"] * (n // 4),
                categories=["S", "M", "L"],
                ordered=True,
            ),
            "color": pd.Categorical(["red", "blue"] * (n // 2)),
            "date": ["not a date"] * n,
            "share": [0.5] * n,
        }
    )
    yield pd.DataFrame(
        {
            "a": ["x", "y"] * (n // 2),
            "b": [str(i) for i in range(n)],
            "time": [f"2024-02-{(i % 28) + 1:02d}" for i in range(n)],
            "v": [float("nan")] + [1.0] * (n - 1),
        }
    )
    yield pd.DataFrame({"only": [1, 2, 3]})
    yield pd.DataFrame({"name": ["a"] * 3})
    yield pd.DataFrame(
        {
            "lat": [1.0, 2.0, 3.0],
            "level": pd.Categorical([1, 2, 1]),
            "count": [4, 5, 6],
        }
    )


def frame_digests():
    """{index: digest of profile_frame(df)} plus recommend over each frame."""
    out = {}
    for i, df in enumerate(frames()):
        prof = rec.profile_frame(df)
        out[f"profile_{i}"] = _digest(prof)
        out[f"recommend_{i}"] = _digest([rec.recommend(df, j) for j in rec.JOBS])
    return out


EXPECTED_RECOMMEND = {
    "_count": 28224,
    "infer_job": "747b0b93b3a3d8f2e56c4dfba86bf66f33dcbaa5772814a5f33f1e0b9acf2531",
    "inferred": "74af9701022886a46623cc95761960f1174018cea79ba3ddcdb4314271e7bee3",
    "magnitude": "b9f587a21e823c67942778211ddb281d0fa8420af5588aafb055b1d387a81da0",
    "identity": "69576dfaad4133c013b14068b0a97fcf421ae8e21cfadc23bc8ac06107647a38",
    "polarity": "b232d74e00acc7a232eab584d6adef9b50e9d9ad4412bd81954a677258ae3eda",
    "headline": "7f008e7e14c51f5ae14c2c1bb464adee34b3ca4fa7bafb50578ae34f6891581c",
    "change-over-time": "726abc57c4f360c0235ba54ba875b99423c882a73a6a4c25cf9ec876ab909259",
    "distribution": "b50a1d45993630a20267528cc145df165520f13d0c89ec8434b71c03dd8238b8",
    "relationship": "4cc124f48252994ea3572f4801fad0ea527852e6c2962e83404a8b9f0ad4d9c9",
    "part-to-whole": "f9278c5204312a0071aa6ee0498a428a06c7d05e101222854ae55f0d7bd4cfc5",
    "ranking": "19eb8927f3c2e17afd6be92d8b98822d9e4a091bc12519205a078c51f5c47263",
    "flow": "2309eaa4809eb7cdf7148542fb136b2df1628f4c90cc59be065d56859e76730b",
    "spatial": "74ab9d8480fd9dacaad127696ea4d5920fac7df681f98065ad996d39a7747a17",
}
EXPECTED_FRAMES = {
    "profile_0": "2a6cbd426dacf751a40544529d645d96fcb9f05fd0b2ec421fd87230fb60e826",
    "recommend_0": "6d906e7a8e561331f2436868b06be83d4108e88af7c774d66f8441d5854e4856",
    "profile_1": "84695dda3e75101527ce08dfa2470b3886fc0499f5a88abda9418144d41944e5",
    "recommend_1": "2d5de64c99a41675664c52b1fcdd8016b76a18e2515cab6f881453aab5a6dcc2",
    "profile_2": "9e13d74e8a5d081b942a71042a72895b98a5a197dac01256809e71b2ec61a131",
    "recommend_2": "1f0f9032c6bb99c00450714d7648256cb1e672c939448f211fa170ff1de5c19d",
    "profile_3": "5006d6eddf8eb825d6112529eabb5f70c05ccfa265abf8f35fd15b317efc8fad",
    "recommend_3": "6dd29a2489b2f05860092e5f5330073779a09c626818c2f5ec5a2dccb5015e1b",
    "profile_4": "5500b9357b3fcb8ca17940d5eb55f1678766a4e03ab94611ba2ba09190727eb5",
    "recommend_4": "a20a3a7b6dbdcc08b0646fe078ec8e6ce23f352d011e654d526c39f850a292b5",
    "profile_5": "1c9ed7cd599d45e992cdbbb96edd89f199821d904718b20e7eb91b52bfc6ea22",
    "recommend_5": "bf7ac0c31d6ae3ae3659a75c3a09fcd019d5c08539784bf4e82411cbb7789f3c",
    "profile_6": "ce206d1fe44545e564027b5e5829393f2f223166e5f1506118946336a44b891b",
    "recommend_6": "a242d61f1bd01b9472615fc2b39dd445acdea7646ab0535e7f0acdf6339a9fa6",
    "profile_7": "653c725c7675babaeff1a5e8cf8db5d878c2ddac8605e2687c39ab17f79abd12",
    "recommend_7": "287854e778d71ed00d2d07fc77976440f30adf05b0724b610d26c935928d1fe6",
}


class Characterization(unittest.TestCase):
    """Digests captured on HEAD before any VAL-613 refactor."""

    maxDiff = None

    def test_recommend_grid(self):
        """recommend()/infer_job() output over the profile grid is unchanged."""
        self.assertEqual(recommend_digests(), EXPECTED_RECOMMEND)

    @unittest.skipIf(pd is None, "pandas not installed")
    def test_profile_frame(self):
        """profile_frame() and recommend(df) over the frame corpus are unchanged."""
        self.assertEqual(frame_digests(), EXPECTED_FRAMES)


if __name__ == "__main__":
    if "--print-digests" in sys.argv:
        print(json.dumps(recommend_digests(), indent=1))
        print(json.dumps(frame_digests(), indent=1))
        sys.exit(0)
    unittest.main()
