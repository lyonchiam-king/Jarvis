# SME Website Outreach Pipeline

Finds local businesses that don't have a website, researches them, and drafts
personalised outreach — with Google Sheets as both the database and the
dashboard.

Region-agnostic by design. Penang is a config file, not a code path. Pointing
this at Manchester or Jakarta is a YAML edit.

**Status:** All five stages are built and tested — discovery, research, site
briefs, site build, outreach drafts. Nothing sends automatically.

---

## How it works

```
  Google Places API (New)
          │  businesses with no website
          ▼
  ┌───────────────┐
  │  Leads tab    │◄──── you edit Priority / Notes / Next Action by hand
  └───────┬───────┘      (automation never touches those columns)
          │
          ├─► Research tab      reviews + social → structured brief
          ├─► Site Briefs tab   ready-to-paste Lovable prompt
          ├─► Outreach Log tab  Gmail draft + WhatsApp text
          └─► Errors tab        anything that failed, per lead
```

Every stage is separately runnable. Nothing sends automatically — email lands
in your Gmail **drafts** and you press send.

---

## Just want to use it?

**Double-click `run.bat`** (Windows) or `run.sh` (macOS/Linux). You get a menu;
type a number. No commands to remember, and it builds its own environment on
first run.

**[DAILY.md](DAILY.md)** is the day-to-day guide, written for someone who
doesn't use a terminal.

The rest of this README is for setting it up the first time, and for
understanding how it works.

---

## Quick start

Needs **Python 3.10 or newer**. macOS ships 3.9 — if that's what you have,
`brew install python@3.12` first. On Windows, install from python.org and tick
**"Add Python to PATH"**.

**macOS / Linux:**

```bash
python3 -m venv .venv
source .venv/bin/activate     # ← without this, `outreach` isn't on your PATH
pip install -e ".[dev]"

mkdir -p credentials          # your OAuth client JSON goes here (gitignored)
cp .env.example .env          # fill in PLACES_API_KEY + ANTHROPIC_API_KEY
nano .env
```

**Windows (PowerShell):**

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # see note below
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

mkdir credentials
cp .env.example .env
notepad .env
```

> Activating a venv in PowerShell fails with *"running scripts is disabled on
> this system"* unless you relax the execution policy. `-Scope Process` applies
> to that window only — nothing permanent — and you repeat it in each new
> window. There is no `nano` on Windows; `notepad` is built in.

Either way, your prompt should gain a `(.venv)` prefix. Then, in order:

```bash
outreach preflight            # checks key, billing, OAuth, Sheet access
outreach init-sheet           # creates the spreadsheet, prints SHEET_ID
# paste SHEET_ID into .env

outreach run discovery --dry-run   # see what you'd get, write nothing
outreach run discovery             # write to the Sheet
outreach run research --limit 5    # research five leads, read the briefs
outreach run research              # then the full batch
outreach run site-brief            # Lovable prompts into the Sheet
outreach run outreach-draft --dry-run   # see the copy before spending
outreach run outreach-draft        # drafts land in your Gmail
```

Every new terminal needs the activate step again (`source .venv/bin/activate`,
or `.venv\Scripts\Activate.ps1` on Windows). If you'd rather skip it, use the
full path instead: `.venv/bin/outreach preflight` — or on Windows,
`.venv\Scripts\outreach.exe preflight`.

Each stage handles up to **100 businesses per run** and skips anything it has
already done, so running a command twice is safe and costs nothing extra.

---

## First-time Google Cloud setup

One Cloud project covers everything. Roughly 10 minutes.

### 1. Create the project and enable four APIs

[console.cloud.google.com](https://console.cloud.google.com) → create a project
(e.g. `sme-outreach`). Then **APIs & Services → Library**, enable each of:

| API | Used for |
|---|---|
| **Places API (New)** | Stage 1 discovery |
| **Google Sheets API** | The lead database |
| **Gmail API** | Stage 5 drafts |
| **Google Drive API** | Creating the spreadsheet |

> Enable **Places API (New)**, not the legacy "Places API". Google deprecated
> the legacy endpoints for new customers in March 2025 and this project only
> speaks to `places.googleapis.com/v1`.

### 2. Link a billing account

Places API (New) returns **HTTP 403 with no results** if billing isn't active,
even inside the free tier. **Billing → Link a billing account.**

### 3. Create the Places API key

**APIs & Services → Credentials → Create credentials → API key.**

Restrict it — an unrestricted key that leaks is an open tab on your card:
- *API restrictions* → Restrict key → **Places API (New)**
- *Application restrictions* → **None** (this runs from your machine, so an
  IP or referrer restriction will just break it)

Paste into `.env` as `PLACES_API_KEY`.

### 4. Configure the OAuth consent screen

**APIs & Services → OAuth consent screen:**
- User type: **External**
- App name, your email for both support and developer contact
- Scopes: you can leave this blank; the app requests them at runtime
- Test users: add your own Google account

Then — **this part matters** — go back to the consent screen and set
publishing status to **In production**.

> ### You log in once. Not every 7 days.
>
> The 7-day expiry is a penalty Google applies **only while the app sits in
> "Testing"**. It is not normal behaviour and you should never experience it.
>
> | Publishing status | Refresh token |
> |---|---|
> | **Testing** | Expires every 7 days → constant re-login |
> | **In production** | **Does not expire** — one-time login |
>
> "In production" sounds like it means verification and app review. It doesn't.
> Your app stays **unverified**: you click through one "Google hasn't verified
> this app" screen (**Advanced → Go to (app) (unsafe)**) on that single login,
> and never see it again. That warning exists to protect *strangers* from
> trusting an unknown app — you are the only user and you wrote it.
>
> After that, `credentials/token.json` refreshes itself silently. The token
> only dies if you revoke access, change your Google password, or don't run the
> pipeline for 6 months.

### 5. Create the OAuth client

**Credentials → Create credentials → OAuth client ID → Desktop app.**

Download the JSON to `credentials/oauth_client.json`. The `credentials/`
directory is gitignored.

```bash
mkdir -p credentials
mv ~/Downloads/client_secret_*.json credentials/oauth_client.json
```

First command that needs Google opens a browser once and writes
`credentials/token.json` (mode `600`, gitignored).

### Why OAuth and not a service account

A service account **cannot create drafts in your mailbox** without Workspace
domain-wide delegation, which is substantially more setup and more privilege
than a solo founder wants. OAuth authenticates the pipeline *as you*.

### On the Gmail scope

There is **no Google scope that permits creating drafts but forbids sending**.
`gmail.compose` is the narrowest one that exists, and it technically allows
send. The "never auto-send" guarantee comes from this codebase containing no
send call — enforced by a test that parses every source file. See
[Nothing is sent. Ever.](#nothing-is-sent-ever) for the detail.

Scopes requested:

```
https://www.googleapis.com/auth/spreadsheets    read/write the Sheet
https://www.googleapis.com/auth/drive.file      only files this app creates
https://www.googleapis.com/auth/gmail.compose   create drafts
```

---

## Commands

| Command | What it does |
|---|---|
| `outreach preflight` | Checks config, Places key, OAuth, and Sheet access. Costs one Places request. |
| `outreach init-sheet` | Creates the spreadsheet with all five tabs. Idempotent — repairs missing tabs on an existing Sheet. |
| `outreach compliance` | Prints the outreach rules on file for the campaign's country. |
| `outreach run discovery` | **Stage 1.** Finds no-website businesses, writes to `Leads`. |
| `outreach run discovery --dry-run` | Searches and reports without touching the Sheet or needing Google credentials. |
| `outreach run research` | **Stage 2.** Pulls reviews + social, writes a brief per lead to `Research`. |
| `outreach run site-brief` | **Stage 4.** Writes a ready-to-paste Lovable prompt per researched lead. Free — no builds. |
| `outreach run site-build` | **Stage 4b.** Builds the sites you marked `Approved`. Spends Lovable credits. |
| `outreach run outreach-draft` | **Stage 5.** Creates Gmail **drafts** and WhatsApp text. Sends nothing. |
| `--limit N` | On any stage: process fewer than the batch size. Good for a first trial. |
| `--dry-run` | On `discovery` and `outreach-draft`: do the work, write nothing. |

Global flags: `--config PATH` to override the campaign file, `-v` for debug logging.

### 100 companies per run, and it never repeats work

Every stage processes at most **`limits.batch_size` (default 100)** businesses
per invocation. Run the command again for the next batch.

Re-running is safe and cheap because each stage skips what it has already done:

- **Discovery** keys on `place_id`, so a second run over the same area refreshes
  existing rows instead of duplicating them.
- **Every processing stage** only picks up leads whose upstream work is done and
  that have no row on its own output tab. A lead already researched costs
  **zero** Places requests and **zero** Claude calls on the next run; a lead
  already drafted costs zero Claude calls and creates no duplicate draft. Each
  of those is verified by a test.

Research also **checkpoints every 10 leads** (`research.checkpoint_every`). If a
run dies at lead 47, the first 40 are already saved and the next run starts at
41 — it doesn't re-buy the reviews or re-spend the tokens.

---

## The Sheet

Five tabs, all keyed on **Lead ID** (the Google `place_id`) in column A. That
key is what makes re-runs idempotent — running discovery twice over Georgetown
refreshes rows instead of duplicating them.

### `Leads` — your dashboard

| Cols | Written by | Fields |
|---|---|---|
| A–O | pipeline | Lead ID, Stage, Business Name, Category, Address, Phone, Rating, Review Count, Website Status, Website URL, Maps URL, Campaign, Country, Discovered At, Last Refreshed |
| P–R | **you** | Priority, Owner Notes, Next Action |

Stage flows `Discovered → Researched → Site Briefed → Site Built → Email
Drafted → Sent → Replied → Closed`, plus `Disqualified` for opt-outs.

**Transitions are forward-only.** Re-running discovery over a lead you already
emailed will not reset it to `Discovered` — so you can't accidentally email
someone twice. `Disqualified` is terminal and no automated stage climbs out of
it.

### Other tabs

- **`Research`** — tone, services, price tier, customer praise/pain points, suggested website angle, social handle/bio/post, email found and where it came from
- **`Site Briefs`** — the Lovable prompt, plus a manual `Approval Status` column you set to `Approved` before anything is built
- **`Outreach Log`** — Gmail draft ID and link, WhatsApp message text to copy, plus manual `Status` / `Sent At` / `Notes`
- **`Errors`** — timestamp, stage, lead, error type, message, whether retrying could help

### Your columns are safe

Columns marked manual in `src/outreach/schema.py` are **never written by
automation**. The upsert computes contiguous runs of automated columns and
writes only those A1 ranges — a refresh of `place-1` writes `Leads!A2:O2` and
nothing else. This is enforced by tests in `tests/test_sheets.py`.

---

## Stage 2 — research

```bash
outreach run research --limit 5     # try it on five leads first
outreach run research               # then the full batch of 100
```

Per lead, in order:

1. **Google Place Details** — reviews, editorial summary, price level, opening hours
2. **Social lookup** (only for `social_only` leads) — handle, bio, recent post
3. **Email discovery** — `mailto:` links and published addresses from the social page or any real site
4. **Claude Opus 5** — turns all of it into a structured brief with a concrete pitch angle
5. **Write** the `Research` row and advance the lead's Stage to `Researched`

Steps 2 and 3 fail often and that's expected — a `login required` status on a
Facebook page is Tuesday, not a bug. The brief still gets written from the
Google reviews alone.

**Raw review text is never stored.** It goes to the model, the model returns a
summary, and the source text is discarded — this is what keeps the Sheet inside
Google's caching terms. A test asserts it.

### What this costs per run

For 100 leads: **100 Place Details requests** (the Enterprise SKU — reviews are
the most expensive field set in the pipeline), up to 200 page fetches (free),
and **100 Claude Opus 5 calls**.

`research.effort` is the dial. It defaults to `medium`, which is genuinely
strong on Opus 5 for summarisation work. Raise it to `high` if the briefs read
thin; the model itself stays Opus 5 either way.

Start with `--limit 5`, read the briefs, then decide whether to tune `effort`
or `max_reviews` before spending on a full batch.

---

## Stage 4 — site briefs

```bash
outreach run site-brief
```

Claude turns each research brief into a **structured site spec** (goal, pages,
hero copy, sections, CTA, visual direction), and a deterministic function
renders that spec into the prompt you paste into Lovable. Same spec in, same
prompt out — so the prompt is reviewable and testable rather than a one-off
generation.

The prompt lands in the `Site Briefs` tab with `Approval Status` seeded to
`Pending`. **This command never spends Lovable credits** — a test asserts it.

### Stage 4b — actually building

```bash
outreach run site-build
```

Three independent gates stand in front of any spend:

1. `lovable.enabled: true` in the campaign config *(default: false)*
2. `Approval Status` reads `Approved` — that column is **manual**, so automation
   cannot approve its own spending
3. An interactive confirmation per project *(unless `require_confirmation: false`)*

With the Lovable connector absent, `site-build` tells you how to connect it
rather than failing obscurely. Until then the workflow is: read the prompt in
the Sheet, paste it into Lovable yourself. The prompt is the deliverable; the
API call is convenience.

---

## Stage 5 — outreach drafts

```bash
outreach run outreach-draft --dry-run   # print the copy, write nothing
outreach run outreach-draft
```

Per lead: Claude writes a subject, an email body, and a **separate** WhatsApp
opener (not the email pasted shorter). The signature and compliance footer are
appended by code, not by the model, so they're identical in every draft and
can't be paraphrased away.

- **Email address found** → a draft appears in your Gmail drafts folder
- **No email** → WhatsApp text only, in the `WhatsApp Message` column to copy

Most no-website SMEs land in the second bucket. That's the expected outcome, not
a failure — it's why WhatsApp text is generated for every lead regardless.

### Nothing is sent. Ever.

There is **no `.send()` call anywhere in this codebase**, and
`tests/test_outreach_draft.py` parses the AST of every file under `src/` on
every test run to keep it that way. I verified the guard catches a real
violation rather than passing vacuously.

This matters because the OAuth scope cannot give you this guarantee: Google
publishes no scope that permits creating a draft while forbidding a send.
`gmail.compose` is the narrowest one that exists and it technically allows both.
The absence of the call **is** the safety property.

### `postal_address` is required

Stage 5 refuses to run until `outreach.postal_address` is set:

```yaml
outreach:
  postal_address: "COAI, 12 Some Road, 10450 Penang, Malaysia"
```

A real postal address is a legal requirement for commercial email under
CAN-SPAM, GDPR/ePrivacy, and Australia's Spam Act. It costs you one config line
and it goes in the footer of every draft, along with a working opt-out. The
compliance gate for the campaign's country also runs before any copy is written.

---

## Changing city, category, or country

Copy a campaign file, edit values, point at it. No code changes.

```bash
cp config/campaigns/penang-my.yaml config/campaigns/kl-cafes.yaml
```

```yaml
campaign:
  name: kl-cafes
  category: "cafe"
  location: "Bangsar, Kuala Lumpur, Malaysia"
compliance:
  country_code: MY
```

```bash
outreach --config config/campaigns/kl-cafes.yaml run discovery
# or set CAMPAIGN_CONFIG in .env
```

`config/campaigns/example-manchester-uk.yaml` shows a cross-border move.

Use one Sheet per campaign (`SHEET_ID` per campaign) if you want clean
separation, or share one Sheet and filter on the `Campaign` column.

---

## Compliance

Only publicly listed business information is used — trading name, business
address, business phone, public reviews. No personal data about individuals is
collected, and no login-walled content is accessed.

**Discovery runs anywhere. Outreach is gated by country.** `compliance.py`
carries a rule table:

| Severity | Meaning | Examples |
|---|---|---|
| `OK` | B2B cold outreach broadly permitted | 🇲🇾 MY, 🇺🇸 US |
| `CAUTION` | Permitted with conditions you must meet first | 🇬🇧 GB, EEA, 🇸🇬 SG, 🇦🇺 AU, 🇹🇭 TH, 🇮🇩 ID |
| `BLOCKED` | Prior consent generally required | 🇨🇦 CA |

A `CAUTION` country refuses to run outreach until you set
`compliance.acknowledged: true` in the campaign YAML — a deliberate "I have
read this" gesture. `BLOCKED` refuses regardless; changing that requires a code
edit, not a config toggle. An unknown country code defaults to `CAUTION`, not
to permitted.

Run `outreach compliance` to see the rules for your current campaign.

*None of this is legal advice. It exists to make you stop and check before
messaging strangers in an unfamiliar jurisdiction.*

### Data retention

Google's Places terms allow `place_id` to be stored indefinitely but restrict
caching of other Places content (name, address, phone, reviews) to about
**30 days**. That's why `Leads` carries a `Last Refreshed` column: `place_id`
is the durable key and everything else is a working cache you re-pull. Raw
review text is deliberately **not** persisted — Stage 2 summarises it into a
brief and discards the source.

---

## Design notes

**A Facebook page is not a website.** Google returns whatever URL the owner
supplied in `websiteUri`, and for small businesses that's very often a Facebook
page or a Linktree. Classifying those as "has a website" would throw away the
warmest leads in the set — owners who already decided they want to be findable
online but got stuck. So `Website Status` is three-valued: `none`,
`social_only`, `real`. Whether `social_only` counts as a lead is
`discovery.treat_social_as_no_website` (default: yes).

**Places doesn't return email addresses.** Businesses without websites usually
have no discoverable email either — it's the same population. Phone is what
Places reliably gives you, which is why a dedicated email-discovery stage
exists and why WhatsApp text is generated for every lead.

**One bad lead never kills a run.** Each lead is processed inside
`ErrorLog.guard`; anything that escapes is written to the `Errors` tab with a
`Retryable` flag and the loop moves on. Errors are buffered and flushed in one
write at the end, so a Sheets hiccup can't take down a run either. In research
this extends to checkpoints: a failed Sheet write is logged and the run keeps
going rather than discarding the leads it has already briefed.

**Social lookups are expected to fail.** Facebook and Instagram serve a login
wall to anonymous requests, so `login required` is the normal status, not an
error. The lookup still salvages the handle from the URL, and the brief is
written from Google reviews regardless. Nothing here bypasses a login or
touches anything behind authentication.

**Cost control.** Places bills per request, and the field mask selects the
pricing tier. `websiteUri` and `rating` put us in the Pro tier — unavoidable,
since `websiteUri` is the entire point. Mitigations: the field mask requests
nothing beyond what's used, `max_results` caps a run, `max_pages` is a hard
stop against a runaway config, and `--dry-run` costs the same as a real run but
writes nothing, so prefer it while tuning filters.

---

## Tests

```bash
.venv/bin/python -m pytest -q
```

155 tests, no network access required. Alongside the usual coverage — website
classification (including lookalike domains like `notfacebook.com`), Places
pagination and Place Details parsing, forward-only stage transitions,
manual-column write protection, per-lead failure isolation, Claude
refusal/truncation paths — these are the ones that encode a promise:

| Test | Promise |
|---|---|
| `test_no_send_call_exists_anywhere_in_the_source` | Nothing can send email |
| `test_reviews_are_summarised_but_never_written_to_the_sheet` | Places caching terms respected |
| `test_parse_details_does_not_capture_review_author_identity` | No personal data collected |
| `test_manual_column_values_are_never_sent_in_the_payload` | Your Sheet edits survive re-runs |
| `test_prompt_generation_never_spends_credits` | Stage 4 is free |
| `test_enabled_adapter_still_needs_your_manual_approval` | Automation can't approve its own spend |
| `test_rerun_does_no_work_when_everything_is_researched` | Re-running costs nothing |
| `test_preflight_requires_a_postal_address` | Drafts are legally shaped |

---

## Layout

```
config/campaigns/     campaign YAML — the only place region lives
credentials/          OAuth client + token (gitignored)
src/outreach/
  cli.py              command entry point
  config.py           YAML + env loading and validation
  auth.py             OAuth flow, all scopes
  sheets.py           upsert that respects manual columns
  schema.py           tab and column definitions (single source of truth)
  stage.py            forward-only stage transitions
  compliance.py       per-country outreach gate
  errors.py           per-lead error capture
  models.py           Lead, WebsiteStatus
  selection.py        which leads a stage should process (the resume logic)
  providers/
    places.py         Places API (New): text search + place details
    web.py            page fetching, email extraction
    social.py         best-effort Facebook / Instagram lookup
    llm.py            Claude Opus 5: research briefs, site specs, outreach copy
    gmail.py          draft creation -- contains no send call
    lovable.py        Lovable adapter, disabled behind three gates
  stages/             one module per pipeline stage
tests/
```

---

## What's left

The pipeline runs end to end. What remains is operational rather than
structural:

- **Connect Lovable** if you want `site-build` to work — otherwise paste prompts
  by hand, which costs you nothing and gives you the same site.
- **Auto-detecting replies** so `Status` moves from `Sent` to `Replied` without
  you editing the Sheet. Doable with a Gmail thread watch; deliberately not
  built yet because it needs a broader Gmail scope than `gmail.compose`, and
  that's a trade worth making deliberately rather than by default.
