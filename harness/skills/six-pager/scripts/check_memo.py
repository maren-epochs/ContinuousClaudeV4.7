"""Mechanical checks for a six-pager Markdown memo.

Usage: py -3.13 check_memo.py MEMO.md [--sources FILE ...] [--json]

Reports body length and page estimate, bullets outside goals/tenets/appendix,
weasel words, long sentences, [TBD...] placeholders, and numbers that do not
appear in the source files. Exit 0 when no FAIL line, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

WORDS_PER_PAGE = 550
MAX_BODY_WORDS = 3300
MIN_BODY_WORDS = 1200
MAX_LONG_SHARE = 0.10
LONG_SENTENCE = 30
MAX_WEASEL = 3

WEASEL = [
    "significantly",
    "significant",
    "substantially",
    "substantial",
    "very",
    "extremely",
    "nearly",
    "many",
    "several",
    "various",
    "numerous",
    "robust",
    "seamless",
    "seamlessly",
    "world-class",
    "best-in-class",
    "synergy",
    "synergies",
    "should help",
    "could potentially",
    "it is believed",
    "leverage",
    "leveraging",
    "a lot",
    "lots of",
    "huge",
    "massive",
    "incredibly",
    "really",
]
BULLET_OK = re.compile(r"goal|tenet|appendix|faq", re.IGNORECASE)
BODY_END = re.compile(r"^#{1,3}\s*(appendix|faq|frequently asked)", re.IGNORECASE)
HEADING = re.compile(r"^(#{1,6})\s+(.*)")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
NUMBER = re.compile(r"(?<![\w.])[$£€]?\d[\d,]*(?:\.\d+)?\s?(?:%|[KkMmBb]\b)?")
PLACEHOLDER = re.compile(r"\[TBD[^\]]*\]", re.IGNORECASE)


def split_body(lines: list[str]) -> tuple[list[str], list[str]]:
    """Split lines into (body, appendix) at the first Appendix/FAQ heading."""
    for i, line in enumerate(lines):
        if BODY_END.match(line.strip()):
            return lines[:i], lines[i:]
    return lines, []


def prose_lines(lines: list[str]) -> list[str]:
    """Non-empty body lines that are not headings, tables or code fences."""
    out = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith(("|", "```")) or HEADING.match(s):
            continue
        out.append(s)
    return out


def words(text: str) -> int:
    """Word count."""
    return len(re.findall(r"[A-Za-z0-9$%][\w'%$.,/-]*", text))


def sentences(text: str) -> list[str]:
    """Sentences of more than two words."""
    text = PLACEHOLDER.sub("TBD", text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(\[])", text)
    return [p for p in parts if words(p) > 2]


MULT = {"k": 1e3, "m": 1e6, "b": 1e9}
ROUND_TOL = 0.005  # 18,100 traces to a ledger value of 18,102


def num_value(tok: str) -> tuple[float, float] | None:
    """(raw, scaled) value of a number token: currency, commas and % dropped, K/M/B applied."""
    t = re.sub(r"[$£€,%\s]", "", tok)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KkMmBb]?)", t)
    if not m:
        return None
    raw = float(m.group(1))
    return raw, raw * MULT.get(m.group(2).lower(), 1.0)


def canon(v: float) -> str:
    """Canonical text of a value, so 44K, 44,000 and 44000.0 compare equal."""
    return str(int(v)) if float(v).is_integer() else f"{v:.4f}".rstrip("0").rstrip(".")


def untraced(text: str, sources: list[Path]) -> list[str]:
    """Numbers in text that appear in no source file (small counts and years skipped)."""
    src = " ".join(p.read_text(encoding="utf-8", errors="replace") for p in sources)
    # Sources mix CSV commas and thousands separators: take both readings.
    thousands = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", src)
    src_vals: set[str] = set()
    for t in re.findall(r"\d+(?:\.\d+)?", thousands) + re.findall(
        r"\d+(?:\.\d+)?", src
    ):
        src_vals.add(canon(float(t)))
    for m in NUMBER.finditer(thousands):
        nv = num_value(m.group())
        if nv:
            src_vals.add(canon(nv[1]))
    big = [float(v) for v in src_vals if float(v) >= 100]
    seen, out = set(), []
    for m in NUMBER.finditer(PLACEHOLDER.sub(" ", text)):
        nv = num_value(m.group())
        if nv is None:
            continue
        raw, scaled = nv
        key = canon(scaled)
        if key in seen:
            continue
        seen.add(key)
        whole = float(raw).is_integer()
        if whole and raw <= 10 and scaled == raw:
            continue  # counts, quarters, list numbers
        if whole and 1990 <= raw <= 2100 and scaled == raw:
            continue  # years
        if key in src_vals or canon(raw) in src_vals:
            continue
        if scaled >= 100 and any(abs(scaled - v) <= ROUND_TOL * v for v in big):
            continue  # a rounded presentation of a source or ledger value
        out.append(m.group().strip())
    return out


def check(memo: Path, sources: list[Path]) -> dict[str, Any]:
    """All checks for one memo, with fail and warn lists."""
    lines = memo.read_text(encoding="utf-8").splitlines()
    body, appendix = split_body(lines)
    prose = prose_lines(body)
    body_text = " ".join(prose)
    table_words = sum(words(l) for l in body if l.strip().startswith("|"))
    body_words = words(body_text) + table_words

    section, stray = "", []
    for line in body:
        h = HEADING.match(line.strip())
        if h:
            section = h.group(2)
            continue
        if BULLET.match(line) and not BULLET_OK.search(section):
            stray.append(f"{section or '(top)'}: {line.strip()[:70]}")

    low = " " + re.sub(r"\s+", " ", body_text.lower()) + " "
    weasel = {}
    for w in WEASEL:
        n = len(re.findall(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", low))
        if n:
            weasel[w] = n
    # One line = at least one sentence: bullets without a final period stay separate.
    sents = [s for line in prose for s in sentences(BULLET.sub("", line))]
    long_s = [s for s in sents if words(s) > LONG_SENTENCE]
    avg = sum(words(s) for s in sents) / len(sents) if sents else 0.0
    full = "\n".join(lines)
    res: dict[str, Any] = {
        "body_words": body_words,
        "pages_est": round(body_words / WORDS_PER_PAGE, 1),
        "appendix_present": bool(appendix),
        "stray_bullets": stray,
        "weasel_words": weasel,
        "sentences": len(sents),
        "avg_sentence_words": round(avg, 1),
        "long_sentences": len(long_s),
        "long_examples": [s[:120] for s in long_s[:3]],
        "placeholders": sorted(set(PLACEHOLDER.findall(full))),
        "untraced_numbers": untraced(full, sources) if sources else None,
    }
    fails, warns = [], []
    if body_words > MAX_BODY_WORDS:
        fails.append(
            f"body {body_words} words (~{res['pages_est']} pages) exceeds {MAX_BODY_WORDS}"
        )
    if body_words < MIN_BODY_WORDS:
        warns.append(f"body only {body_words} words; thin for a six-pager")
    if stray:
        fails.append(f"{len(stray)} bullet line(s) outside goals/tenets/appendix")
    if sum(weasel.values()) > MAX_WEASEL:
        fails.append(f"{sum(weasel.values())} weasel words: {weasel}")
    elif weasel:
        warns.append(f"weasel words: {weasel}")
    if sents and len(long_s) / len(sents) > MAX_LONG_SHARE:
        fails.append(f"{len(long_s)}/{len(sents)} sentences over {LONG_SENTENCE} words")
    if not appendix:
        warns.append("no Appendix/FAQ heading found")
    if res["untraced_numbers"]:
        warns.append(
            f"{len(res['untraced_numbers'])} number(s) not in sources (computed or invented?): "
            + ", ".join(res["untraced_numbers"][:25])
        )
    if res["placeholders"]:
        warns.append(
            f"{len(res['placeholders'])} placeholder(s) to fill: "
            + "; ".join(res["placeholders"])
        )
    res["fail"], res["warn"] = fails, warns
    return res


def main() -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("memo", type=Path)
    ap.add_argument("--sources", type=Path, nargs="*", default=[])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = check(a.memo, a.sources)
    if a.json:
        print(json.dumps(r, indent=2))
    else:
        print(
            f"body: {r['body_words']} words, ~{r['pages_est']} pages; "
            f"{r['sentences']} sentences, avg {r['avg_sentence_words']} words, "
            f"{r['long_sentences']} over {LONG_SENTENCE}"
        )
        for f in r["fail"]:
            print("FAIL " + f)
        for w in r["warn"]:
            print("WARN " + w)
        for s in r["stray_bullets"][:10]:
            print("  bullet: " + s)
        for s in r["long_examples"]:
            print("  long: " + s)
        if not r["fail"]:
            print("OK no FAIL lines")
    return 1 if r["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
