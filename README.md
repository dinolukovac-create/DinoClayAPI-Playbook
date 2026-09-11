# Clay API Playbook

**Everything needed to build, run and maintain Clay tables programmatically — including the failure modes
that are not written down anywhere else.**

**Clay has two separate programmatic surfaces.** The **REST API** (§4–§7) builds and runs table
infrastructure. The **MCP server** (§4b) searches Clay's own people and company database — that is where
sourcing lives. They do not share a data model, and knowing which one to reach for is most of the battle.

Clay publishes no REST API documentation. `docs.clay.com` redirects to a UI-focused university site, there is
no OpenAPI spec, and widely-read third-party articles still state that Clay "has no API". It does. This
playbook is the result of mapping it against a live workspace, including several wrong conclusions that had
to be corrected and one incident that destroyed live data.

> **If you are an AI assistant reading this to do work in Clay: read §3 before you write anything.** Clay's
> characteristic failure is a `200 OK` that quietly did less than you asked. Nine of the ten ways it fails
> are silent. One of them will delete your customer's data if you run a batch without checking first.

---

## Contents

1. [Setup and access](#1-setup-and-access)
2. [The data model](#2-the-data-model)
3. [**The seven silent failures**](#3-the-seven-silent-failures) ← read first
4. [Route reference](#4-route-reference)
4b. [**The MCP server — sourcing and enrichment**](#4b-the-mcp-server--sourcing-and-enrichment)
5. [Building a table](#5-building-a-table)
6. [Running columns](#6-running-columns)
7. [Reading data back](#7-reading-data-back)
8. [Keeping prompts under version control](#8-keeping-prompts-under-version-control)
9. [Cost control](#9-cost-control)
10. [A complete worked workflow](#10-a-complete-worked-workflow)
11. [How to explore the API safely](#11-how-to-explore-the-api-safely)
12. [Known limits and open questions](#12-known-limits-and-open-questions)

Files here: `clay.py` (the client — import it, don't rewrite it) and `examples/` (runnable scripts).

---

## 1. Setup and access

Generate an API key in the Clay UI (workspace menu → API keys). Then:

```bash
export CLAY_API_KEY='clay_user_...'      # or put CLAY_API_KEY=... in a .env beside clay.py
python examples/01_explore.py
```

| | |
|---|---|
| Base URL | `https://api.clay.com/v3` |
| Auth header | `Authorization: <key>` — **raw** |
| Not `Bearer <key>` | returns 403 |
| Not `x-api-key` | returns 401 |

The key acts **as the user who created it**. Writes appear under their name, and it inherits their
permissions — so an API key is not a service account and should not be treated as one. Rotate it if it is
ever pasted into a chat, a ticket, or a shared document.

**Workspace discovery.** `GET /v3/my-workspaces` returns every workspace the key can see, with ids.
`GET /me` does *not* include the workspace id and `GET /workspaces` is admin-only, so this is the route
to use. (The id is also embedded in the permission rules at `GET /v3` → `auth.abilities`, which is a
usable fallback.) `clay.py` handles both; pass `Clay(workspace=...)` if a key can see more than one.
`GET /v3/workspaces/{ws}/users` lists the members — names and email addresses, so treat it as
personal data.

---

## 2. The data model

```
workspace
└── table  (t_...)          "spreadsheet" or "people"
    ├── fields (f_...)      the columns
    │     text | formula | action | date
    ├── views  (gv_...)     saved filters — ALSO the only way to list rows
    └── records (r_...)     the rows; each holds cells keyed by field id
```

Ids are prefixed by type: `t_` table, `f_` field, `r_` record, `gv_` view, `aa_` auth account,
`wb_` workbook. Useful when reverse-engineering an unfamiliar payload.

**Column types**

| type | what it is | where the logic lives |
|---|---|---|
| `text` | a plain value | — |
| `formula` | a computed cell, free, instant | `typeSettings.formulaText` |
| `action` | an enrichment or AI call, **costs credits** | `typeSettings.inputsBinding` |
| `date` | `Created At` / `Updated At`, automatic | — |

**Formulas** are single JS *expressions* — no statements, no `const`. Use an arrow IIFE for locals:

```js
(s => s ? s.trim().toLowerCase() : "")({{f_abc123}})
```

They reference other columns as `{{f_...}}` — **by id, never by name**. Resolve names via `field_map()` /
`id_map()`. A formula recomputes automatically and immediately; it never needs running.

**Action columns** (AI, Claygent, and every third-party enrichment) keep their configuration in
`inputsBinding`, a list of `{name, formulaText}` entries. Each `formulaText` is a **JS expression**, so a
literal prompt is a quoted string, and chips are concatenated in:

```js
"You are analysing " + Clay.formatForAIPrompt({{f_company}}) + ". Reply with JSON."
```

That means reading a prompt back requires parsing the concatenation — see
`examples/05_export_config.py`.

---

## 3. The ten silent failures

Clay rarely refuses. It returns `200` and quietly does less than you asked. Every one of these cost real time
to diagnose; #6 and #7 together destroyed live customer-facing data.

| # | Failure | What you see | Defence |
|---|---|---|---|
| 1 | A setting Clay won't accept is **dropped** | 200, then the key isn't there on read-back | `verify_field()` — assert after every write |
| 2 | `runRecords` in the wrong shape | 200 with `{"runMode":"NONE"}`; nothing ran | `run()` raises on `NONE` |
| 3 | An **AI column with no `model`** | Column looks perfect, never executes — ever | Clone a working column; never hand-assemble |
| 4 | A run condition on a **formula** column | 200, the gate silently vanishes | Gates only work on `action` columns |
| 5 | `POST /tables/{t}/records/{anything}` | 200 that **creates or blanks** a record | It's an upsert. Only ever `GET` to read |
| 6 | Column on an **auth account with no credentials** | `ERROR_INVALID_CREDENTIALS` *in the cell*, not in the response | `preflight()` before every run |
| 7 | **`forceRun` clears the cell before running** | Run fails ⇒ the old value is gone, no undo | Preflight, then test on ONE row |
| 8 | **Non-ASCII in a prompt literal** | Column created, preflights clean, dispatches — and never runs | Build prompts with `prompt_literal()` |
| 9 | A gate comparing a **formula column to a boolean** | Gate never matches; column silently skipped | Formula columns are STRINGS — use `truthy()` |
| 10 | **`offset` on the records endpoint** | Every page returns the same rows; paging loops never end | Raise `limit` instead; `offset` does nothing |

### #8 — the one that cost the most time

An AI column built through the API can be **perfect in every observable way and still never execute**:
created successfully, correct `actionKey` / `model` / `authAccountId`, `preflight()` clean, `run()` returns
`{"runMode":"INDIVIDUAL"}` — and the cell just sits there with `trigger: FORCE-RUN` and no status, forever.

The tell is **`inputFieldIds`**. Clay derives that list server-side, at create time, by parsing the prompt for
chip references. It is the column's dependency graph. If it comes back `None`, the column will never run —
and you cannot repair it: `PATCH`ing `inputFieldIds` is accepted and silently ignored (failure #1 again).

What breaks the parse is **any non-ASCII character in the prompt literal**. Binary-searched to the exact
character: smart quotes (`“ ” ’`) and em dashes all break it; 2,500 plain ASCII characters and newlines are
fine, and chip count is irrelevant.

The trap is that the obvious way to build the literal causes it:

```python
json.dumps(text)                      # ensure_ascii=True -> \u201c escapes -> BROKEN, silently
json.dumps(text, ensure_ascii=False)  # raw UTF-8 -> works
```

`Clay.prompt_literal(text, chips)` does it correctly, and `clone_column()` now raises if
`inputFieldIds` comes back empty rather than handing you a column that will never run.

### #9 — formula columns are strings

A formula column that evaluates to a boolean is stored and returned as the **string** `"true"` / `"false"`.
So a run condition written the obvious way never matches, and the gated column is silently skipped with no
error anywhere:

```js
{{f_qualified}} === true                        // never matches
String({{f_qualified}}).toLowerCase() === "true" // correct — Clay.truthy() emits this
```

The same coercion bites values arriving through a lookup: booleans may come back as booleans *or* as
strings depending on the path they took. Compare defensively.

### #6 and #7 — the incident, in full, because this is the expensive one

A new AI column is assigned a default "account". At least one such default carries **no model credentials**.
The column is created successfully, reports correct settings through the API, and passes every structural
check — then fails **at run time, inside the cell**, with `ERROR_INVALID_CREDENTIALS` and
*"API key is missing."* The HTTP response for the run is a cheerful `200 {"runMode": "INDIVIDUAL"}`.

Now combine that with `forceRun`, which **empties a cell before regenerating it**. A batch re-run across
live rows wipes the existing values and then fails to replace them. In the incident this is drawn from:

- two columns sat on the wrong account; a batch re-run **blanked 27 rows** of finished, customer-facing copy
- the pipeline's "ready to send" count fell from 245 to 218
- **retrying made it worse** — every retry cleared more cells before failing
- diagnosis was slow because the failure is invisible from the API response; it only shows in cell metadata

The fix took seconds once found: point the columns at the account that has credentials (UI dropdown, or
`authAccountId` via PATCH). Everything regenerated immediately.

**The rule, and there is no exception to it:**

```python
bad = c.preflight(table, ["My AI Column"], scope=table)
if bad: raise SystemExit(bad)          # never forceRun over live data unverified
c.run_and_wait(table, col, ids[:1])    # then ONE row
# inspect the result, and only then the batch
```

`preflight()` scores every auth account by **actual cell outcomes** — successes versus credential failures,
grouped by action type, because accounts are provider-specific. It works in any workspace without knowing
account ids in advance.

### Other cell statuses worth recognising

| status | meaning |
|---|---|
| `SUCCESS` | ran and produced a value |
| `ERROR_INVALID_CREDENTIALS` | the account has no working key — see above |
| `ERROR_BLANK_TOKEN` | a **required input chip is blank**, so the row can never run — see `optional=` on `clone_column()` |
| `ERROR_RUN_CONDITION_NOT_MET` | the gate correctly excluded this row. Not an error |
| `isStale` + `staleReason` | e.g. `TABLE_AUTO_RUN_OFF` — the cell is out of date and nothing will refresh it automatically |

### Error dictionary — how to read Clay's refusals

| response | meaning |
|---|---|
| `{"type":"NoMatchingURL"}` | **route + method** doesn't exist. Method-specific: `POST /run` is NoMatchingURL while `PATCH /run` works. Never conclude "impossible" from one verb |
| `{"type":"BadRequest"}` + field errors | the route **exists**; your body is wrong. When probing this is a *success* — `details.bodyErrors.issues` names the exact expected types |
| `{"type":"NotFound","message":"Field f_x does not exist..."}` | route resolved, id wrong. The safe way to prove a route exists |
| `{"type":"FieldNameAlreadyExists"}` | a retry after a partial failure |
| `"Missing data type settings"` | `dataTypeSettings` omitted when creating a column |
| `"Missing data type settings in type settings"` | you sent a **partial** `typeSettings` to PATCH — it replaces, never merges |
| `"value" does not match any of the allowed types` | enum/shape rejection; start from a working column's shape |
| `504 Gateway Timeout` | usually self-inflicted: polling per-record in a loop. Poll through a view instead |

---

## 4. Route reference

Everything verified against a live workspace.

| method | route | purpose |
|---|---|---|
| GET | `/v3` | identity envelope — **workspace id lives here** |
| GET | `/me` | the acting user |
| GET | `/workspaces/{ws}/tables` | list tables |
| GET | `/tables/{t}` | **the whole table**: settings, all field definitions, views |
| POST | `/tables` | create a table (requires `workspaceId`) |
| PATCH | `/tables/{t}` | table settings — auto-run lives here |
| DELETE | `/tables/{t}` | delete a table |
| POST | `/tables/{t}/fields` | create a column |
| PATCH | `/tables/{t}/fields/{f}` | **edit a column** — prompt, formula, run condition, account |
| GET | `/tables/{t}/views/{v}` | one view, including its filter tree |
| POST | `/tables/{t}/views` | create a view (requires `name`) |
| PATCH | `/tables/{t}/views/{v}` | edit a view / its filter |
| GET | `/tables/{t}/views/{v}/records` | **list rows** — `?limit=&offset=`, default page 100 |
| GET | `/tables/{t}/records/{r}` | read one record |
| POST | `/tables/{t}/records` | insert rows; returns them with computed cells |
| PATCH | `/tables/{t}/records` | update rows |
| PATCH | `/tables/{t}/run` | **run columns** — body shape matters, see §6 |
| GET | `/actions?workspaceId={ws}` | provider registry (thousands of entries, tens of MB) |
| GET | `/sources?workspaceId={ws}` | sources; also accepts `?tableId=` |
| GET | `/workbooks/{wb}/tables` | tables in a workbook |

**Do not exist:** `GET /tables/{t}/fields`, `GET /tables/{t}/fields/{f}`, `PUT` on a field,
`GET /tables/{t}/records` (collection), `/views/{v}/records` without the table prefix, and any
`/fields/{f}/run`.

---

## 4b. The MCP server — sourcing and enrichment

**This is where finding new companies and people happens.** It is a *different product surface* from the REST
API: conversational, credit-metered, and built around Clay's own database rather than around your tables.

Connect it in the AI client (Clay's own page positions it for "ChatGPT, Codex or Claude"); it authenticates
through the workspace connection, not the REST API key. There is no HTTP contract to hand-roll — the tools
arrive in the assistant's tool list.

### What it does that the REST API cannot

| tool | purpose |
|---|---|
| `find-and-enrich-contacts-at-company` | **search people by criteria at a company** — the main sourcing tool |
| `find-and-enrich-list-of-contacts` | resolve specific *named* people to profiles |
| `find-and-enrich-company` | look up / research one company |
| `add-company-data-points` / `add-contact-data-points` | enrich an existing search (**costs credits**) |
| `query-objects` / `ask-question-about-accounts` | your OWN CRM/account data — not prospecting |
| `list_subroutines` / `run_subroutine` | workspace-defined Functions (lead scoring, account prep) |
| `get-task` / `get-task-context` | re-read a prior search instead of re-running it |
| `get-credits-available` / `get-current-workspace` | account state |

### People search filters

`find-and-enrich-contacts-at-company` accepts a rich filter set — this is the real ICP surface:

`job_title_keywords`, `job_title_exclude_keywords`, `names`, `profile_keywords` (anything in the profile),
`certification_keywords`, `languages`, `school_names`, `locations`, `locations_exclude`,
`current_role_min_months_since_start_date`, `current_role_max_months_since_start_date` (new hires vs tenured).

Filters combine with **AND**; values inside one array combine with **OR**. Keep compound titles as a single
string — `["VP Finance"]`, not `["VP", "Finance"]` — and be specific: `"Software Engineer"` rather than
`"Engineer"`, which also matches Sales Engineer.

### What a search returns

```
{ taskId, searchId,
  companies: { "<domain>": { name, domain, industry, employee_count, annual_revenue,
                             total_funding_amount_range_usd, locations[], description, entityId } },
  contacts:  [ { name, latest_experience_title, latest_experience_company,
                 latest_experience_start_date, url (LinkedIn), location_name,
                 domain, entityId, enrichments[] } ],
  page, hasMore }
```

A verified example: searching one company for Product Managers in the United States returned **20 contacts**
with names, titles, LinkedIn URLs, locations and role start dates, plus a full company record — with
`enrichments: []` and **no credits spent**, because no data points were requested.

### The credit rule that matters

**The search itself is cheap. `dataPoints` are what cost credits.** Clay's own tool documentation is emphatic:
*never add data points unless the user explicitly asked for them.*

- "Find PMs at Acme" → **no** data points
- "Find PMs at Acme **and get their emails**" → add `Email`

Company data points: `Headcount Growth`, `Recent News`, `Investors`, `Company Competitors`,
`Company Customers`, `Tech Stack`, `Website Traffic`, `Open Jobs`, `Revenue Model`, `Annual Revenue`,
`Latest Funding`. Contact data points: `Email`, `Summarize Work History`, `Find Thought Leadership`.
Both accept `{type: "Custom", ...}` for open-ended research.

Enrich an existing search with `add-*-data-points` and its `taskId` (optionally `entityIds` for specific
rows) — do **not** start a new search to enrich something you already found. Check `get-task-context` first
in case the data is already there.

### Gotchas

- **`companyIdentifier` must be a domain or LinkedIn company URL.** Bare company names fail. Convert
  confidently (`"Stripe"` → `"stripe.com"`), and ask when ambiguous (`"Delta"`).
- **Person LinkedIn URLs are not company identifiers** — a common mix-up in `find-and-enrich-list-of-contacts`.
- **`query-objects` is not prospecting.** It queries your own synced CRM objects and returns nothing in a
  workspace with no CRM connected — which reads exactly like "no results" if you mistake it for search.
- **Re-call the tool to refine**, rather than filtering results in the conversation.
- Results are paginated (`page`, `hasMore`).

### Joining the two surfaces

**Nothing links them automatically.** An MCP search yields `taskId` and `entityId`s (LinkedIn profile ids); a
table holds `t_`/`r_` ids. To get sourced people into a pipeline you carry them across yourself:

```python
# after an MCP search returns `contacts`
ids = c.insert(table, [{
    name_col:    p["name"],
    title_col:   p["latest_experience_title"],
    li_col:      p["url"],
    domain_col:  p["domain"],
} for p in contacts])
```

Which surface to reach for:

| goal | surface |
|---|---|
| find companies / people that match an ICP | **MCP** |
| research one account conversationally | **MCP** |
| build columns, prompts, formulas, gates | **REST** |
| run enrichment at scale over many rows | **REST** |
| enumerate, QA and export a pipeline | **REST** |

Rule of thumb: **MCP for discovery and one-off research; REST for repeatable infrastructure.** A campaign
typically starts in the MCP and lives in the REST API.

---

## 5. Building a table

```python
from clay import Clay
c = Clay()

t     = c.create_table("Campaign — Companies")     # auto-run OFF by default here
name  = c.add_text(t, "company")
qual  = c.add_formula(t, "qualified", '(({{%s}}||"").length > 0)' % name)
```

### Creating action columns: clone, never hand-assemble

Hand-built AI columns silently never run (§3 #3, #6). Clone a column that demonstrably works and override
its bindings:

```python
src_table = c.find_table("Some Existing Table")
fit = c.clone_column(t, "consumer_fit", src_table, "An AI Column That Works", bindings={
    "prompt": '"Is " + Clay.formatForAIPrompt({{%s}}) + " a consumer brand? Reply {\\"fit\\":true}"' % name,
    "model":  '"claude-sonnet-5"',
})
```

Cloning carries over `authAccountId`, `model`, `useCase`, `runBudget` and the rate-limit rules — precisely
the settings whose absence causes silent non-execution.

**Three things a clone gets wrong, and `clone_column()` now handles all three:**

1. **The source's run condition comes with it.** Stripped, so you never inherit another column's gate.
2. **`optionalPathsInInputs` still points at the SOURCE table's field ids.** Those mean nothing in the new
   table, so every chip is effectively required, and any row with one blank chip dies with
   `ERROR_BLANK_TOKEN`. On a real run this was 136 of 250 rows. Pass `optional=[field_ids]` — mark every
   chip optional except the one or two the prompt genuinely cannot work without.
3. **`inputFieldIds` may come back empty** (failure #8), which means the column will never run. It now
   raises instead of returning a dead column.

**Prompt chips are named by the prompt, not the column.** Two prompts for the same job routinely use
different placeholder names for the same input (`{{linkedin1_message}}` vs `{{cmo_linkedin_message_1}}`, or
`{{company_name}}` vs `{{company}}`). Keep an alias map from placeholder name to the target table's columns
rather than assuming they line up.

**Action output keys are camelCased.** A prompt that specifies `"arr_band"` and `"use_as_opener"` in its
JSON schema is read back as `?.arrBand` and `?.useAsOpener`. Extract with a formula column per field:
`{{f_action}}?.triggerSummary`.

**In a brand-new workspace with nothing to clone**, build one column by hand *in the UI*, confirm it runs on
one row, then clone that from then on. This is faster than deriving the required binding set by trial.

`GET /actions?workspaceId=` lists every provider with its `inputParameterSchema` if you need to construct one
from scratch (`c.actions(contains="email")`).

### Run conditions — gate anything that costs money

A column with no run condition **runs on every row**. On action columns that is a direct bill.

```python
c.set_run_condition(t, fit,
    '{{%s}}?.toLowerCase()==="uk" && {{%s}}===true' % (country, qual),
    "Only run for UK rows that are qualified")
```

The gate is a JS expression in `typeSettings.conditionalRunFormulaText`, with an optional plain-English
`conditionalRunFormulaPrompt` shown in the UI. `conditionalRunFieldIds` is **derived** by Clay — read it,
never write it.

Audit gates with `run_condition_of()`; `None` means ungated. In the workspace this playbook came from,
**7 of 8 paid columns turned out to have no gate at all** despite documentation claiming otherwise. Check,
don't assume.

⚠️ **Beware type coercion through lookups.** A boolean in one table can arrive as the *string* `"true"` in
another. Gates must match what actually arrives:

```js
{{f_fit}} === true && {{f_qualified}} === "true"     // both, in the same expression, deliberately
```

---

## 6. Running columns

```
PATCH /v3/tables/{tableId}/run
{
  "fieldIds":   ["f_..."],
  "runRecords": {"recordIds": ["r_...", ...]},   ← MUST be an object of REAL ids
  "callerName": "api",
  "forceRun":   true
}
```

The response is a `runMode`. **`runRecords` is the whole trick**: real ids give
`{"runMode":"INDIVIDUAL"}`; `{}`, `{"all":true}`, `{"ids":[...]}`, or an empty list all give
`{"runMode":"NONE"}` — a 200 that did nothing. `clay.py` raises on `NONE`.

There is **no per-field run route** and **no "run everything" mode**. You must enumerate the rows you want.

```python
ids = [r["_id"] for r in c.rows(t) if r["qualified"] == "true"]
bad = c.preflight(t, ["consumer_fit"], scope=t)
if bad: raise SystemExit(bad)
c.run_and_wait(t, c.field_map(t)["consumer_fit"], ids[:1])   # one row first
c.run_and_wait(t, c.field_map(t)["consumer_fit"], ids)       # then the batch
print(c.statuses(t, "consumer_fit"))
```

**`forceRun=True` re-runs cells that already have values and clears them first.** Use `False` to fill only
blanks — much safer for a retry.

**Batches are far faster than single rows.** Clay reports `runMode` as `INDIVIDUAL` for small sets and
`BULK` for large ones, and BULK is dramatically more efficient: 437 AI cells finished in about 20 seconds,
while the same column on one row took 5 minutes end to end. Do not pace your polling off a single-row test —
fire the batch and check back in two or three minutes.

**Per-cell cost is not reported, but account-level pricing and credits are.** A cell's `metadata`
carries `status` and `confidence` and nothing else, so you cannot read back what one row cost. The
account ledger, however, is available:

| route | what |
|---|---|
| `GET /v3/workspaces/{ws}/model-pricing/base-costs` | **base credit cost per AI model** |
| `GET /v3/credit-accrual?workspaceId={ws}` | credit grants: type, amount, period |
| `GET /v3/workspaces/{ws}/credit-limits/workbook/{wb}/balance` | a workbook's credit limit and balance |
| `GET /v3/workspaces/{ws}/peopleSearchLimit` | the people-search ceiling |

That is enough to **estimate a run before starting it**, which is the number that actually matters:

```python
cost = c.model_costs()["claude-sonnet-5"] * c.count(table)   # credits, before you spend any
```

`model_costs()` returns ~47 models with their per-call credit cost, and they differ by an order of
magnitude — choosing the model is a real cost lever, not a detail.

**Polling: never per record.** Reading each cell individually in a loop earns a `504` from Clay's edge once
the batch is more than a handful of rows.

The cheapest way to watch a run is **`GET /v3/workspaces/{ws}/tables/{t}/fields/runstatus`**, which returns
status counts for *every field in the table* in a single request — no records fetched at all. `wait()` polls
through the view; `run_status()` is the lighter option when you only need to know whether a batch has
settled.

**Dependency order matters.** If column B consumes column A's output, re-running A means re-running B.
Lookup columns are **snapshots**: changing a source table does nothing downstream until the lookup is
re-run. A correct chain is: source table → lookup refresh → dependent columns, in that order, each waited
on before the next.

**Expect a few percent of AI cells to fail per batch** — typically JSON that arrives as a raw string, so the
parent cell holds text while extracted sub-fields stay blank. Re-running usually fixes it. Occasionally a row
never recovers: cap your retries, exclude it, and note it, rather than burning credits on convergence that
isn't coming. Adding *"Return ONLY the raw JSON object. No code fences."* to the prompt reduces the rate.

---

## 7. Reading data back

**Rows are enumerated through a view — there is no records-listing route.**

```python
rows = c.rows(t)                       # [{'_id': 'r_...', 'column name': value, ...}]
recs = c.records(t, limit=1000)        # raw form, with per-cell metadata/status
one  = c.read(t, "r_abc")              # a single record
```

- Server default page size is **100** — always pass `limit`.
- Page with `offset` beyond 1000.
- Every table normally has an **`All rows`** view; `rows()` uses it by default. Pass a view name to read a
  filtered subset (`c.rows(t, view="Errored rows")`).
- View filters support types like `HAS_ERROR`, `RUN_CONDITION_NOT_MET`, `NO_RESULTS`, `EMPTY` — server-side
  filtering if a table is too big to filter locally.

Cell metadata is where truth lives: `cells[field_id].metadata.status` tells you whether a value is real, a
failure, or was never attempted. `statuses(table, column)` tallies it in one call.

**Validate against something you trust.** When this enumeration was first used, it was checked against a
known-good CSV export and reproduced every figure exactly. Do the same once, then trust it.

---

## 8. Keeping prompts under version control

Prompts and formulas edited in the Clay UI live **only** in Clay. Clay's own AI builder also rewrites prompts
in place. Both mean your repository copy silently stops matching production.

In the workspace this came from, a prompt file in the repo was a 1.4k stub while the live column held a 4.1k
rewritten version. Nobody knew until it was diffed.

**Keep files as the source of truth, and verify it** — don't assume:

```bash
python examples/05_export_config.py   # pull live prompts/formulas into files
python examples/06_drift_check.py     # diff files vs live, exit non-zero on drift
```

Normalisation matters when diffing:

- **Prompts** are stored as a JS concatenation, and the UI flattens newlines. Collapse whitespace runs before
  comparing, and reconstruct chips as readable `{{Column Name}}`.
- **Formulas** are minified by Clay (`s=>s?s:""` vs your `s => s ? s : ""`). Strip whitespace entirely — it's
  insignificant in JS — or every re-paste looks like a change.
- Strip your own header comments from files; they never go into Clay.

Run the drift check at the start of any session that touches an existing table.

---

## 9. Cost control

Formulas and reads are free. **Action columns cost credits per cell.** The controls, in order of importance:

1. **Turn auto-run OFF.** It's table-level (`AUTO_RUN_ON`) and Clay defaults it **on**. With it on, paid
   columns fire whenever rows land or a column changes — including as a side effect of an API edit.
   `create_table()` here defaults it off.
2. **Gate every paid column** with a run condition (§5).
3. **Filter and enumerate, then run on that list.** Don't rely on a gate as a budget: gates protect against
   stray single-row runs, they don't stop you dispatching 400 rows by mistake.
4. **Iterate on 5–10 rows**, including deliberate edge cases, before running the full set.
5. **Order paid columns cheapest-first.** A free formula and a cheap classifier can cut the population
   before an expensive web-research column ever sees it — in the source workspace, two cheap gates cut
   394 rows to 157 before the expensive column ran.
6. **Audit periodically** (`examples/04_audit.py`): which tables have auto-run on, and which paid columns
   are ungated. The dangerous combination is both at once.

Web-research columns are non-deterministic: re-running the same row yields different findings. Never re-run
research "to be safe" — you may lose a good result.

---

## 10. A complete worked workflow

The shape that works, end to end:

```
1. SOURCE      via the Clay MCP (§4b) or an external tool; then insert() the results into a table
2. GATE        cheap formulas first — free, and they shrink everything downstream
3. CLASSIFY    a cheap AI column, gated on the formula, to drop non-ICP rows
4. ENRICH      expensive research, gated on the classifier
5. GENERATE    output columns, gated on persona/segment + the gates above
6. QA          enumerate rows, check the output in Python, collect failing ids
7. RE-RUN      only the failures — then re-QA. Repeat until it converges or plateaus
8. EXPORT      filter to the ready set, write a CSV for the downstream tool
```

Notes from doing this in anger:

- **Steps 6–7 are a loop, and it plateaus.** Most defects clear in one or two passes; a small tail never
  converges because the prompt itself causes it. Recognise the plateau, flag those rows, move on.
- **Put a QA flag in the export** rather than silently dropping rows — let the human decide.
- **Cap output per company/account** if you're contacting several people at one organisation. Prompts keyed
  on company + segment produce near-identical text for two people at the same company with the same role —
  visible and embarrassing if colleagues compare. Deduplicate on (company, segment).
- **Keep a stable join key** (a slug formula) so downstream replies can be matched back. Normalise accents.
- **Re-QA from live data, not yesterday's export.** They diverge the moment anyone edits anything.

---

## 11. How to explore the API safely

There is no documentation, so mapping is part of the work. What works:

1. **Guess the noun, then try every method.** Route matching is method-specific. A whole capability was
   wrongly written off as impossible because only `POST` was tried on the right path — it needed `PATCH`.
2. **Read the validation errors.** `details.bodyErrors.issues` is the API describing its own schema. The
   run-endpoint body shape was reverse-engineered entirely from those messages.
3. **Probe with ids that cannot exist.** `PATCH /tables/{real}/fields/f_FAKE` proves a route resolves
   (typed `NotFound`) while being incapable of changing anything.
4. **Do all shape-finding on a throwaway table.** Create it, learn on it, delete it. Never on live data.
5. **Search the web first.** One key route came from a third-party blog post, not from probing — though its
   documented body shape was wrong and still needed experiment.

### What not to do

**Never send a destructive method to a live table as a probe.** During this mapping a `DELETE` was fired at
a production table's `/fields` route to see what would happen. It returned `200` and happened to be a no-op —
verified immediately afterwards — but that was luck. Destructive verbs belong on scratch tables only.

Related: probing `POST /tables/{t}/records/bulk|query|list` looks like endpoint discovery but is actually
`/records/{recordId}` **upserting**, which creates junk records literally named `bulk`, `query` and `list`.
If you ever see records with names like that, this is where they came from.

---

## 12. Known limits and open questions

**Confirmed limits**

- **The REST API does not source.** It builds and runs tables. Sourcing is the MCP's job (§4b) — or an
  external tool. Either way the results reach a table through `insert()`.
- **No bulk "run all".** You must enumerate record ids.
- **No records-listing route.** Enumeration is view-scoped (this is fine, just not obvious).
- **Rows over 1000** need `offset` paging; untested at very large scale.

**Unexplored — worth investigating if you need them**

- **Webhook sources.** `GET /sources?workspaceId=` lists them; creating an inbound webhook source was never
  attempted. This is the pattern Clay's own docs describe for continuous programmatic row entry, and it may
  run enrichments on arrival.
- **Server-side filtered views** via `POST /tables/{t}/views` — create a view for "errored rows" and read
  only those, instead of filtering locally.
- **Running Claygent (web-research) columns via the API.** Standard AI columns are proven to execute; the
  research variant is proven only to be *creatable*. Verify before depending on it.
- **`workbookId`** appears on every table and is barely explored beyond `/workbooks/{wb}/tables`.

- **MCP ↔ REST are not joined.** An MCP search returns `taskId` / `entityId`; a table holds `t_` / `r_` ids.
  Nothing links them automatically — you carry the results across yourself.

**A closing warning.** This API is undocumented and can change without notice. Everything here was true when
tested against a live workspace. Re-verify anything load-bearing before trusting it in production, and treat
a surprising result as new information about the API rather than a bug in your code.
