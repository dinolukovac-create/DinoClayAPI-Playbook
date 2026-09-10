# -*- coding: utf-8 -*-
"""Connect, discover the workspace, and inspect a table. Read-only, free.

usage: python examples/01_explore.py [table name or id]
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clay import Clay

c = Clay()
print("workspace :", c.workspace)
print("acting as :", c.whoami().get("email"))

tables = c.list_tables()
print("\n%d tables:" % len(tables))
for t in tables:
    print("  %-34s %s" % (t["name"][:34], t["id"]))

target = sys.argv[1] if len(sys.argv) > 1 else (tables[0]["name"] if tables else None)
if not target:
    sys.exit(0)
tid = target if target.startswith("t_") else c.find_table(target)
full = c.table(tid)

print("\n=== %s" % full["name"])
print("auto-run  :", (full.get("tableSettings") or {}).get("AUTO_RUN_ON"))
print("views     :", ", ".join(v["name"] for v in full["views"]))
print("\n%-40s %-9s %s" % ("column", "type", "gate / provider"))
for f in full["fields"]:
    ts = f.get("typeSettings") or {}
    note = ""
    if f["type"] == "action":
        note = ts.get("actionKey", "?")
        note += "  GATED" if ts.get("conditionalRunFormulaText") else "  *** UNGATED ***"
    print("%-40s %-9s %s" % (f["name"][:40], f["type"], note))
