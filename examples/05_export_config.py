# -*- coding: utf-8 -*-
"""Export live prompts and formulas to files, so a repo can be the source of truth. Read-only, free.

Prompts are stored as JS concatenations of string literals and chips; this reconstructs them into
readable text with {{Column Name}} placeholders. Run it before you start editing anything, and again
whenever someone has been working in the UI.

usage: python examples/05_export_config.py "Table" [output_dir]
"""
import sys, os, io, json, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clay import Clay

TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|Clay\.formatForAIPrompt\(\s*\{\{[^}]+\}\}\s*\)|\{\{[^}]+\}\}')


def deref(text, names):
    """{{f_abc}} -> {{Column Name}}"""
    return re.sub(r"\{\{(f_[A-Za-z0-9]+)\}\}",
                  lambda m: "{{%s}}" % names.get(m.group(1), m.group(1)), text or "")


def unwrap(expr):
    """Rebuild a prompt from its JS concatenation into readable text."""
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
    """The editable body of a column: a formula expression, or an action column's prompt."""
    ts = field.get("typeSettings") or {}
    if field["type"] == "formula":
        return deref(ts.get("formulaText", ""), names), "js"
    for b in ts.get("inputsBinding") or []:
        if b.get("name") in ("prompt", "userPrompt", "instructions"):
            return unwrap(deref(b.get("formulaText", ""), names)), "txt"
    return None, None


if len(sys.argv) < 2:
    sys.exit(__doc__)
c = Clay()
tid = c.find_table(sys.argv[1])
outdir = sys.argv[2] if len(sys.argv) > 2 else "clay_config"
os.makedirs(outdir, exist_ok=True)

full = c.table(tid)
names = {f["id"]: f["name"] for f in full["fields"]}
manifest = []
for f in full["fields"]:
    body, kind = body_of(f, names)
    if not body:
        continue
    safe = re.sub(r"[^A-Za-z0-9]+", "_", f["name"]).strip("_").lower()
    path = os.path.join(outdir, "%s.%s" % (safe, kind))
    io.open(path, "w", encoding="utf-8").write(body.strip() + "\n")
    ts = f.get("typeSettings") or {}
    manifest.append({"column": f["name"], "file": os.path.basename(path), "type": f["type"],
                     "action": ts.get("actionKey"), "account": ts.get("authAccountId"),
                     "run_condition": deref(ts.get("conditionalRunFormulaText") or "", names) or None})
    print("wrote %-44s <- %s" % (os.path.basename(path), f["name"][:44]))

io.open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8").write(
    json.dumps({"table": full["name"], "table_id": tid, "columns": manifest}, indent=1) + "\n")
print("\n%d columns exported to %s/ (plus manifest.json)" % (len(manifest), outdir))
