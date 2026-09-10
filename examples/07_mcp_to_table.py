# -*- coding: utf-8 -*-
"""Bridge the two surfaces: take MCP search results and load them into a Clay table.

The MCP (sourcing) and the REST API (tables) share no data model. An MCP search returns a taskId and
entityIds; a table holds t_/r_ ids. Nothing joins them — you carry the results across, which is what this
does. See README section 4b.

The MCP tools run inside the AI assistant, not in Python, so the flow is:

  1. In the assistant:  "find product managers at example.com in the United States"
     -> the MCP returns a `contacts` array
  2. Save that array as JSON (or paste it into a file)
  3. Run this script to create the table, columns and rows

usage: python examples/07_mcp_to_table.py contacts.json "My New Table"
"""
import sys, os, io, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clay import Clay

# MCP contact field -> the column name we want in the table
FIELDS = [
    ("name",                          "full_name"),
    ("latest_experience_title",       "title"),
    ("latest_experience_company",     "company"),
    ("latest_experience_start_date",  "role_started"),
    ("url",                           "linkedin_url"),
    ("location_name",                 "location"),
    ("domain",                        "company_domain"),
    ("entityId",                      "clay_entity_id"),   # keep it: your only link back to the search
]

if len(sys.argv) < 3:
    sys.exit(__doc__)
contacts = json.load(io.open(sys.argv[1], encoding="utf-8"))
if isinstance(contacts, dict):                 # accept a whole MCP response or just the array
    contacts = contacts.get("contacts", [])
if not contacts:
    sys.exit("no contacts in that file")

c = Clay()
table = c.create_table(sys.argv[2])            # auto-run OFF, so nothing paid fires on insert
print("created table", table)

cols = {}
for src, name in FIELDS:
    cols[src] = c.add_text(table, name)

rows = [{cols[src]: str(p.get(src) or "") for src, _ in FIELDS} for p in contacts]
ids = c.insert(table, rows)
print("inserted %d rows" % len(ids))

# A free formula, to show the pattern: a stable join key for downstream tools.
c.add_formula(table, "lead_key",
              '(({{%s}}||"") + ":" + ({{%s}}||"")).toLowerCase().replace(/[^a-z0-9:]+/g,"-")'
              % (cols["domain"], cols["name"]))

print("\nnext: add enrichment/AI columns by CLONING a working one (README section 5),")
print("gate them (section 5), preflight, then run one row before the batch (section 6).")
print("table id:", table)
