# -*- coding: utf-8 -*-
"""Portable Clay REST client. Company-agnostic: discovers the workspace and auth accounts itself.

Clay has no public API documentation. Everything here was established by probing the live API and is
verified working. Read README.md before extending it — especially the silent-failure section, because
Clay's characteristic failure is a 200 that quietly did less than you asked.

    from clay import Clay
    c = Clay()                                   # reads CLAY_API_KEY from env or .env
    for t in c.list_tables():
        print(t["id"], t["name"])

Dependencies: none. Standard library only, Python 3.6+.
"""
import io, os, re, json, time, urllib.request, urllib.error

API = "https://api.clay.com/v3"


class ClayError(RuntimeError):
    pass


class Clay(object):
    """A thin, defensive wrapper. Every method maps to a route verified against the live API."""

    def __init__(self, key=None, workspace=None):
        self.key = key or self._find_key()
        self._ai_account = None
        self.workspace = workspace or self.discover_workspace()

    # ---------- setup

    @staticmethod
    def _find_key():
        """CLAY_API_KEY from the environment, else a .env in this directory or its parents."""
        if os.environ.get("CLAY_API_KEY"):
            return os.environ["CLAY_API_KEY"].strip()
        here = os.path.dirname(os.path.abspath(__file__))
        for _ in range(4):
            path = os.path.join(here, ".env")
            if os.path.exists(path):
                for line in io.open(path, encoding="utf-8"):
                    if line.startswith("CLAY_API_KEY="):
                        return line.split("=", 1)[1].strip()
            here = os.path.dirname(here)
        raise ClayError("No CLAY_API_KEY. Export it, or put CLAY_API_KEY=... in a .env file.")

    def call(self, method, path, body=None, timeout=90):
        """Raw call.

        Auth is the bare key. NOT 'Bearer <key>' (403) and NOT an x-api-key header (401) — this trips
        up everyone who assumes a conventional REST API.
        """
        req = urllib.request.Request(
            API + path, method=method,
            headers={"Authorization": self.key, "Content-Type": "application/json"},
            data=json.dumps(body).encode() if body is not None else None)
        try:
            return json.load(urllib.request.urlopen(req, timeout=timeout))
        except urllib.error.HTTPError as e:
            raise ClayError("%s %s -> %s %s" % (method, path, e.code, e.read().decode()[:500]))

    def whoami(self):
        return self.call("GET", "/me")

    def discover_workspace(self):
        """Pull the workspace id out of the identity envelope at GET /v3.

        /me does NOT include it, and GET /workspaces is admin-only. The id is buried in the permission
        rules instead. If an account can see several, this returns the first — pass `workspace=` explicitly.
        """
        root = self.call("GET", "")
        ids = []
        for rule in ((root.get("auth") or {}).get("abilities") or {}).get("M") or []:
            wid = (rule.get("conditions") or {}).get("id")
            if isinstance(wid, int) and wid not in ids:
                ids.append(wid)
        if not ids:
            raise ClayError("Could not discover a workspace id; pass workspace=<id> yourself.")
        return ids[0]

    # ---------- tables

    def list_tables(self):
        return self.call("GET", "/workspaces/%s/tables" % self.workspace)["results"]

    def find_table(self, name):
        """First table whose name matches (case-insensitive, exact then prefix)."""
        ts = self.list_tables()
        for t in ts:
            if t["name"].lower() == name.lower():
                return t["id"]
        for t in ts:
            if t["name"].lower().startswith(name.lower()):
                return t["id"]
        raise ClayError("No table named %r" % name)

    def table(self, table_id):
        """The whole table in one call: settings, every field definition, and views.

        There is no /tables/{t}/fields route — this IS how you read columns. Expect a large response
        on a wide table (100KB+ is normal).
        """
        return self.call("GET", "/tables/%s" % table_id)["table"]

    def create_table(self, name, auto_run=False):
        """Create a table. auto_run defaults to False here on purpose — see set_auto_run."""
        t = self.call("POST", "/tables", {"name": name, "workspaceId": self.workspace,
                                          "type": "spreadsheet"})["table"]["id"]
        if not auto_run:
            self.set_auto_run(t, False)
        return t

    def delete_table(self, table_id):
        return self.call("DELETE", "/tables/%s" % table_id)

    def set_auto_run(self, table_id, on):
        """Auto-run is table-level. Clay defaults it ON for new tables.

        With it on, paid columns fire as soon as rows land or a column changes. Leave it off and run
        explicitly, so spend is always something you asked for.
        """
        s = dict(self.table(table_id)["tableSettings"] or {}, AUTO_RUN_ON=bool(on))
        self.call("PATCH", "/tables/%s" % table_id, {"tableSettings": s})
        return self.table(table_id)["tableSettings"]

    # ---------- fields

    def fields(self, table_id):
        return self.table(table_id)["fields"]

    def field_map(self, table_id):
        """name -> id. Formulas and prompts reference columns by id, never by name."""
        return {f["name"]: f["id"] for f in self.fields(table_id)}

    def id_map(self, table_id):
        """id -> name, for rendering a stored formula back into something readable."""
        return {f["id"]: f["name"] for f in self.fields(table_id)}

    def _add(self, table_id, body):
        r = self.call("POST", "/tables/%s/fields" % table_id, body)
        return (r.get("field") or r)["id"]        # creates come back wrapped as {"field": {...}}

    def add_text(self, table_id, name):
        return self._add(table_id, {"name": name, "type": "text",
                                    "typeSettings": {"dataTypeSettings": {"type": "text"}}})

    def add_formula(self, table_id, name, formula):
        """A single JS expression referencing columns as {{f_...}}.

        Clay formulas are expressions, not programs: no statements, no `const`. An arrow IIFE is the
        usual way to get local variables: (s => s ? s.trim() : "")({{f_abc}})
        """
        return self._add(table_id, {"name": name, "type": "formula",
                                    "typeSettings": {"dataTypeSettings": {"type": "text"},
                                                     "formulaType": "text", "formulaText": formula}})

    def clone_column(self, table_id, name, source_table, source_column, bindings=None, optional=None):
        """Create an action column by cloning a WORKING one, overriding named inputsBinding entries.

        This is the only reliable way to create AI and enrichment columns. Hand-assembled ones omit
        settings that Clay accepts and then silently never runs on. `bindings` maps binding name ->
        formulaText (a JS expression, so string literals must be quoted: '"hello"').

        `optional` is a list of field ids in the TARGET table whose chips may be blank. Clay blocks a
        whole row with ERROR_BLANK_TOKEN when a required chip is empty, and the cloned
        optionalPathsInInputs still points at the SOURCE table's field ids, which do nothing here.

        Raises if Clay fails to derive `inputFieldIds` — a column with none never runs (see
        prompt_literal / README section 3).
        """
        src = [f for f in self.fields(source_table) if f["name"] == source_column]
        if not src:
            raise ClayError("no column %r in table %s" % (source_column, source_table))
        ts = json.loads(json.dumps(src[0]["typeSettings"]))
        ts.pop("conditionalRunFormulaText", None)      # never inherit another column's gate
        ts.pop("conditionalRunFormulaPrompt", None)
        # NOTE: the source's optionalPathsInInputs points at the SOURCE table's field ids, which mean
        # nothing here. Left in place unless the caller passes `optional`, because clearing it or
        # replacing it with ids that are not real inputs produces a column that never runs at all.
        for b in ts.get("inputsBinding") or []:
            if bindings and b.get("name") in bindings:
                b["formulaText"] = bindings[b["name"]]
        fid = self._add(table_id, {"name": name, "type": "action", "typeSettings": ts})
        f = [x for x in self.fields(table_id) if x["id"] == fid][0]
        if not f.get("inputFieldIds"):
            raise ClayError(
                "%r created but Clay derived NO inputFieldIds, so it will never run. The usual cause "
                "is a non-ASCII character in the prompt literal -- build it with prompt_literal()." % name)
        if optional:
            real = set(f.get("inputFieldIds") or [])
            bogus = [i for i in optional if i not in real]
            if bogus:
                raise ClayError(
                    "%r: optional=%s includes ids that are not inputs of this prompt (%s). Marking a "
                    "non-input path optional yields a column that never runs. Optional ids must be a "
                    "subset of inputFieldIds." % (name, bogus, sorted(real)))
            self.update_field(table_id, fid, optionalPathsInInputs={"prompt": [[i] for i in optional]})
        return fid

    @staticmethod
    def prompt_literal(text, chips):
        """Turn prompt text with {{name}} placeholders into the JS concatenation Clay stores.

        `chips` maps placeholder name -> field id in the target table.

        CRITICAL: the literal chunks are serialised with ensure_ascii=False. json.dumps defaults to
        ensure_ascii=True, which turns smart quotes and em dashes into backslash-u escapes -- and those
        escapes break Clay's chip parser, so inputFieldIds comes back empty and the column silently
        never executes. Raw UTF-8 in the literal works; the escaped form does not.
        """
        out, last = [], 0
        for m in re.finditer(r"\{\{(\w+)\}\}", text):
            seg = text[last:m.start()]
            if seg:
                out.append(json.dumps(seg, ensure_ascii=False))
            name = m.group(1)
            if name not in chips:
                raise ClayError("prompt references {{%s}} but no chip was supplied for it" % name)
            out.append("Clay.formatForAIPrompt({{%s}})" % chips[name])
            last = m.end()
        if text[last:]:
            out.append(json.dumps(text[last:], ensure_ascii=False))
        return " + ".join(out)

    @staticmethod
    def truthy(field_id):
        """A gate expression for a FORMULA column holding a boolean.

        Formula columns come back as the STRING "true"/"false", so `{{x}}===true` never matches and
        the gated column silently never runs. Always compare as a string.
        """
        return 'String({{%s}}).toLowerCase()==="true"' % field_id

    def update_field(self, table_id, field_id, **type_settings):
        """Read-modify-write, because PATCH REPLACES typeSettings rather than merging it.

        Sending only the key you want to change is rejected. Verifies the change landed, since Clay
        drops settings it will not honour and still returns 200.
        """
        cur = [f for f in self.fields(table_id) if f["id"] == field_id]
        if not cur:
            raise ClayError("no field %s" % field_id)
        ts = json.loads(json.dumps(cur[0]["typeSettings"]))
        ts.update(type_settings)
        self.call("PATCH", "/tables/%s/fields/%s" % (table_id, field_id), {"typeSettings": ts})
        return self.verify_field(table_id, field_id, type_settings.keys())

    def verify_field(self, table_id, field_id, keys):
        f = [x for x in self.fields(table_id) if x["id"] == field_id][0]
        ts = f.get("typeSettings") or {}
        missing = [k for k in keys if not ts.get(k)]
        if missing:
            raise ClayError("Clay silently dropped %s on column %r" % (missing, f["name"]))
        return f

    def set_run_condition(self, table_id, field_id, formula, description=""):
        """Gate an ACTION column so it only runs on matching rows.

        Formula columns silently ignore run conditions — the setting vanishes and update_field raises.
        """
        return self.update_field(table_id, field_id,
                                 conditionalRunFormulaText=formula,
                                 conditionalRunFormulaPrompt=description or "set via API")

    def run_condition_of(self, table_id, field_name):
        """The gate expression, or None. A column with no expression runs on EVERY row."""
        for f in self.fields(table_id):
            if f["name"] == field_name:
                return (f.get("typeSettings") or {}).get("conditionalRunFormulaText")
        raise ClayError("no column %r" % field_name)

    # ---------- records

    def views(self, table_id):
        """name -> view id. Tables normally ship with an 'All rows' view."""
        return {v["name"]: v["id"] for v in self.table(table_id)["views"]}

    def records(self, table_id, view=None, limit=1000, offset=0):
        """List records THROUGH A VIEW — the only enumeration route that exists.

        There is no /tables/{t}/records listing. The server default page size is 100, so always pass
        `limit`. Page with `offset` for tables over 1000 rows.
        """
        vs = self.views(table_id)
        vid = view or vs.get("All rows") or (list(vs.values())[0] if vs else None)
        if not vid:
            raise ClayError("table %s has no views to enumerate through" % table_id)
        if not vid.startswith("gv_"):
            vid = vs[vid]                                   # allow passing a view name
        return self.call("GET", "/tables/%s/views/%s/records?limit=%d&offset=%d"
                         % (table_id, vid, limit, offset)).get("results") or []

    def rows(self, table_id, view=None, limit=1000):
        """records() flattened to {column name: value} plus '_id'. The convenient form for filtering."""
        names = self.id_map(table_id)
        out = []
        for r in self.records(table_id, view, limit):
            row = {"_id": r["id"]}
            for fid, cell in (r.get("cells") or {}).items():
                row[names.get(fid, fid)] = (cell or {}).get("value")
            out.append(row)
        return out

    def insert(self, table_id, rows):
        """rows: [{field_id: value}, ...]. Returns new record ids, and computes formulas immediately.

        Keep the ids. Enumeration only works through a view, so ids you discard are awkward to recover.
        """
        body = {"records": [{"cells": r} for r in rows]}
        return [r["id"] for r in self.call("POST", "/tables/%s/records" % table_id, body)["records"]]

    def read(self, table_id, record_id):
        """Read ONE record. GET only.

        POST to this path is an upsert: it will create a record named after whatever you put in the
        path, or blank an existing one if you send an empty body.
        """
        return self.call("GET", "/tables/%s/records/%s" % (table_id, record_id))

    def cell(self, table_id, record_id, field_id):
        return (self.read(table_id, record_id).get("cells") or {}).get(field_id) or {}

    # ---------- auth accounts (the biggest trap in the whole API)

    def account_scores(self, table_id=None):
        """How well each auth account has actually performed, grouped by action type.

        Clay attaches an 'account' to each action column. A newly created column can land on a default
        account that has no credentials for that provider, and it then fails AT RUN TIME, inside the
        cell, with 'API key is missing.' Nothing in the HTTP response tells you. This counts real cell
        outcomes so you can see which account genuinely works.

        Grouped by actionKey because accounts are provider-specific: the account that runs an email
        finder is not the one that runs an AI column. Returns {actionKey: {account: {...counts}}}.
        """
        tables = [table_id] if table_id else [t["id"] for t in self.list_tables()]
        score = {}
        for tid in tables:
            try:
                full = self.table(tid)
            except ClayError:
                continue
            byid = {f["id"]: f for f in full["fields"]}
            if not any(f["type"] == "action" for f in full["fields"]):
                continue
            for rec in self.records(tid, limit=300):
                for fid, cell in (rec.get("cells") or {}).items():
                    f = byid.get(fid)
                    if not f or f["type"] != "action":
                        continue
                    ts = f.get("typeSettings") or {}
                    acct, key = ts.get("authAccountId"), ts.get("actionKey")
                    if not acct or not key:
                        continue
                    st = ((cell or {}).get("metadata") or {}).get("status")
                    s = score.setdefault(key, {}).setdefault(
                        acct, {"SUCCESS": 0, "INVALID_CREDENTIALS": 0, "other": 0})
                    if st == "SUCCESS":
                        s["SUCCESS"] += 1
                    elif st == "ERROR_INVALID_CREDENTIALS":
                        s["INVALID_CREDENTIALS"] += 1
                    elif st:
                        s["other"] += 1
        return score

    def best_account_for(self, action_key, table_id=None):
        """The account with the most successes and zero credential failures, for one action type."""
        if self._ai_account is None:
            self._ai_account = self.account_scores(table_id)
        per = self._ai_account.get(action_key) or {}
        good = [(v["SUCCESS"], k) for k, v in per.items()
                if v["SUCCESS"] > 0 and v["INVALID_CREDENTIALS"] == 0]
        if not good:
            return None                       # nothing proven; preflight reports it rather than guessing
        return sorted(good)[-1][1]

    def preflight(self, table_id, columns, scope=None):
        """Check columns are runnable BEFORE running them. Returns a list of problems, empty if fine.

        ALWAYS call this before running a column over live data. Runs clear the cell first, so a column
        on a broken account destroys existing values and then fails to replace them.

        `scope` limits the evidence used to judge accounts (default: the whole workspace, which is
        slow but thorough; pass a table id to keep it quick).
        """
        by = {f["name"]: f for f in self.fields(table_id)}
        bad = []
        for name in columns:
            f = by.get(name)
            if f is None:
                bad.append("%s: column not found" % name)
                continue
            if f["type"] != "action":
                continue
            ts = f.get("typeSettings") or {}
            acct, key = ts.get("authAccountId"), ts.get("actionKey")
            if not acct:
                bad.append("%s: no auth account set" % name)
            else:
                per = (self._ai_account or self.account_scores(scope)).get(key) or {}
                mine = per.get(acct)
                best = self.best_account_for(key, scope)
                if mine and mine["INVALID_CREDENTIALS"] and not mine["SUCCESS"]:
                    bad.append("%s: account %s only ever returns invalid-credentials%s"
                               % (name, acct, (" — %s works" % best) if best else ""))
                elif mine is None and best and acct != best:
                    bad.append("%s: account %s has no track record; %s is the proven one for %s"
                               % (name, acct, best, key))
            if key in ("use-ai", "claygent") and not any(
                    b.get("name") == "model" and b.get("formulaText")
                    for b in ts.get("inputsBinding") or []):
                bad.append("%s: no model bound — an AI column without a model never runs" % name)
        return bad

    # ---------- running

    def run(self, table_id, field_ids, record_ids, force=True):
        """Run columns on specific records. Raises unless Clay reports it actually dispatched them.

        runRecords MUST be {"recordIds": [...]} with real ids. Every other shape returns
        {"runMode": "NONE"} — a 200 that did nothing at all.

        force=True re-runs cells that already have a value, and CLEARS THEM FIRST. If the run then
        fails, the old value is gone; there is no undo. Preflight, then test on one row.
        """
        if not record_ids:
            raise ClayError("no record ids: Clay would return runMode NONE and do nothing")
        r = self.call("PATCH", "/tables/%s/run" % table_id,
                      {"fieldIds": list(field_ids), "runRecords": {"recordIds": list(record_ids)},
                       "callerName": "api", "forceRun": bool(force)})
        if r.get("runMode") == "NONE":
            raise ClayError("runMode NONE — nothing ran. Are those record ids real?")
        return r

    def wait(self, table_id, field_id, record_ids, timeout=600, interval=15):
        """Wait for a batch to settle. Polls through the view: ONE request per cycle.

        Do not poll per record — reading each cell individually in a loop earns a 504 from Clay's edge
        once the batch is more than a handful of rows.
        """
        want, deadline = set(record_ids), time.time() + timeout
        while time.time() < deadline:
            time.sleep(interval)
            pending = [r["id"] for r in self.records(table_id, limit=1000)
                       if r["id"] in want and
                       (((r.get("cells") or {}).get(field_id) or {}).get("metadata") or {}).get("status")
                       not in ("SUCCESS", "ERROR", "FAILED")]
            if not pending:
                return True
        return False

    def run_and_wait(self, table_id, field_id, record_ids, force=True, timeout=600):
        self.run(table_id, [field_id], record_ids, force=force)
        return self.wait(table_id, field_id, record_ids, timeout=timeout)

    def statuses(self, table_id, field_name):
        """Tally cell outcomes for one column. The fastest way to see what actually happened."""
        fid = self.field_map(table_id)[field_name]
        out = {}
        for r in self.records(table_id, limit=1000):
            st = (((r.get("cells") or {}).get(fid) or {}).get("metadata") or {}).get("status")
            if st:
                out[st] = out.get(st, 0) + 1
        return out

    # ---------- registry

    def actions(self, contains=None):
        """The enrichment/provider registry. Large (thousands of entries, tens of MB) — filter it.

        Each entry carries inputParameterSchema, which is how you learn a provider's expected inputs.
        """
        acts = self.call("GET", "/actions?workspaceId=%s" % self.workspace, timeout=180)["actions"]
        if contains:
            n = contains.lower()
            acts = [a for a in acts
                    if n in (str(a.get("key", "")) + str(a.get("displayName", ""))).lower()]
        return acts
