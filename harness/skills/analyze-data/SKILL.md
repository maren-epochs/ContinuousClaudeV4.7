---
name: analyze-data
description: Data analysis loop via Ouros + run_python host bridge — "analyze this data", "explore this CSV", "plot this", "dataframe stats", "what's in this parquet"
user-invocable: true
---

Loop: locate → load → explore → transform → visualize → report. The ouros sandbox CANNOT
import pandas/numpy — all DataFrame work goes through `run_python(code, timeout=60)`,
which executes on host CPython (`py -3.13`: pandas, numpy, matplotlib, seaborn, scipy,
pyarrow, polars, sklearn, statsmodels, duckdb, openpyxl — declared in
`tools/requirements.txt`). It returns the stdout+stderr tail (~8KB cap).

**State model — internalize before writing any step:**

- `run_python` is STATELESS between calls — each call is a fresh host interpreter.
- Its cwd is pinned to the per-session work dir `C:\tmp\ouros-sandbox-output\{session}\`.
  Persist intermediate frames THERE: `df.to_parquet("step1.parquet")`, reload next call.
- The ouros session (`--session`) persists SANDBOX variables only. Keep the SMALL stuff
  there: file paths, stats dicts, findings strings. Never pull frames into the sandbox.
- Token doctrine: process inside `run_python`, print ONLY distilled results — `shape`,
  `dtypes`, `describe().round(2)`, `head(5)`, top-N `value_counts`. Never full frames.

**1 LOCATE.** Data inside the project or `C:\tmp\ouros` is readable now. Outside → add its
directory to `OUROS_DATA_ROOTS` (env or `~/.claude/.env`; os.pathsep-separated absolute
dirs, read-only).

**2 SESSION.** One session per analysis, slug named after the question:

```bash
py -3.13 tools/ouros_harness.py --file {scratch}/step1.py \
  --session {slug} --storage thoughts/shared/dives
```

Existing session loads by default; `--reset` wipes; `--list-vars` / `--get-var` inspect.

**3 LOAD + EXPLORE.** First step: read, checkpoint, print the compact profile.

```python
# step1.py — runs IN the sandbox; the triple-quoted code runs on host
out = run_python(r'''
import pandas as pd
df = pd.read_csv("C:/data/sales.csv")             # forward slashes: backslashes re-escape across the bridge
df.to_parquet("raw.parquet")                      # checkpoint in work dir (cwd)
print(df.shape)
print(df.dtypes.to_string())
print(df.describe().round(2).to_string())
print("nulls:", df.isna().sum().to_dict())
''')
print(out)
src = "C:/data/sales.csv"       # small stuff persists as sandbox vars
findings = {}
```

**4 TRANSFORM.** Each step reads the previous parquet, transforms, writes the next
(`clean.parquet`, `agg.parquet`, ...), prints one distilled sanity check. Store conclusions
in the sandbox `findings` dict as you go.

**5 VISUALIZE.** Any chart a person will read follows `/visualize` (form, color by job, palette
validation, house style, render + LOOK); the example below is only the mechanical path.
`savefig` to cwd — the harness diffs the output root after every run and
prints new files as `artifacts:` lines with absolute host paths. Report that path verbatim.

```python
out = run_python(r'''
import sys, pathlib, importlib.util as _u; _c = pathlib.Path.cwd(); _d = next((p/'tools'/'viz' for p in (_c, *_c.parents, pathlib.Path.home()/'.claude') if (p/'tools'/'viz'/'palette.json').exists()), None) or (_ for _ in ()).throw(ModuleNotFoundError('ccv_viz: no tools/viz/palette.json in cwd, its parents or ~/.claude - run py -3.13 install/sync_global.py --apply from the ccv47 repo')); _s = _u.spec_from_file_location('ccv_viz', _d/'__init__.py', submodule_search_locations=[str(_d)]); sys.modules['ccv_viz'] = _m = _u.module_from_spec(_s); _s.loader.exec_module(_m)
import pandas as pd, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from ccv_viz import style
style.apply_matplotlib("light")
df = pd.read_parquet("raw.parquet")
fig, ax = plt.subplots(figsize=(7, 5))
ax.scatter(df["x"], df["y"], s=10, alpha=0.6)
ax.set_xlabel("x"); ax.set_ylabel("y"); ax.set_title("y vs x")
fig.savefig("scatter.png", dpi=120, bbox_inches="tight")
print("saved scatter.png")
''')
print(out)
```

**6 REPORT.** Telegraphic artifact at `continuum/research/{topic}/findings.md`, same convention
as `/research`. Write it with the agent's Write tool — sandbox `write_file` only reaches the
output root and is denied for `continuum/`. Include sources + exclusions, data-quality issues,
distilled stats, artifact paths.

Give the user: the stats summary, the absolute PNG path(s) from `artifacts:`, the
findings.md path. Reusable discovery (API quirk, data gotcha) → `bloks new rule "<text>"
--tags {lib},...`.

**Failure modes:** `read_csv` PermissionError → path not under a readable root, fix
OUROS_DATA_ROOTS (step 1). Output ends `[truncated]` → you printed too much; distill
harder. `[exit code: N]` suffix → host traceback is in the tail, fix and rerun — the
parquet checkpoints mean you never re-pay the load. Spreadsheet-export CSV (banner rows,
`Unnamed: N` columns) → `header=None`, locate the header row by a known label, slice; check
for duplicated table blocks before aggregating.
