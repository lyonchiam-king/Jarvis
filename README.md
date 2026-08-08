# SME Website Outreach Pipeline

Finds local businesses that don't have a website, researches them, and drafts
personalised outreach — with Google Sheets as both the database and the
dashboard.

Region-agnostic by design. Penang is a config file, not a code path. Pointing
this at Manchester or Jakarta is a YAML edit.

**Status:** Stages 1 (discovery) and 2 (research) are complete and tested.
Stages 4–5 are scaffolded and will refuse to run until built.

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

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

cp .env.example .env          # fill in PLACES_API_KEY (+ ANTHROPIC_API_KEY for Stage 2)
outreach preflight            # verifies key, OAuth, Sheet access
outreach init-sheet           # creates the spreadsheet, prints SHEET_ID
# paste SHEET_ID into .env
outreach run discovery --dry-run   # see what you'd get, write nothing
outreach run discovery             # write to the Sheet
outreach run research --limit 5    # research five leads, read the briefs
outreach run research              # then the full batch
```

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
send call — not from the scope. Worth knowing rather than assuming.

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
| `outreach run research --limit 5` | Same, but process fewer than the batch size — good for a first trial. |

Global flags: `--config PATH` to override the campaign file, `-v` for debug logging.

### 100 companies per run, and it never repeats work

Every stage processes at most **`limits.batch_size` (default 100)** businesses
per invocation. Run the command again for the next batch.

Re-running is safe and cheap because each stage skips what it has already done:

- **Discovery** keys on `place_id`, so a second run over the same area refreshes
  existing rows instead of duplicating them.
- **Research** only picks up leads still at `Discovered` that have no row in the
  `Research` tab. A lead already researched costs **zero** Places requests and
  **zero** Claude calls on the next run — verified by a test.

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

117 tests, no network access required. Covers website classification (including
lookalike domains like `notfacebook.com`), Places pagination and Place Details
parsing, discovery filtering, forward-only stage transitions, the compliance
gate, manual-column write protection, and for Stage 2: the skip-already-done
selection logic, the batch cap, checkpoint flushing, per-lead failure isolation,
email-candidate filtering, login-wall handling, Claude refusal/truncation
paths, and the assertion that raw review text never reaches the Sheet.

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
  providers/
    places.py         Places API (New): text search + place details
    web.py            page fetching, email extraction
    social.py         best-effort Facebook / Instagram lookup
    llm.py            Claude Opus 5 structured research briefs
  stages/             one module per pipeline stage
tests/
```

---

## Not built yet

Stages 4–5 are scaffolded — `outreach run site-brief` and `run outreach-draft`
exit with a clear message rather than pretending. Still to come: Lovable prompt
generation (coded behind `lovable.enabled: false` so no credits are spent by
accident) and Gmail draft creation.
