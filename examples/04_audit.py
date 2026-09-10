# -*- coding: utf-8 -*-
"""Workspace cost-risk audit: auto-run state and ungated paid columns. Read-only, free.

The dangerous combination is auto-run ON together with ungated action columns: paid columns fire
whenever rows land or a column is edited.

usage: python examples/04_audit.py [--rows]
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clay import Clay, ClayError

ROWS = "--rows" in sys.argv
c = Clay()
tables = c.list_tables()
print("=== %d tables in workspace %s" % (len(tables), c.workspace))
print("%-32s %-9s %6s %7s %8s  %s" % ("table", "auto-run", "cols", "action", "ungated", "rows"))
risky = []
for t in tables:
    try:
        full = c.table(t["id"])
    except ClayError as e:
        print("%-32s ERROR %s" % (t["name"][:32], str(e)[-50:])); continue
    auto = (full.get("tableSettings") or {}).get("AUTO_RUN_ON")
    actions = [f for f in full["fields"] if f["type"] == "action"]
    ungated = [f for f in actions if not (f.get("typeSettings") or {}).get("conditionalRunFormulaText")]
    n = ""
    if ROWS:
        try:
            got = len(c.records(t["id"], limit=1000))
            n = ">=1000" if got == 1000 else str(got)
        except ClayError:
            n = "?"
    print("%-32s %-9s %6d %7d %8d  %s" % (t["name"][:32], "ON" if auto else "off",
                                          len(full["fields"]), len(actions), len(ungated), n))
    if auto and ungated:
        risky.append((t["name"], len(ungated)))

print("\n=== COST RISK")
if risky:
    for name, n in risky:
        print("  %-34s auto-run ON with %d ungated paid columns" % (name, n))
    print("\nTurn auto-run off, or gate those columns.")
else:
    print("  No table combines auto-run with ungated action columns.")
