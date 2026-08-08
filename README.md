# SME Website Outreach Pipeline

Finds local businesses that don't have a website, researches them, and drafts
personalised outreach — with Google Sheets as both the database and the
dashboard.

Region-agnostic by design. Penang is a config file, not a code path. Pointing
this at Manchester or Jakarta is a YAML edit.

**Status:** Stage 1 (discovery) is complete and tested. Stages 2–5 are
scaffolded and will refuse to run until built.

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

cp .env.example .env          # then fill in PLACES_API_KEY
outreach preflight            # verifies key, OAuth, Sheet access
outreach init-sheet           # creates the spreadsheet, prints SHEET_ID
# paste SHEET_ID into .env
outreach run discovery --dry-run   # see what you'd get, write nothing
outreach run discovery             # write to the Sheet
```

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

> While the app sits in **Testing**, Google expires your refresh token every
> **7 days** and you re-authenticate constantly. "In production" stops that.
> The app stays unverified, so you'll see a "Google hasn't verified this app"
> screen once — click **Advanced → Go to (app) (unsafe)**. That warning is
> about *other people* trusting your app; you are the only user.

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

Global flags: `--config PATH` to override the campaign file, `-v` for debug logging.

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

- **`Research`** — tone, services, price tier, customer praise/pain points, suggested website angle, social bio and recent post
- **`Site Briefs`** — the Lovable prompt, plus a manual `Approval Status` column you set to `Approved` before anything is built
- **`Outreach Log`** — Gmail draft ID and link, WhatsApp message text to copy, plus manual `Status` / `Sent At` / `Notes`
- **`Errors`** — timestamp, stage, lead, error type, message, whether retrying could help

### Your columns are safe

Columns marked manual in `src/outreach/schema.py` are **never written by
automation**. The upsert computes contiguous runs of automated columns and
writes only those A1 ranges — a refresh of `place-1` writes `Leads!A2:O2` and
nothing else. This is enforced by tests in `tests/test_sheets.py`.

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
write at the end, so a Sheets hiccup can't take down a run either.

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

58 tests, no network access required. Covers website classification (including
lookalike domains like `notfacebook.com`), Places pagination and error
handling, discovery filtering, forward-only stage transitions, the compliance
gate, and the manual-column write protection.

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
  providers/places.py Places API (New) client
  stages/             one module per pipeline stage
tests/
```

---

## Not built yet

Stages 2–5 are scaffolded — `outreach run research` and friends exit with a
clear message rather than pretending. Still to come: research briefs from
reviews and social, email discovery, Lovable prompt generation (coded behind
`lovable.enabled: false` so no credits are spent by accident), and Gmail draft
creation.
