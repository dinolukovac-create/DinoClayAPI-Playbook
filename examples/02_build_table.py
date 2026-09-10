# -*- coding: utf-8 -*-
"""Create a table with each column type, insert rows, and tear it down. Costs nothing unless you run.

Demonstrates the safe construction pattern: auto-run off, formulas free, action columns CLONED from a
column that already works (hand-assembled AI columns silently never execute).

usage: python examples/02_build_table.py "Existing Table" "A Working AI Column"
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clay import Clay, ClayError

c = Clay()
src_table = c.find_table(sys.argv[1]) if len(sys.argv) > 1 else None
src_col = sys.argv[2] if len(sys.argv) > 2 else None

t = c.create_table("DEMO scratch DELETEME")          # auto-run OFF by default
print("created", t, c.table(t)["tableSettings"])
try:
    company = c.add_text(t, "company")
    country = c.add_text(t, "country")
    # free, instant, recomputes itself
    qualified = c.add_formula(t, "qualified",
                              '(({{%s}}||"").length > 0 && {{%s}} !== "XX")' % (company, country))

    if src_table and src_col:
        ai = c.clone_column(t, "classification", src_table, src_col, bindings={
            "prompt": '"Company: " + Clay.formatForAIPrompt({{%s}}) + '
                      '". Reply ONLY {\\"consumer\\": true}"' % company,
        })
        c.set_run_condition(t, ai, '{{%s}}===true' % qualified, "only qualified rows")
        print("cloned AI column, gated:", bool(c.run_condition_of(t, "classification")))
        print("preflight:", c.preflight(t, ["classification"], scope=src_table) or "clear")
    else:
        print("(pass a source table + working AI column to demo cloning)")

    ids = c.insert(t, [{company: "Acme Consumer Co", country: "GB"},
                       {company: "Globex Industrial", country: "US"},
                       {company: "", country: "XX"}])
    print("inserted:", ids)
    for r in c.rows(t):
        print("   ", {k: v for k, v in r.items() if not k.endswith("At")})
finally:
    c.delete_table(t)
    print("deleted scratch table")
