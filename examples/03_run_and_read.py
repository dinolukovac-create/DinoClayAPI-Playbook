# -*- coding: utf-8 -*-
"""Run a column safely: preflight, one row, inspect, then the batch. SPENDS CREDITS.

usage: python examples/03_run_and_read.py "Table" "Column" [max_rows]
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clay import Clay, ClayError

if len(sys.argv) < 3:
    sys.exit(__doc__)
c = Clay()
t = c.find_table(sys.argv[1])
col = sys.argv[2]
cap = int(sys.argv[3]) if len(sys.argv) > 3 else 5
fid = c.field_map(t)[col]

# 1. Never run over live data without checking the column can actually run.
bad = c.preflight(t, [col], scope=t)
if bad:
    sys.exit("PREFLIGHT FAILED:\n  " + "\n  ".join(bad))
print("preflight clear")

# 2. Only rows that have no value yet, so nothing existing is at risk.
blank = [r["_id"] for r in c.rows(t) if not str(r.get(col) or "").strip()][:cap]
if not blank:
    print("nothing blank to run; current statuses:", c.statuses(t, col)); sys.exit(0)
print("%d blank rows; running ONE first" % len(blank))

# 3. One row, verified, before the batch.
c.run_and_wait(t, fid, blank[:1], force=False, timeout=300)
cell = c.cell(t, blank[0], fid)
print("  status:", (cell.get("metadata") or {}).get("status"))
print("  value :", str(cell.get("value"))[:200])
if (cell.get("metadata") or {}).get("status") != "SUCCESS":
    sys.exit("first row did not succeed: stopping before the batch")

if len(blank) > 1:
    input("press enter to run the remaining %d rows... " % (len(blank) - 1))
    c.run_and_wait(t, fid, blank[1:], force=False)
print("final statuses:", c.statuses(t, col))
