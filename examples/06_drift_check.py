# -*- coding: utf-8 -*-
"""Diff live Clay columns against exported files. Read-only, free. Exit 1 on drift.

Run this at the start of any session touching an existing table. Prompts edited in the UI, or rewritten
by Clay's own AI builder, live only in Clay — a repo copy silently stops matching production.

usage: python examples/06_drift_check.py [config_dir] [--verbose]
"""
import sys, os, io, json, re, difflib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clay import Clay

# The prompt-parsing helpers are duplicated from 05_export_config.py on purpose: each example is meant
# to be readable and runnable on its own. If you build on this, move them into clay.py instead.

TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|Clay\.formatForAIPrompt\(\s*\{\{[^}]+\}\}\s*\)|\{\{[^}]+\}\}')
deref = lambda t, n: re.sub(r"\{\{(f_[A-Za-z0-9]+)\}\}",
                            lambda m: "{{%s}}" % n.get(m.group(1), m.group(1)), t or "")


def unwrap(expr):
    parts = []
    for tok in TOKEN.findall(expr or ""):
        if tok.startswith('"'):
            try:
                parts.append(json.loads(tok))
            except ValueError:
                parts.append(tok[1:-1].replace('\\"', '"').replace("\\n", "\n"))
        else:
            parts.append("{{%s}}" % re.search(r"\{\{([^}]+)\}\}", tok).group(1))
    return "".join(parts) if parts else (expr or "")


def body_of(field, names):
    ts = field.get("typeSettings") or {}
    if field["type"] == "formula":
        return deref(ts.get("formulaText", ""), names)
    for b in ts.get("inputsBinding") or []:
        if b.get("name") in ("prompt", "userPrompt", "instructions"):
            return unwrap(deref(b.get("formulaText", ""), names))
    return None


def norm(s, code=False):
    """Prompts: Clay flattens newlines, so collapse whitespace. Formulas: Clay minifies, so drop it."""
    s = s or ""
    return re.sub(r"\s+", "", s) if code else re.sub(r"\s+", " ", s).strip()


cfg = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "clay_config"
VERBOSE = "--verbose" in sys.argv
man = json.load(io.open(os.path.join(cfg, "manifest.json"), encoding="utf-8"))
c = Clay()
full = c.table(man["table_id"])
names = {f["id"]: f["name"] for f in full["fields"]}
by = {f["name"]: f for f in full["fields"]}

match = drift = missing = 0
for entry in man["columns"]:
    f = by.get(entry["column"])
    path = os.path.join(cfg, entry["file"])
    if f is None or not os.path.exists(path):
        print("  MISSING  %-44s %s" % (entry["column"][:44], "column gone" if f is None else "file gone"))
        missing += 1
        continue
    live = body_of(f, names) or ""
    src = io.open(path, encoding="utf-8").read()
    code = entry["file"].endswith(".js")
    if norm(live, code) == norm(src, code):
        match += 1
        print("  match    %s" % entry["column"][:44])
    else:
        drift += 1
        a, b = norm(src, code), norm(live, code)
        at = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b)))
        print("  DRIFT    %-44s file %d chars / clay %d, diverge at %d" % (entry["column"][:44], len(a), len(b), at))
        print("           file: ...%s..." % a[max(0, at - 50):at + 50])
        print("           clay: ...%s..." % b[max(0, at - 50):at + 50])
        if VERBOSE:
            for line in difflib.unified_diff(a.split(" "), b.split(" "), "file", "clay", lineterm="", n=5):
                print("      " + line)

    gate_live = deref((f.get("typeSettings") or {}).get("conditionalRunFormulaText") or "", names) or None
    if norm(gate_live or "") != norm(entry.get("run_condition") or ""):
        print("  GATE     %-44s manifest=%r live=%r" % (entry["column"][:44], entry.get("run_condition"), gate_live))
        drift += 1

print("\n%d match, %d drift, %d unresolved" % (match, drift, missing))
sys.exit(1 if (drift or missing) else 0)
