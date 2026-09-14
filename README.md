# Clay API Playbook

**Everything needed to build, run and maintain Clay tables programmatically, including the failure modes
that are not written down anywhere else.**

**Clay has two separate programmatic surfaces.** The **REST API** (§4-§7) builds and runs table
infrastructure. The **MCP server** (§4b) searches Clay's own people and company database, which is where
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
3. [**The seven silent failures**](#3-the-ten-silent-failures) ← read first
4. [Route reference](#4-route-reference)
4b. [**The MCP server: sourcing and enrichment**](#4b-the-mcp-server-sourcing-and-enrichment)
5. [Building a table](#5-building-a-table)
6. [Running columns](#6-running-columns)
7. [Reading data back](#7-reading-data-back)
8. [Keeping prompts under version control](#8-keeping-prompts-under-version-control)
9. [Cost control](#9-cost-control)
10. [A complete worked workflow](#10-a-complete-worked-workflow)
11. [How to explore the API safely](#11-how-to-explore-the-api-safely)
12. [Known limits and open questions](#12-known-limits-and-open-questions)
13. Auto-run, parking, and what actually triggers a run

Files here: `clay.py` (the client, which you should import rather than rewrite) and `examples/` (runnable scripts).

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
| Auth header | `Authorization: <key>`: **raw** |
| Not `Bearer <key>` | returns 403 |
| Not `x-api-key` | returns 401 |

The key acts **as the user who created it**. Writes appear under their name, and it inherits their
permissions, so an API key is not a service account and should not be treated as one. Rotate it if it is
ever pasted into a chat, a ticket, or a shared document.

**Workspace discovery.** `GET /v3/my-workspaces` returns every workspace the key can see, with ids.
`GET /me` does *not* include the workspace id and `GET /workspaces` is admin-only, so this is the route
to use. (The id is also embedded in the permission rules at `GET /v3` → `auth.abilities`, which is a
usable fallback.) `clay.py` handles both; pass `Clay(workspace=...)` if a key can see more than one.
`GET /v3/workspaces/{ws}/users` lists the members, returning names and email addresses, so treat it as
personal data.

---

## 2. The data model

```
workspace
└── table  (t_...)          "spreadsheet" or "people"
    ├── fields (f_...)      the columns
    │     text | formula | action | date
    ├── views  (gv_...)     saved filters, and the only way to list rows
    └── records (r_...)     the rows; each holds cells keyed by field id
```

Ids are prefixed by type: `t_` table, `f_` field, `r_` record, `gv_` view, `aa_` auth account,
`wb_` workbook. Useful when reverse-engineering an unfamiliar payload.

**Column types**

| type | what it is | where the logic lives |
|---|---|---|
| `text` | a plain value |: |
| `formula` | a computed cell, free, instant | `typeSettings.formulaText` |
| `action` | an enrichment or AI call, **costs credits** | `typeSettings.inputsBinding` |
| `date` | `Created At` / `Updated At`, automatic |: |

**Formulas** are single JS *expressions*: no statements, no `const`. Use an arrow IIFE for locals:

```js
(s => s ? s.trim().toLowerCase() : "")({{f_abc123}})
```

They reference other columns as `{{f_...}}`, **by id and never by name**. Resolve names via `field_map()` /
`id_map()`. A formula recomputes automatically and immediately; it never needs running.

**Action columns** (AI, Claygent, and every third-party enrichment) keep their configuration in
`inputsBinding`, a list of `{name, formulaText}` entries. Each `formulaText` is a **JS expression**, so a
literal prompt is a quoted string, and chips are concatenated in:

```js
"You are analysing " + Clay.formatForAIPrompt({{f_company}}) + ". Reply with JSON."
```

That means reading a prompt back requires parsing the concatenation. See
`examples/05_export_config.py`.

---

## 3. The ten silent failures

Clay rarely refuses. It returns `200` and quietly does less than you asked. Every one of these cost real time
to diagnose; #6 and #7 together destroyed live customer-facing data.

| # | Failure | What you see | Defence |
|---|---|---|---|
| 1 | A setting Clay won't accept is **dropped** | 200, then the key isn't there on read-back | `verify_field()`: assert after every write |
| 2 | `runRecords` in the wrong shape | 200 with `{"runMode":"NONE"}`; nothing ran | `run()` raises on `NONE` |
| 3 | An **AI column with no `model`** | Column looks perfect, never executes: ever | Clone a working column; never hand-assemble |
| 4 | A run condition on a **formula** column | 200, the gate silently vanishes | Gates only work on `action` columns |
| 5 | `POST /tables/{t}/records/{anything}` | 200 that **creates or blanks** a record | It's an upsert. Only ever `GET` to read |
| 6 | Column on an **auth account with no credentials** | `ERROR_INVALID_CREDENTIALS` *in the cell*, not in the response | `preflight()` before every run |
| 7 | **`forceRun` clears the cell before running** | Run fails ⇒ the old value is gone, no undo | Preflight, then test on ONE row |
| 8 | **Non-ASCII in a prompt literal** | Column created, preflights clean, dispatches, and never runs | Build prompts with `prompt_literal()` |
| 9 | A gate comparing a **formula column to a boolean** | Gate never matches; column silently skipped | Formula columns are STRINGS: use `truthy()` |
| 10 | **`offset` on the records endpoint** | Every page returns the same rows; paging loops never end | Raise `limit` instead; `offset` does nothing |

### #8: the one that cost the most time

An AI column built through the API can be **perfect in every observable way and still never execute**:
created successfully, correct `actionKey` / `model` / `authAccountId`, `preflight()` clean, `run()` returns
`{"runMode":"INDIVIDUAL"}`, and the cell just sits there with `trigger: FORCE-RUN` and no status, forever.

The tell is **`inputFieldIds`**. Clay derives that list server-side, at create time, by parsing the prompt for
chip references. It is the column's dependency graph. If it comes back `None`, the column will never run: 
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

### #9: formula columns are strings

A formula column that evaluates to a boolean is stored and returned as the **string** `"true"` / `"false"`.
So a run condition written the obvious way never matches, and the gated column is silently skipped with no
error anywhere:

```js
{{f_qualified}} === true                        // never matches
String({{f_qualified}}).toLowerCase() === "true" // correct: Clay.truthy() emits this
```

The same coercion bites values arriving through a lookup: booleans may come back as booleans *or* as
strings depending on the path they took. Compare defensively.

### #6 and #7: the incident, in full, because this is the expensive one

A new AI column is assigned a default "account". At least one such default carries **no model credentials**.
The column is created successfully, reports correct settings through the API, and passes every structural
check, then fails **at run time, inside the cell**, with `ERROR_INVALID_CREDENTIALS` and
*"API key is missing."* The HTTP response for the run is a cheerful `200 {"runMode": "INDIVIDUAL"}`.

Now combine that with `forceRun`, which **empties a cell before regenerating it**. A batch re-run across
live rows wipes the existing values and then fails to replace them. In the incident this is drawn from:

- two columns sat on the wrong account; a batch re-run **blanked 27 rows** of finished, customer-facing copy
- the pipeline's "ready to send" count fell from 245 to 218
- **retrying made it worse**: every retry cleared more cells before failing
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

`preflight()` scores every auth account by **actual cell outcomes**: successes versus credential failures,
grouped by action type, because accounts are provider-specific. It works in any workspace without knowing
account ids in advance.

### Other cell statuses worth recognising

| status | meaning |
|---|---|
| `SUCCESS` | ran and produced a value |
| `ERROR_INVALID_CREDENTIALS` | the account has no working key: see above |
| `ERROR_BLANK_TOKEN` | a **required input chip is blank**, so the row can never run: see `optional=` on `clone_column()` |
| `ERROR_RUN_CONDITION_NOT_MET` | the gate correctly excluded this row. Not an error |
| `isStale` + `staleReason` | e.g. `TABLE_AUTO_RUN_OFF`: the cell is out of date and nothing will refresh it automatically |

### Error dictionary: how to read Clay's refusals

| response | meaning |
|---|---|
| `{"type":"NoMatchingURL"}` | **route + method** doesn't exist. Method-specific: `POST /run` is NoMatchingURL while `PATCH /run` works. Never conclude "impossible" from one verb |
| `{"type":"BadRequest"}` + field errors | the route **exists**; your body is wrong. When probing this is a *success*: `details.bodyErrors.issues` names the exact expected types |
| `{"type":"NotFound","message":"Field f_x does not exist..."}` | route resolved, id wrong. The safe way to prove a route exists |
| `{"type":"FieldNameAlreadyExists"}` | a retry after a partial failure |
| `"Missing data type settings"` | `dataTypeSettings` omitted when creating a column |
| `"Missing data type settings in type settings"` | you sent a **partial** `typeSettings` to PATCH: it replaces, never merges |
| `"value" does not match any of the allowed types` | enum/shape rejection; start from a working column's shape |
| `504 Gateway Timeout` | usually self-inflicted: polling per-record in a loop. Poll through a view instead |

---

## 4. Route reference

Everything verified against a live workspace.

| method | route | purpose |
|---|---|---|
| GET | `/v3` | identity envelope: **workspace id lives here** |
| GET | `/me` | the acting user |
| GET | `/workspaces/{ws}/tables` | list tables |
| GET | `/tables/{t}` | **the whole table**: settings, all field definitions, views |
| POST | `/tables` | create a table (requires `workspaceId`) |
| PATCH | `/tables/{t}` | table settings: auto-run lives here |
| DELETE | `/tables/{t}` | delete a table |
| POST | `/tables/{t}/fields` | create a column |
| PATCH | `/tables/{t}/fields/{f}` | **edit a column**: prompt, formula, run condition, account |
| GET | `/tables/{t}/views/{v}` | one view, including its filter tree |
| POST | `/tables/{t}/views` | create a view (requires `name`) |
| PATCH | `/tables/{t}/views/{v}` | edit a view / its filter |
| GET | `/tables/{t}/views/{v}/records` | **list rows**: `?limit=&offset=`, default page 100 |
| GET | `/tables/{t}/records/{r}` | read one record |
| POST | `/tables/{t}/records` | insert rows; returns them with computed cells |
| PATCH | `/tables/{t}/records` | update rows |
| PATCH | `/tables/{t}/run` | **run columns**: body shape matters, see §6 |
| GET | `/actions?workspaceId={ws}` | provider registry (thousands of entries, tens of MB) |
| GET | `/sources?workspaceId={ws}` | sources; also accepts `?tableId=` |
| GET | `/workbooks/{wb}/tables` | tables in a workbook |

**Do not exist:** `GET /tables/{t}/fields`, `GET /tables/{t}/fields/{f}`, `PUT` on a field,
`GET /tables/{t}/records` (collection), `/views/{v}/records` without the table prefix, and any
`/fields/{f}/run`.

---

## 4b. The MCP server: sourcing and enrichment

**This is where finding new companies and people happens.** It is a *different product surface* from the REST
API: conversational, credit-metered, and built around Clay's own database rather than around your tables.

Connect it in the AI client (Clay's own page positions it for "ChatGPT, Codex or Claude"); it authenticates
through the workspace connection, not the REST API key. There is no HTTP contract to hand-roll: the tools
arrive in the assistant's tool list.

### What it does that the REST API cannot

| tool | purpose |
|---|---|
| `find-and-enrich-contacts-at-company` | **search people by criteria at a company**: the main sourcing tool |
| `find-and-enrich-list-of-contacts` | resolve specific *named* people to profiles |
| `find-and-enrich-company` | look up / research one company |
| `add-company-data-points` / `add-contact-data-points` | enrich an existing search (**costs credits**) |
| `query-objects` / `ask-question-about-accounts` | your OWN CRM/account data, not prospecting |
| `list_subroutines` / `run_subroutine` | workspace-defined Functions (lead scoring, account prep) |
| `get-task` / `get-task-context` | re-read a prior search instead of re-running it |
| `get-credits-available` / `get-current-workspace` | account state |

### People search filters

`find-and-enrich-contacts-at-company` accepts a rich filter set: this is the real ICP surface:

`job_title_keywords`, `job_title_exclude_keywords`, `names`, `profile_keywords` (anything in the profile),
`certification_keywords`, `languages`, `school_names`, `locations`, `locations_exclude`,
`current_role_min_months_since_start_date`, `current_role_max_months_since_start_date` (new hires vs tenured).

Filters combine with **AND**; values inside one array combine with **OR**. Keep compound titles as a single
string: `["VP Finance"]`, not `["VP", "Finance"]`, and be specific: `"Software Engineer"` rather than
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
with names, titles, LinkedIn URLs, locations and role start dates, plus a full company record, with
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
rows): do **not** start a new search to enrich something you already found. Check `get-task-context` first
in case the data is already there.

### Gotchas

- **`companyIdentifier` must be a domain or LinkedIn company URL.** Bare company names fail. Convert
  confidently (`"Stripe"` → `"stripe.com"`), and ask when ambiguous (`"Delta"`).
- **Person LinkedIn URLs are not company identifiers**: a common mix-up in `find-and-enrich-list-of-contacts`.
- **`query-objects` is not prospecting.** It queries your own synced CRM objects and returns nothing in a
  workspace with no CRM connected, which reads exactly like "no results" if you mistake it for search.
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

t     = c.create_table("Campaign: Companies")     # auto-run OFF by default here
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

Cloning carries over `authAccountId`, `model`, `useCase`, `runBudget` and the rate-limit rules: precisely
the settings whose absence causes silent non-execution.

**Three things a clone gets wrong, and `clone_column()` now handles all three:**

1. **The source's run condition comes with it.** Stripped, so you never inherit another column's gate.
2. **`optionalPathsInInputs` still points at the SOURCE table's field ids.** Those mean nothing in the new
   table, so every chip is effectively required, and any row with one blank chip dies with
   `ERROR_BLANK_TOKEN`. On a real run this was 136 of 250 rows. Pass `optional=[field_ids]`: mark every
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

### Run conditions: gate anything that costs money

A column with no run condition **runs on every row**. On action columns that is a direct bill.

```python
c.set_run_condition(t, fit,
    '{{%s}}?.toLowerCase()==="uk" && {{%s}}===true' % (country, qual),
    "Only run for UK rows that are qualified")
```

The gate is a JS expression in `typeSettings.conditionalRunFormulaText`, with an optional plain-English
`conditionalRunFormulaPrompt` shown in the UI. `conditionalRunFieldIds` is **derived** by Clay: read it,
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
`{"runMode":"NONE"}`: a 200 that did nothing. `clay.py` raises on `NONE`.

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
blanks: much safer for a retry.

**Batches are far faster than single rows.** Clay reports `runMode` as `INDIVIDUAL` for small sets and
`BULK` for large ones, and BULK is dramatically more efficient: 437 AI cells finished in about 20 seconds,
while the same column on one row took 5 minutes end to end. Do not pace your polling off a single-row test: 
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
magnitude: choosing the model is a real cost lever, not a detail.

**Polling: never per record.** Reading each cell individually in a loop earns a `504` from Clay's edge once
the batch is more than a handful of rows.

The cheapest way to watch a run is **`GET /v3/workspaces/{ws}/tables/{t}/fields/runstatus`**, which returns
status counts for *every field in the table* in a single request: no records fetched at all. `wait()` polls
through the view; `run_status()` is the lighter option when you only need to know whether a batch has
settled.

**Dependency order matters, but only when auto-run is on.** If column B consumes column A's output,
re-running A re-runs B **only if the table has `AUTO_RUN_ON: true`**. Measured on a two-column chain:
with auto-run on, running A fired B on every row; with auto-run off, B did not fire at all. This is the
single most useful safety fact in the API (§13).
Lookup columns are **snapshots**: changing a source table does nothing downstream until the lookup is
re-run. A correct chain is: source table → lookup refresh → dependent columns, in that order, each waited
on before the next.

**Expect a few percent of AI cells to fail per batch**, typically JSON that arrives as a raw string, so the
parent cell holds text while extracted sub-fields stay blank. Re-running usually fixes it. Occasionally a row
never recovers: cap your retries, exclude it, and note it, rather than burning credits on convergence that
isn't coming. Adding *"Return ONLY the raw JSON object. No code fences."* to the prompt reduces the rate.

---

## 7. Reading data back

**Rows are enumerated through a view: there is no records-listing route.**

```python
rows = c.rows(t)                       # [{'_id': 'r_...', 'column name': value, ...}]
recs = c.records(t, limit=1000)        # raw form, with per-cell metadata/status
one  = c.read(t, "r_abc")              # a single record
```

- Server default page size is **100**, always pass `limit`.
- ⚠️ **`offset` does not paginate.** It re-serves the first page. A loop that pages by offset
  silently returns roughly one page per view and stops: on one table an offset loop returned
  **1,268 rows for a table holding 7,093+**. The only working lever is a **high `limit`**.
- **Very large tables cannot be enumerated exhaustively.** Raising the limit kept returning more
  rows (4,650 → 5,032 → 6,716 → 7,093) and the read was *still* truncated at a limit of 12,000.
  Compare rows-returned against the limit you asked for; if they are close, raise it and re-read.
- Every table normally has an **`All rows`** view; `rows()` uses it by default. Pass a view name to read a
  filtered subset (`c.rows(t, view="Errored rows")`).
- View filters support types like `HAS_ERROR`, `RUN_CONDITION_NOT_MET`, `NO_RESULTS`, `EMPTY`: server-side
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

**Keep files as the source of truth, and verify it**: don't assume:

```bash
python examples/05_export_config.py   # pull live prompts/formulas into files
python examples/06_drift_check.py     # diff files vs live, exit non-zero on drift
```

Normalisation matters when diffing:

- **Prompts** are stored as a JS concatenation, and the UI flattens newlines. Collapse whitespace runs before
  comparing, and reconstruct chips as readable `{{Column Name}}`.
- **Formulas** are minified by Clay (`s=>s?s:""` vs your `s => s ? s : ""`). Strip whitespace entirely: it's
  insignificant in JS, or every re-paste looks like a change.
- Strip your own header comments from files; they never go into Clay.

Run the drift check at the start of any session that touches an existing table.

---

## 9. Cost control

Formulas and reads are free. **Action columns cost credits per cell.** The controls, in order of importance:

1. **Turn auto-run OFF.** It's table-level (`AUTO_RUN_ON`) and Clay defaults it **on**. With it on, paid
   columns fire whenever rows land or a column changes, including as a side effect of an API edit.
   `create_table()` here defaults it off.
2. **Gate every paid column** with a run condition (§5).
3. **Filter and enumerate, then run on that list.** Don't rely on a gate as a budget: gates protect against
   stray single-row runs, they don't stop you dispatching 400 rows by mistake.
4. **Iterate on 5-10 rows**, including deliberate edge cases, before running the full set.
5. **Order paid columns cheapest-first.** A free formula and a cheap classifier can cut the population
   before an expensive web-research column ever sees it: in the source workspace, two cheap gates cut
   394 rows to 157 before the expensive column ran.
6. **Audit periodically** (`examples/04_audit.py`): which tables have auto-run on, and which paid columns
   are ungated. The dangerous combination is both at once.

Web-research columns are non-deterministic: re-running the same row yields different findings. Never re-run
research "to be safe": you may lose a good result.

---

## 10. A complete worked workflow

The shape that works, end to end:

```
1. SOURCE      via the Clay MCP (§4b) or an external tool; then insert() the results into a table
2. GATE        cheap formulas first: free, and they shrink everything downstream
3. CLASSIFY    a cheap AI column, gated on the formula, to drop non-ICP rows
4. ENRICH      expensive research, gated on the classifier
5. GENERATE    output columns, gated on persona/segment + the gates above
6. QA          enumerate rows, check the output in Python, collect failing ids
7. RE-RUN      only the failures, then re-QA. Repeat until it converges or plateaus
8. EXPORT      filter to the ready set, write a CSV for the downstream tool
```

Notes from doing this in anger:

- **Steps 6-7 are a loop, and it plateaus.** Most defects clear in one or two passes; a small tail never
  converges because the prompt itself causes it. Recognise the plateau, flag those rows, move on.
- **Put a QA flag in the export** rather than silently dropping rows: let the human decide.
- **Cap output per company/account** if you're contacting several people at one organisation. Prompts keyed
  on company + segment produce near-identical text for two people at the same company with the same role: 
  visible and embarrassing if colleagues compare. Deduplicate on (company, segment).
- **Keep a stable join key** (a slug formula) so downstream replies can be matched back. Normalise accents.
- **Re-QA from live data, not yesterday's export.** They diverge the moment anyone edits anything.

---

## 11. How to explore the API safely

There is no documentation, so mapping is part of the work. What works:

1. **Guess the noun, then try every method.** Route matching is method-specific. A whole capability was
   wrongly written off as impossible because only `POST` was tried on the right path: it needed `PATCH`.
2. **Read the validation errors.** `details.bodyErrors.issues` is the API describing its own schema. The
   run-endpoint body shape was reverse-engineered entirely from those messages.
3. **Probe with ids that cannot exist.** `PATCH /tables/{real}/fields/f_FAKE` proves a route resolves
   (typed `NotFound`) while being incapable of changing anything.
4. **Do all shape-finding on a throwaway table.** Create it, learn on it, delete it. Never on live data.
5. **Search the web first.** One key route came from a third-party blog post, not from probing, though its
   documented body shape was wrong and still needed experiment.

### What not to do

**Never send a destructive method to a live table as a probe.** During this mapping a `DELETE` was fired at
a production table's `/fields` route to see what would happen. It returned `200` and happened to be a no-op: 
verified immediately afterwards, but that was luck. Destructive verbs belong on scratch tables only.

Related: probing `POST /tables/{t}/records/bulk|query|list` looks like endpoint discovery but is actually
`/records/{recordId}` **upserting**, which creates junk records literally named `bulk`, `query` and `list`.
If you ever see records with names like that, this is where they came from.

---

---

## 13. Auto-run, parking, and what actually triggers a run

This section exists because getting it wrong is how an API session creates records, sends
notifications, or bills for enrichment nobody asked for. Everything here was measured, not inferred.

### 13.1 A config `PATCH` never runs anything

The UI, when you edit a column, offers **"Save and run N rows"** or **"Save and don't run"**. Through
the API there is no such prompt, because **saving never runs**. It is permanently "Save and don't run".

Measured with `AUTO_RUN_ON: true`, which is the case where you would most expect a run:

| Action | Cells that re-ran |
|---|---|
| `PATCH` the column's `conditionalRunFormulaText` (its run condition) | **0** |
| `PATCH` the column's `inputsBinding` (a structural change) | **0** |
| `POST .../run` on three rows | **3** |

So the working pattern is **PATCH to save, then run explicitly on the row ids you choose**. That is
finer control than the UI, which only offers a fixed sample or everything.

> A run condition that references **its own column** is genuinely cyclical and is rejected
> (`400 "Dag is cyclical"`). Reference a different column.

### 13.2 `AUTO_RUN_ON: false` is the real safety switch

Two mechanisms stop a column firing by itself, and they are not equivalent:

| Mechanism | Scope |
|---|---|
| `typeSettings.runAsButton: true` ("parking") | that **one column** |
| `tableSettings.AUTO_RUN_ON: false` | **every column on that table** |

Measured on a chain where column B reads column A's output:

| Setup | Run A explicitly → did B fire? |
|---|---|
| `AUTO_RUN_ON: true`, B not parked | **yes, every row** |
| `AUTO_RUN_ON: false`, B not parked | **no, zero rows** |

**Turning auto-run off makes parking redundant for automatic runs**, and it is one call per table
instead of one per column. Three caveats that matter:

1. **It protects only that table.** Tables that source from it have their own `AUTO_RUN_ON`. A
   read-only lookup run on one table can wake a *different* table, which then runs its own paid
   columns and write actions. Enumerate what references a table before touching it.
2. **Parking still stops a human** clicking Run in the UI, and it survives someone switching auto-run
   back on. Worth keeping on shared production tables even when auto-run is off.
3. **Run conditions are enforced on explicit runs too.** A false gate yields
   `ERROR_RUN_CONDITION_NOT_MET` and the action does not execute. That is the real protection;
   parking only stops *automatic* runs.

### 13.3 `AUTO_RUN_MODE`: the "keep existing results" choice is a stored setting

Turning auto-run on in the UI asks whether to keep existing results. That answer is stored:

```
tableSettings.AUTO_RUN_MODE       = "keep_existing"
tableSettings.AUTO_RUN_LAST_ENABLED = <epoch ms>
```

- `keep_existing` appears to be the **only value the field ever takes**, and it is the default: a
  newly created table already has it.
- **It cannot be removed.** `PATCH /tables/{id}` **merges** `tableSettings` rather than replacing it,
  so omitting a key leaves it untouched. Useful: you cannot accidentally wipe dedupe settings by
  sending a partial object.
- **What it does:** with it set, switching auto-run on does **not** backfill cells that have never
  run. Observed directly: a table enabled with `keep_existing` left dozens of never-run action cells
  untouched.
- ⚠️ A helper that sets auto-run usually writes **only `AUTO_RUN_ON`**. To mirror what the UI does,
  PATCH the mode first, verify it, then flip `AUTO_RUN_ON` in a second call.

**Before switching auto-run on, count never-run cells on that table's action columns.** That number
is your exposure. A table can look idle and still be holding hundreds of rows ready to fire.

### 13.4 `extendedContent` is per-record only

`runId`, `finishedAt` and `inputsHash` live on a **single-record** read. A bulk records read **strips
them**. A freshness check that looks for `finishedAt` in a bulk response is measuring nothing, and
will report every row as stale forever.

Two related traps when judging whether a batch has finished:

- **Sample rows that have a value.** A completion check that only inspects populated cells reports
  "nothing outstanding" on a table where every row was gated off and nothing ran at all.
- **Census the statuses, not the values.** Count `SUCCESS`, `SUCCESS_NO_DATA`,
  `ERROR_RUN_CONDITION_NOT_MET` and never-run separately. "Has a value" hides all three failure modes.

### 13.5 `"Dag is cyclical"`, and why you cannot predict it

Binding a column to another column that is computed **downstream of it** is refused:

```
400 {"type":"BadRequest","message":"Dag is cyclical",
     "details":{"inputFieldIdsWithErrors":["f_..."]}}
```

⚠️ **A dependency graph built from the field config gives false negatives.** In one case a candidate
column was cleared by a scan of `inputFieldIds`, `formulaText`, `inputsBinding` and
`conditionalRunFormulaText`, and the change was still rejected: the chain ran through a formula column
whose body was **not present** in the config the API returns.

**So attempt the change and let the server adjudicate.** Make that safe by having your script restore
the table on failure and exit with a distinct code, so a batch runner can skip and continue rather
than abort halfway.

### 13.6 Composing run conditions without destroying the existing rule

Adding a guard to a column that already has business logic is the common case. Two rules:

```js
// keep the original verbatim, in parentheses
NEW_GUARD && ( ORIGINAL_CONDITION )
```

- **`&&` binds tighter than `?:`.** An original containing a ternary **must** be parenthesised or its
  meaning changes silently.
- **Make it idempotent.** Peel any guard already present before applying one, or a second run nests
  the guard inside itself and a third nests it again.
- ⚠️ **Never rebuild a gate from a live column whose condition reads `None`.** "Preserve the original
  and prepend a guard" then writes the guard *alone* and silently drops the business rule. The column
  will look correctly gated in every check. Rebuild from a stored snapshot instead.

### 13.7 Smaller things that cost time

| | |
|---|---|
| `runAsButton` | `true` = parked. `false` **or the key absent** = live; the server normalises `false` to absent |
| Gate values | conditions see the cell's **full structured value**, not the rendered grid string (which may be display text like `"Found 1 object(s)"`) |
| `removeBlankValues: true` | **silently drops a blank filter clause**. A two-field match degrades to a one-field match with no error. Require both fields non-empty in the run condition |
| Gated-off rows | a row whose condition is false has its **cell cleared** when run, even without force |
| Duplicate table names | names are not unique in a workspace. Address tables by id |
| `conditionalRunFormulaPrompt` | the human-readable line shown in the UI. It can be stale and is not what executes |
| Column id shape | real ids look like `f_0` followed by ~18 alphanumerics. A naive `f_` grep also matches template tokens such as `f_company` |
| Run attribution | **does not exist.** `/runs/{id}`, `/tables/{id}/runs`, `/activity`, `/history` and `/audit` all 404. You cannot determine from the API who queued a run |

### 13.8 A safe-change recipe

```
1. blast radius  which tables reference this one?
2. exposure      never-run cells on their action columns, especially create actions
3. protect       AUTO_RUN_ON=false on the target AND on every table downstream of it
4. snapshot      full table config to disk, before anything
5. change        PATCH config. Nothing runs (13.1). Read it back and assert it stuck.
6. run           only the read-only columns you intend, non-force, on ids you chose
7. verify        census cell statuses (13.4), not just values
8. measure       count SUCCESS cells on the TARGET's own write columns, before and after
9. leave parked  restoring write access is a human decision, taken after reading the numbers
10. restore      downstream tables back to their exact prior state, watched
```

**Judge a change by whether *that table's* write columns fired**, not by a system-wide record count:
other live tables create records legitimately while you work, and a system-wide delta will make you
halt for something that was never yours. And **check the sign**: a *negative* delta is a sampling
artefact from a truncated read (§7), not a creation.

---

## 14. Config traps the API will not warn you about

Everything here was established by controlled test, usually after it had already cost something.

### 14.1 🔴 The UI can silently delete what the API wrote

**If you set a binding the UI has no control for, any action that re-saves that column in the UI
destroys it.** Opening the column and unparking it is enough.

This was found the expensive way: a binding was written across two dozen tables, verified present on
every one by read-back, then handed to a colleague as a list of columns to unpark by hand. Afterwards
the binding was gone from every table they touched.

Proved by elimination rather than assumed:

| Test | Result |
|---|---|
| binding written, then left untouched for two minutes | **survives** — the server does not normalise it away |
| unpark and re-enable auto-run **through the API** | **survives**, every sibling binding intact |
| unpark **in the UI** | **binding destroyed** |

**The rule:** when a column carries a binding the UI cannot represent, do the whole sequence through
the API — park, change, unpark, restore auto-run — and **verify the binding after the restore, not
just after the write.** Re-reading immediately proves the write landed; it does not prove it will
still be there once a human opens the column.

This is the one case where the usual "restoring write access is a human decision" (§13.8 step 9) has
to be done by script. Ask for approval explicitly and explain why, rather than handing over a list.

### 14.2 🔴 A rollout script that runs twice corrupts its own record of prior state

The standard pattern — capture prior state at the top of each table's turn, so it can be restored —
**breaks silently if the batch is ever re-run.** The second pass reads back the changes the first
pass made and records *those* as the "prior" values.

Concretely: a batch that sets `AUTO_RUN_ON=false` and parks columns, then dies partway. Re-run it,
and for every table the first pass reached, prior state is now recorded as "was off, was parked" —
when the truth was "was on, was live". Dated snapshot files share the flaw when the filename is
`<table>-<date>-before.json`: the second run overwrites the first run's snapshot.

Restoring from that record does not put things back. It **switches off tables that were running**,
and nothing errors.

**Write prior state once and refuse to overwrite an existing entry.** Include a run id or timestamp
in snapshot filenames. When it has already happened, prior state can only be recovered from evidence
captured *before* the work — an earlier audit, or the snapshots of tables a failed run never reached.

### 14.3 Association bindings are not property bindings

Two different mechanisms that are easy to confuse, because both live in `inputsBinding`:

```python
"fields|<property>"                     # a property ON the record being created
"associationFields|toObjectTypeId"      # an explicit association to ANOTHER object
"associationFields|associationType"     # "<typeId>|<CATEGORY>"
"associationFields|toObjectId"          # "{{f_...}}" - the id to link to
```

They are independent: adding an association does not disturb a property that happens to link
elsewhere, and vice versa. Verify both after writing — the failure mode is a create column that
looks correct and links the record to only one of the two things you expected.

⚠️ `associationFields|*` is a **single** association slot. Writing it replaces whatever was there.
Strip existing `associationFields|` entries and re-add, so a re-run cannot nest or duplicate them.

⚠️ The association type id is direction-specific, and the generic and "primary" variants are
different ids. Read them from the CRM's own schema rather than guessing, and cross-check against how
existing pairs are actually linked — picking the "primary" variant marks every record you create as
the primary one, which is usually wrong when you attach several.

### 14.4 `"Dag is cyclical"` also blocks association bindings

§13.5 covers this for run conditions; it applies equally to `associationFields|toObjectId`. If the
column holding the id you want to link to is computed **downstream** of the create column, binding
to it closes a loop and the `PATCH` is rejected.

It is **deterministic** — retrying is pointless, and a retry wrapper that does not special-case it
turns a skippable table into a batch-stopping failure. Catch the message, restore that table, and
continue.

The fix is structural: the id has to arrive from the source rather than being derived in the same
table.

### 14.5 Table `updatedAt` is not a data-freshness signal

`table.updatedAt` moves when the **configuration** changes, not only when rows arrive. A table whose
newest row is months old reads as "updated today" the moment you edit it.

**`record.createdAt` is the only trustworthy arrival timestamp.** Both bulk and per-record reads
carry it (unlike `extendedContent`, §13.4).

### 14.6 View ordering is per-view, and it decides how expensive a freshness check is

Records come back through a view (§7), and **different views sort differently**. In practice large
tables tend to return newest-first, so a small `limit` reaches today's rows in a couple of seconds;
smaller tables often return oldest-first, but they read completely anyway.

So a "did this table produce anything today" check is cheap — **but only if you take the maximum
`createdAt` across what you read and treat a full page as a floor, not a count.** Since `offset` is
ignored, a bigger `limit` is the only lever.

### 14.7 Judging whether a table is "late" needs care

If you derive a schedule from a table's own history, measure the gap over **recent calendar days**,
not over the last N *active* days. The latter invents a daily rhythm for a table that runs every
third day, and for one that died a year ago whose final active days happened to be consecutive.

Two more rules that cut the false-alarm rate sharply:

- **A table with fewer than two active days has no rhythm.** It is a one-off import and can never be
  late. Reporting these was the single largest source of noise: one check flagged twenty tables to
  surface about six real ones, nearly all one-off imports.
- **A configured period beats an observed one.** Where the table carries a schedule setting, trust
  it — the observed gap is measured over a truncated read window and under-reads long schedules.

Schedule-related settings worth knowing: a flag for scheduled **runs** and a separate one for
scheduled **sources**. A recurring feed usually has the *source* scheduled and no scheduled run at
all, so looking only for scheduled runs will tell you nothing fires.

### 14.8 Formula engine: what is actually available

Confirmed by running each in a throwaway table rather than assuming:

| | |
|---|---|
| `Date.now()`, `Date.parse()` | available. `Date.parse("")` is `NaN`, and every `NaN` comparison is false — which is what makes a missing-date branch fail safe |
| `moment(...)` with `.isValid()` | available, and **necessary**: a guard that only tests for blank still emits an invalid-date string for unparseable input |
| `\b` in regex | **rejected**. For a word-boundary match, normalise non-letters to spaces, pad both sides, and substring-match the padded needle |
| Arrow function with several clauses inside `.some(...)` | works |
| Return type | still strings (§3 #9): compare against `"true"`, never `true` |

The word-boundary point is not academic. A naive substring match of a short place name inside a
free-text name matched hundreds of unrelated records in one test set — three letters that sit inside
ordinary words in that language. The padded-normalised form scored zero false matches on the same
set.

### 14.9 🔴 A formula column reports SUCCESS when the action it reads has failed

**This is the reason a broken column can sit in a workspace for weeks without anything flagging it.**

An action column fails on **every** row. A formula column that reads it evaluates cleanly — optional
chaining on a failed result yields `undefined`, which is not an error — and so reports `SUCCESS`
while returning nothing. Everything downstream of the formula then reads blank, and no status
anywhere says "broken".

Confirmed instance, measured:

| Column | Type | Status | Value |
|---|---|---|---|
| the lookup | action | **`ERROR` on 746 of 746 rows** | — |
| formula A reading it | formula | **`SUCCESS` on 746 of 746** | blank on all 746 |
| formula B reading it | formula | **`SUCCESS` on 746 of 746** | blank on all 746 |
| formula C reading it | formula | **`SUCCESS` on 746 of 746** | blank on all 746 |

**One broken action, three formulas all reporting healthy.** The underlying cause was a single token:
the lookup passed a whole webhook object where the working sister tables passed one field out of it.

**What follows for any health check:**

- **Census action columns by status, and separately check formula columns for values, not status.**
  A formula at 100% `SUCCESS` and 0% populated is the signature. Status alone will never show it.
- **Treat "column X is empty everywhere" as a first-class alert**, equal in weight to an error count.
  It is often more informative, because it survives the masking.
- When a column *is* empty everywhere, walk **upstream** to what feeds it before touching the column
  itself. The defect is rarely where the blank appears.

Cross-check a suspect column against a sister table that works. Near-identical tables diverging on
one binding is the fastest way to find this class of bug, and the diff is usually trivial once seen.

---

## 15. Diagnosing a pipeline that produces nothing

An engine that silently under-produces is harder than one that errors. Everything here came out of
chasing a single symptom — "most records have no related contacts" — through five wrong diagnoses.
The traps are generic; the discipline at the end is the part worth keeping.

### 15.1 🔴 An empty field is almost never the bug. Walk upstream.

Every wrong diagnosis in that investigation was **a field that was empty because something feeding
it was empty**. Each time, the blank looked like the defect and was actually the symptom:

| What looked broken | What was actually broken |
|---|---|
| records not linked to their parent | the linking column had no id to link with |
| that id was missing | the record it came from was never created |
| a country field empty, blocking a gate | the extraction step that should fill it produced nothing |
| every scored row scoring zero | the same extraction step |

**When a field is empty: find what writes it, check whether THAT ran, and repeat, until you reach
something that actually failed.** The bug is where the chain breaks, never where the blank appears.

This is the same rule as §14.9, stated as a procedure rather than a symptom, because knowing it and
applying it turn out to be different things.

### 15.2 🔴 A cell that FAILED is not a cell that never ran

`keep_existing` does not retry a cell that holds an error. It only fills cells that have **no status
at all**.

Observed: a column failed on every row because its input was blank. The input was then repaired and
populated correctly. The column sat at its old error, **auto-run did not pick it up**, and nothing
changed until it was run explicitly.

So repairing an input is only half a fix. **After fixing what feeds a column, that column must be
re-run on the affected rows** — and it will not tell you it is waiting.

### 15.3 🔴 An async action column stores the SUBMISSION, not the outcome

A column that calls an API which does work in the background — submit a job, results arrive later by
webhook — stores **the immediate response** and never updates it.

A payload captured at submission looks like this, and still looks like this a fortnight later:

```json
{ "status": "RUNNING", "progress_percentage": 0,
  "csv_result_file_url": null, "credits_deducted": 0 }
```

**A job that completed and a job that vanished are indistinguishable from the cell.** The status
field is a snapshot of the first millisecond, not a live view.

Consequences worth designing around:

- **Never read completion from the cell.** Match submissions against arrivals — both sides usually
  carry a correlation id — and diff the two sets.
- **Check whether the vendor reports empty results at all.** Many default to silence: if a search
  finds nothing, nothing is sent. Then "found nothing" and "lost in transit" are the same event from
  your side. If the API offers a flag for this, set it, or you are choosing to be blind.
- **Nothing watches the queue.** A submission with no arrival is invisible unless you build the
  check yourself.

### 15.4 A gate blocking 99% of rows usually means its input is empty

`ERROR_RUN_CONDITION_NOT_MET` at that rate is rarely bad gate logic. Read the gate, take the rows it
blocked, and check each input it depends on:

- **input empty on all blocked rows** → the gate is fine, something upstream never produced the
  value. Do not touch the gate.
- **input populated but not matching** → the condition is too narrow. A different, much smaller fix.

Worth distinguishing before anyone is asked to change anything: they lead to opposite work. In one
case a country-matching gate blocked 99% of rows on three tables; the value was **empty**, and where
it *was* filled the gate matched correctly and let the right rows through. The gate was never the
problem.

### 15.5 Scanning one action type and concluding about the system

A mechanism can be implemented by a **dedicated action type** rather than as a binding on the column
you expect. Searching the obvious place and finding nothing is not evidence of absence.

Concretely: an association can be a binding on a create column, **or** its own
`*-create-association` column. Scanning only create columns produced the confident and wrong
conclusion that nothing in the workspace created associations. Dozens of dedicated columns existed
and most were working.

**Before concluding a capability is missing, enumerate every `actionKey` in the workspace and look
for anything that could implement it.**

### 15.6 The grid value is a label. The payload is elsewhere.

§13.7 notes that gates bind the structured value. The sharper version, because it silently breaks
analysis scripts:

A cell holding a rich object renders in the grid as a short human string. One integration column's
grid value was literally `"Received September 2nd, 2026"` while the actual payload — containing the
correlation id needed to match it — was only in `externalContent.fullValue` on a **per-record** read
(§13.4).

A detector built on the grid value found **zero** matches and would have "proved" that no result
ever came back. **Any script that greps cell values for structured data must read per-record, and
must be sanity-checked against a case known to be present.** A scan returning zero is a result to
distrust, not to report.

### 15.7 Duplicated tables share field ids

Copying a table clones its field ids. Three separate tables were seen carrying the identical column
id. So a `f_…` grep across the workspace gives false positives, and a worklist keyed by field id
silently collapses entries.

**Key anything cross-table by `(tableId, fieldId)`, never by field id alone.** Only an explicit
`tableId` binding proves a real link between two tables.

### 15.8 The discipline, since the traps above are only half the problem

Five wrong diagnoses were not five unlucky measurements. They shared one habit: **reporting a
finding as the cause the moment it looked real, without checking whether it accounted for the size
of the gap.**

```
1. denominator   exclude populations that structurally cannot qualify
2. arithmetic    does this cause account for the gap? "16% x 73% = 12%, observed 26%"
                 exposes a missing population instantly
3. populations   never blend two in one sample; split by origin, age, source first
4. language      "a contributing cause" until the arithmetic closes
5. consequence   scale verification to what the claim CAUSES. A sentence is cheap to retract;
                 a ticket, a document or a config change dispatches a person. Verify first.
```

Step 2 would have caught three of the five, using numbers already in hand. Step 5 is the one that
matters most in a shared codebase: the cost of being wrong is not your time, it is someone else's.

---

## 12. Known limits and open questions

**Confirmed limits**

- **The REST API does not source.** It builds and runs tables. Sourcing is the MCP's job (§4b), or an
  external tool. Either way the results reach a table through `insert()`.
- **No bulk "run all".** You must enumerate record ids.
- **No records-listing route.** Enumeration is view-scoped (this is fine, just not obvious).
- **`offset` is ignored** (§7). Paging is by `limit` only, and very large tables cannot be read
  exhaustively at all. Any count taken from a truncated read is wrong, and if you then *run* the
  rows you read, everything past the limit keeps stale values.

**Unexplored: worth investigating if you need them**

- **Webhook sources.** `GET /sources?workspaceId=` lists them; creating an inbound webhook source was never
  attempted. This is the pattern Clay's own docs describe for continuous programmatic row entry, and it may
  run enrichments on arrival.
- **Server-side filtered views** via `POST /tables/{t}/views`: create a view for "errored rows" and read
  only those, instead of filtering locally.
- **Running Claygent (web-research) columns via the API.** Standard AI columns are proven to execute; the
  research variant is proven only to be *creatable*. Verify before depending on it.
- **`workbookId`** appears on every table and is barely explored beyond `/workbooks/{wb}/tables`.

- **MCP ↔ REST are not joined.** An MCP search returns `taskId` / `entityId`; a table holds `t_` / `r_` ids.
  Nothing links them automatically: you carry the results across yourself.

**A closing warning.** This API is undocumented and can change without notice. Everything here was true when
tested against a live workspace. Re-verify anything load-bearing before trusting it in production, and treat
a surprising result as new information about the API rather than a bug in your code.
