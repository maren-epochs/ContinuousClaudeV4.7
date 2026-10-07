"""tools.fleet - cross-session fleet view, harness guard data and proposal inbox.

Data contract: ``tools/fleet/schema.md`` and ``tools.fleet.model``. Stdlib only.
All fleet data lives under ``~/.claude``; only ``fleet.py apply`` writes into a repo
(the harness repo file a proposal targets, never committed).
"""
