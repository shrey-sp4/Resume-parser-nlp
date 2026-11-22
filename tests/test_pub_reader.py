"""The learned publication-field reader, on made-up citations only."""

from __future__ import annotations

import pytest

pytest.importorskip("sklearn")

import pickle  # noqa: E402
import random  # noqa: E402
import re  # noqa: E402

import pub_reader_loo as loo  # noqa: E402
from resume_parser.entities import publications as pub  # noqa: E402
from resume_parser.learn import pub_reader as pr  # noqa: E402
from resume_parser.schema import REPO_ROOT, DataDirError, load_thresholds  # noqa: E402

PARAMS = load_thresholds()["stage5_entities"]
SYLLABLES = ("zor", "blax", "quen", "vil", "trab", "nox", "frel", "dum", "pash", "kiv", "orb", "lune")


def _word(rng: random.Random) -> str:
    return "".join(rng.choice(SYLLABLES) for _ in range(rng.randint(2, 3)))


def _citation(rng: random.Random, kind: str) -> tuple[str, dict]:
    authors = ", ".join(f"{_word(rng).capitalize()}, {rng.choice('ABCDEFGH')}." for _ in range(rng.randint(1, 3)))
    title = " ".join(_word(rng) for _ in range(rng.randint(4, 8))).capitalize()
    if kind == "journal":
        venue = f"Journal of {_word(rng).capitalize()} {_word(rng).capitalize()}"
    else:
        venue = f"Proceedings of the {_word(rng).capitalize()} Conference"
    year = str(rng.randint(1995, 2022))
    text = f"{authors} {title}. {venue}, vol. {rng.randint(1, 40)}, pp. {rng.randint(1, 90)}-{rng.randint(91, 200)}, {year}."
    return text, {"title": title, "venue": venue, "year": year, "type": kind}


def _entry(text: str, k: int, sub_type=None) -> dict:
    line = {"id": f"l{k}", "text": text, "page": 1, "bold": 0.0, "pub_sec_type": None, "pub_sub_type": sub_type}
    parsed = pub.parse_entry([line], PARAMS)["entry"]
    return {"lines": [line], "rules": parsed}


def _resume(seed: int, n: int = 8, group: str = "dev") -> dict:
    rng = random.Random(seed)
    gt, entries = [], []
    for k in range(n):
        text, g = _citation(rng, "journal" if k % 2 else "conference")
        gt.append(g)
        entries.append(_entry(text, k))
    return {"group": group, "gt": gt, "entries": entries}


def _input_words(resumes) -> set[str]:
    """The made-up words of the fake citations (the fixed template words such as "vol" and
    "journal" are left out, as feature names may hold them)."""
    words = set()
    for r in resumes:
        for e in r["entries"]:
            words.update(w.lower() for w in re.findall(r"[A-Za-z]{3,}", e["lines"][0]["text"]))
    return words - {"vol", "journal", "proceedings", "the", "conference"}


def _models(resumes):
    return loo.train_on([loo.prepare(r, PARAMS) for r in resumes])


# ---------------------------------------------------------------- privacy


def _check_value(v):
    if isinstance(v, str):
        assert all(part in pr.FEATURE_VALUES for part in v.split("|")), v
    else:
        assert v == 1


def test_features_hold_shapes_only():
    resumes = [_resume(s) for s in range(3)]
    for r in resumes:
        for e in r["entries"]:
            tok = pr.tokenize(e)
            for f in pr.token_features(e, tok, PARAMS):
                assert set(f) <= pr.TOKEN_KEYS
                for v in f.values():
                    _check_value(v)
            f = pr.type_features(e, tok, PARAMS)
            assert set(f) <= pr.TYPE_KEYS
            for v in f.values():
                _check_value(v)


def test_trained_model_holds_no_words():
    resumes = [_resume(s) for s in range(3)]
    models = _models(resumes)
    for key, allowed in (("tag", pr.TOKEN_KEYS), ("type", pr.TYPE_KEYS)):
        vec = models[key][0]
        for name in vec.feature_names_:
            k, _, v = name.partition("=")
            assert k in allowed, name
            if v:
                _check_value(v)
    blob = pickle.dumps(models).lower()
    for word in _input_words(resumes):
        assert word.encode() not in blob, word


# ---------------------------------------------------------------- weak labels


def test_weak_labels_find_title_venue_year():
    text = "Quen, A., Vil, B. Trabnox frelzor dumpash kivorb. Journal of Lunezor Blax, vol. 3, pp. 1-9, 2011."
    entry = _entry(text, 0)
    tok = pr.tokenize(entry)
    labels, reason = pr.weak_labels(tok, {"title": "Trabnox frelzor dumpash kivorb", "venue": "Journal of Lunezor Blax",
                                          "year": "2011"})
    assert reason == "ok"
    words = {lab: " ".join(t for (t, _, _), l in zip(tok["tokens"], labels) if l == lab and t.isalnum())
             for lab in pr.LABELS}
    assert words["TITLE"] == "Trabnox frelzor dumpash kivorb"
    assert words["VENUE"] == "Journal of Lunezor Blax"
    assert words["YEAR"] == "2011"
    assert words["AUTHOR"] == "Quen A Vil B"


def test_weak_labels_skip_with_a_reason():
    text = "Quen, A. Trabnox frelzor dumpash kivorb. Journal of Lunezor Blax, 2011."
    tok = pr.tokenize(_entry(text, 0))
    assert pr.weak_labels(tok, {"title": "Orbnox vilquen", "venue": "", "year": ""})[1] == "title not found"
    assert pr.weak_labels(tok, {"title": "Trabnox frelzor dumpash kivorb", "venue": "Annals of Zorvil",
                                "year": ""})[1] == "venue not found"
    assert pr.weak_labels(tok, {"title": "Trabnox frelzor dumpash kivorb", "venue": "",
                                "year": "1999"})[1] == "year not found"
    twice = pr.tokenize(_entry("Trabnox frelzor dumpash. Trabnox frelzor dumpash, 2011.", 0))
    assert pr.weak_labels(twice, {"title": "Trabnox frelzor dumpash", "venue": "", "year": ""})[1] == "title ambiguous"


# ---------------------------------------------------------------- determinism and reading


def test_training_is_deterministic():
    resumes = [_resume(s) for s in range(3)]
    a, b = _models(resumes), _models(resumes)
    for key in ("tag", "type"):
        assert a[key][0].feature_names_ == b[key][0].feature_names_
        assert (a[key][1].coef_ == b[key][1].coef_).all()
    held = _resume(9)
    assert [pr.read_entry(a, e, PARAMS) for e in held["entries"]] == \
        [pr.read_entry(b, e, PARAMS) for e in held["entries"]]


def test_reader_returns_text_as_printed_and_a_plausible_year():
    models = _models([_resume(s) for s in range(4)])
    for e in _resume(11)["entries"]:
        out = pr.read_entry(models, e, PARAMS)
        text = pr.entry_text(e["lines"])
        for field in ("title", "venue"):
            assert out[field] in text
        assert out["year"] == "" or 1950 <= int(out["year"]) <= PARAMS["current_year"] + PARAMS["year_max_offset"]
        assert out["type"] in pr.TYPES
        assert all(0.0 <= c <= 1.0 for c in out["conf"].values())


def test_combined_uses_learned_only_above_the_threshold():
    rules = {"title": "r", "venue": "rv", "year": "2001", "type": "journal"}
    learned = {"title": "l", "venue": "lv", "year": "2002", "type": "conference",
               "conf": {"title": 0.9, "venue": 0.5, "year": 0.8, "type": 0.79}}
    assert pr.combine(rules, learned, 0.8) == {"title": "l", "venue": "rv", "year": "2002", "type": "journal"}


# ---------------------------------------------------------------- leave one out


def test_leave_one_out_runs_on_a_tiny_set():
    resumes = [_resume(s, n=6, group=("dev", "tuned", "dev2")[s % 3]) for s in range(4)]
    a = loo.run_loo(resumes, PARAMS, thresholds=(0.8,))
    b = loo.run_loo(resumes, PARAMS, thresholds=(0.8,))
    assert a == b
    rules, learned = a["counts"][("combined", "rules")], a["counts"][("combined", "learned")]
    assert rules["gt"] == learned["gt"] == 24
    assert rules["pred"] == learned["pred"] == 24
    assert rules["pairs"] == learned["pairs"] > 0
    assert set(loo.systems((0.8,))) == {name for (_g, name) in a["counts"]}
    assert sum(c["matched"] for c in a["labels"].values()) == rules["pairs"]


def test_leave_one_out_refuses_guarded_groups(capsys):
    assert loo.main(["--groups", "validation"]) == 2
    assert loo.main(["--groups", "dev,test_frozen"]) == 2


# ---------------------------------------------------------------- saving


def test_model_is_written_only_under_the_data_folder(data_dir):
    models = _models([_resume(s) for s in range(3)])
    path = pr.save_model(models)
    assert path == (data_dir / "models" / pr.MODEL_FILE).resolve() and path.is_file()
    for bad in (REPO_ROOT / "models" / pr.MODEL_FILE, data_dir / "reports" / pr.MODEL_FILE):
        with pytest.raises(DataDirError):
            pr.save_model(models, bad)
    assert not list(REPO_ROOT.rglob(pr.MODEL_FILE))
