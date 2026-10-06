#!/usr/bin/env python3
"""Typed accessors over tools/viz/palette.json - the dataviz reference palette as data.

Pure stdlib. Every value comes from palette.json (itself transcribed from the
skill's references/palette.md); this module never carries a hex literal.

    from tools.viz import palette
    palette.categorical("light")        # 8 hex, slot order (never re-order)
    palette.categorical("dark", 3)      # first three slots (all-pairs cap)
    palette.sequential()                # default hue ramp, light -> dark
    palette.ordinal("dark")             # ramp steps inside ordinal_bounds, >= 100 apart
    palette.diverging("light")          # (low, mid, high)
    palette.css_tokens("dark")          # "--surface-1: #1a1a19;" lines
"""
import copy
import functools
import json
from pathlib import Path

PALETTE_PATH = Path(__file__).with_name("palette.json")
MODES = ("light", "dark")
MAX_SERIES = 8
ORDINAL_MIN_GAP = 100  # min ramp-step distance between adjacent ordinal colors


@functools.lru_cache(maxsize=1)
def _load_cached():
    with PALETTE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def load():
    """Return the whole palette as a dict (file read once; callers get a private copy)."""
    return copy.deepcopy(_load_cached())


def _mode(mode):
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    return _load_cached()["modes"][mode]


def categorical(mode="light", n=None):
    """Categorical slots in fixed order. n=None returns all; n > 8 is refused."""
    slots = list(_mode(mode)["categorical"])
    if n is None:
        return slots
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ValueError(f"n must be a positive int, got {n!r}")
    if n > MAX_SERIES:
        raise ValueError(
            f"{n} series requested but the palette has {MAX_SERIES} categorical slots: "
            "fold the smallest series into 'Other' or facet - never cycle colors")
    return slots[:n]


def categorical_hues():
    """Hue family name per slot, parallel to categorical()."""
    return list(_load_cached()["categorical_hues"])


def all_pairs_cap():
    """Max series for all-pairs chart forms (scatter, bubble, choropleth, small multiples)."""
    return _load_cached()["categorical_all_pairs_cap"]


def ramp(hue=None):
    """{step_name: hex} for a hue, in light -> dark step order."""
    data = _load_cached()
    hue = hue or data["sequential_default"]
    ramps = data["ramps"]
    if hue not in ramps:
        raise ValueError(f"no sequential ramp for hue {hue!r}; available: {sorted(ramps)}")
    steps = ramps[hue]
    return {k: steps[k] for k in sorted(steps, key=int)}


def sequential(hue=None, steps=None):
    """Hex list light -> dark for a hue ramp; steps = optional subset of step names (int or str)."""
    full = ramp(hue)
    if steps is None:
        return list(full.values())
    out = []
    for s in steps:
        key = str(s)
        if key not in full:
            raise ValueError(f"step {s!r} not in ramp; available: {list(full)}")
        out.append(full[key])
    return out


def ordinal_steps(mode="light", hue=None, min_gap=None):
    """Ramp step names for an ordinal scale in one mode, light -> dark.

    Bounds come from palette.json ``ordinal_bounds[mode]``: ``min_step`` caps the
    light end, ``max_step`` the dark end; a missing end is the ramp end. Inside
    the bounds, steps are taken from the light bound onward, each at least
    ``min_gap`` (default ORDINAL_MIN_GAP = 100) past the previous one.
    """
    _mode(mode)
    gap = ORDINAL_MIN_GAP if min_gap is None else min_gap
    bounds = _load_cached()["ordinal_bounds"][mode]
    names = list(ramp(hue))
    lo = int(bounds.get("min_step", names[0]))
    hi = int(bounds.get("max_step", names[-1]))
    picked = []
    for name in names:
        step = int(name)
        if lo <= step <= hi and (not picked or step - int(picked[-1]) >= gap):
            picked.append(name)
    return picked


def ordinal(mode="light", hue=None):
    """Hex list for an ordinal scale: ordinal_steps(mode) of the hue ramp, light -> dark.

    The list is short (light 5, dark 6 with the current palette.json). Vega-Lite
    ``range.ordinal`` repeats colors when a field has more levels than the list
    has entries - bin or fold above that count instead of relying on the cycle.
    """
    return sequential(hue, steps=ordinal_steps(mode, hue))


def diverging(mode="light"):
    """(low, mid, high) for the blue <-> red diverging pair with the mode's neutral midpoint."""
    _mode(mode)
    d = _load_cached()["diverging"][mode]
    return d["low"], d["mid"], d["high"]


def status(mode="light"):
    """{good, warning, serious, critical} - fixed, never themed."""
    return dict(_mode(mode)["status"])


def surface(mode="light"):
    """Surface-level tokens: surface, surface_alt (page plane), grid, axis, border."""
    m = _mode(mode)
    return {k: m[k] for k in ("surface", "surface_alt", "grid", "axis", "border")}


def text(mode="light"):
    """Ink tokens: primary, secondary, muted, success."""
    return dict(_mode(mode)["text"])


def texture():
    return copy.deepcopy(_load_cached()["texture"])


def font():
    return copy.deepcopy(_load_cached()["font"])


def tokens(mode="light"):
    """Flat {css-var-name: value} map for one mode (the source for css_tokens)."""
    out = {}
    sf = surface(mode)
    out["surface-1"] = sf["surface"]
    out["surface-2"] = sf["surface_alt"]
    for k, v in text(mode).items():
        out[f"text-{k}"] = v
    out["grid"] = sf["grid"]
    out["axis"] = sf["axis"]
    out["border"] = sf["border"]
    for i, c in enumerate(categorical(mode), start=1):
        out[f"series-{i}"] = c
    for k, v in status(mode).items():
        out[f"status-{k}"] = v
    for step, c in ramp().items():
        out[f"seq-{step}"] = c
    low, mid, high = diverging(mode)
    out["div-low"], out["div-mid"], out["div-high"] = low, mid, high
    return out


def css_tokens(mode="light"):
    """CSS custom-property declarations, one per line: ``--name: value;``."""
    return "\n".join(f"--{k}: {v};" for k, v in tokens(mode).items() if v is not None)


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for m in MODES:
        print(f"/* {m} */")
        print(css_tokens(m))
