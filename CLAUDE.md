# Jarvis -- SME outreach pipeline

Finds small businesses with no website, researches them, writes a website brief,
builds a demo site, and drafts the outreach message. The demo site **is** the
pitch: nothing is sent to a business until their site exists.

**Owner:** Lyon Chiam (COAI, Penang). Works on **Windows/PowerShell**, is not a
programmer, and drives the tool through `Dashboard.bat` -- not the CLI. Anything
that only works on Linux, or only works from a terminal, is not finished.

**Stack:** Python 3.11+, few dependencies. The dashboard is a single Flask file
serving one page of hand-written HTML/JS -- no templates on disk, no frontend
build step, no npm. Google Sheets is the database.

## Commands

```bash
.venv/bin/pytest -q          # the whole suite -- must be green before any push
.venv/bin/pytest tests/test_research.py -q
python -m outreach run discovery --limit 10
python -m outreach dashboard
```

On Windows the equivalents are `run.bat` and `Dashboard.bat`.

## Layout

```
src/outreach/
  config.py        campaign YAML + .env loading, all validation at load time
  selection.py     who gets picked next, and in what order -- shared by stages
  sheets.py        Google Sheets client; column-position based
  schema.py        tab + column definitions
  stage.py         the Stage enum and legal transitions
  dashboard.py     the local web UI Lyon actually uses
  campaign_edit.py surgical YAML rewriting that preserves comments
  env_edit.py      writes keys into .env without ever reading them back
  modelcheck.py    proves a model can produce every required shape
  stages/          discovery, research, site_brief, outreach_draft
  providers/       places, sheets auth, llm, social, web, lovable, sitegen
  images.py        generated page imagery -- mood and texture, never the premises
  sitecheck.py     refuses a generated page that would embarrass you
  screenshot.py    photographs the built page for the WhatsApp message
  slug.py          deterministic, unguessable per-business URL paths
config/campaigns/  penang-my.yaml is the live campaign
tests/             995 tests, pytest, no CI -- the suite is the only gate
```

## Rules that came from real bugs

**Never write a model name into config.** `gemini-3-flash` was invented and
404'd; `gemini-2.5-flash-image` was taken from a doc and never checked. A name
that is correct today has a shelf life, and nothing in this container can
verify one -- egress is blocked. Ask the API: `build.image_model` defaults to
blank and discovers, and `outreach list-models` shows what a key really has.

**The dashboard page is a Python string full of JavaScript.** A JS escape has
to be doubled -- `\n` becomes a real newline and breaks the string it sits in.
The whole page is one `<script>`, so one bad line stops every panel with no
visible error and a server log that looks healthy. `test_the_page_javascript_actually_parses`
runs a real parser over it; keep it green.

**A green suite is not a working app.** 646 tests passed while the dashboard
was entirely dead. Tests asserted the buttons were in the HTML and the
endpoints answered -- all true, all useless when the script would not parse.
Ask what the test would have caught, not how many there are.

**Read the quota name, not the status code.** Gemini returns 429 for a
per-minute limit that clears by waiting and for a per-day one that does not.
Treating them alike either wastes an hour retrying a dead quota or throws
away a whole run's images because one minute was busy.

**The Sheet is positional.** Rows are read by column index, so schema columns
are **append-only**. Inserting a column in the middle silently shifts every
value in every existing row.

**Manual columns are manual.** `Approval Status` and anything else a human sets
is guarded by `guard_manual_column`. A stage may never write one. A dashboard
action on a named row the user just clicked may, with `user_edit=True`.

**Test fakes must enforce what the real client enforces.** A `FakeSheets` that
was more permissive than `SheetsClient` is how the manual-column bug shipped
past a green suite. When you add a rule to the real client, add it to the fakes.

**Never write to a git-tracked file at runtime.** The dashboard once wrote to
`config/campaigns/penang-my.yaml` and broke `git pull` on Lyon's machine. Config
edits fork to `*.local.yaml` via `fork_to_local`.

**Pin what you assert.** Tests that read the live campaign file inherit its
defaults; when the file changes, they quietly start asserting something else.
Helpers pin the fields their assertions depend on.

**Never inject a duplicate YAML key in a test.** Last-wins means the config you
loaded was not the config you thought you wrote. Replace the real line.

**Per-business facts are schema fields; craft rules are template text.** The
site prompt grew by asking the model to restate rules that never change. If
every business gets the same answer -- focus rings, token declaration, icon
format -- it belongs in `render_lovable_prompt` as fixed text, where it costs
nothing per lead and cannot be forgotten. Only what genuinely differs per
business earns a field in `SITE_SPEC_SCHEMA`.

**The required-field count is capped at 19 on purpose.** A 20th required field
costs every weaker non-Anthropic model the whole spec, not just that field --
Lyon runs Ollama. New spec fields go in `properties` but not `required`, with
the prompt block rendered conditionally, so a model that skips one produces
the previous prompt rather than a broken one. `layout_system` is the worked
example.

**A site can exist only at a URL.** Pages this pipeline generates sit on
disk, but one built in Google AI Studio or Lovable exists only at its
Preview URL -- so a screenshot that looked solely for
`sites/<slug>/index.html` silently photographed nothing for every
externally built site. `screenshot.capture` takes either, and the draft
falls back to the Preview URL.

**"Never fatal" means catching every exception, not the one you named.**
`capture_site_shot` caught only `ScreenshotError`, so a permissions error or
an OSError from the browser escaped into the per-lead guard and killed that
lead's draft -- when it happened for every lead the run produced no messages
at all. An optional step catches `Exception` and reports the reason in the
summary, because the only other trace was a log nobody reads.

**The screenshot is never worth the message.** Every way photographing the
site can fail -- no browser, no page, a hung browser, a browser that exits 0
having written nothing -- ends with the text still sending. It drives Edge or
Chrome headless rather than adding Playwright: a 150MB browser download is a
real cost to a non-programmer on Windows, and every Windows machine already
has Edge. A zero exit code is not proof; bytes on disk are.

**"Sent" must mean WhatsApp acknowledged it.** `sendMessage()` resolving only
means the socket accepted the message; a half-open connection queues it,
resolves, and transmits nothing -- so the dashboard reported success while
the sender's own phone showed no chat at all. The relay waits for a server
ack and reports `pending` otherwise, which the client raises, so the lead
stays in the queue instead of being marked done. It also asks `onWhatsApp`
before sending: WhatsApp accepts a message for any well-formed JID, so
sending blind to an unregistered number is indistinguishable from success.

**Normalise a number once, and only where its country is known.** The send
path normalised twice -- in the dashboard queue with the lead's country, then
again in `send_message` with the campaign's -- and the second pass glued that
code onto a number that already had one. A UK lead went out as
60447450225230: a JID Baileys accepts and reports as **sent**, delivered to
nobody. Malaysia survived by luck. Pass `country=""` when a number is already
E.164. Do not "detect" E.164 from the digits either -- a US national number
opens with 504, which is Honduras.

**A fix that needs a button pressed is not a fix for existing rows.** New
leads got Google's country, but every row already on the Sheet kept a
national number and a Country cell stamped from config, so UK leads carried
on dialling as Malaysian until someone remembered to press Refresh. The
address is the one field on those rows still telling the truth, so
`_country_of` reads the number's own prefix, then the address, and only then
the stored cell. Legacy rows heal themselves.

**Google knows each lead's country; ask it.** `addressComponents` carries the
ISO code per place. That is the first source, the phone's own prefix the
second, and `compliance.country_code` only the last resort -- it is
campaign-wide and was silently wrong the moment a Penang config was copied.

**Store the international phone number, never the national one.** Google
returns both. The national form ("07450 225230") names no country, so keeping
it meant rebuilding the country from `compliance.country_code` -- which on a
UK campaign copied from Penang still said MY, and dialled a Manchester mobile
as 607450225230. The international form carries its own country code, so
nothing is inferred and a campaign works abroad with no configuration. The
Country column is likewise read off the number, not stamped from config.

**`onWhatsApp` takes a phone number, not a JID.** It builds its query as
`"+" + what you pass`, so a JID asked about `+4479...@s.whatsapp.net` and
matched nothing -- every lead in every country came back "not on WhatsApp".
Malaysia hid it for weeks because the numbering plan answered there and the
lookup was never called. `tests/test_relay_check.py` reads server.js as text
and holds that line, plus the send path's old forced `60`. When the relay is
wrong, `outreach whatsapp-probe +44...` shows the digits asked and the raw
reply -- use it before theorising.

**The numbering plan never decides who is on WhatsApp.** It was wrong in both
directions: WhatsApp Business verifies landlines by voice call, so reading a
UK `020` number as unreachable wrote off every shop running WhatsApp on its
shop line -- and a mobile is no guarantee of an account either. `line_type`
is informational only. Every lead with a dialable number gets asked, and the
answer is cached; the plan's one remaining verdict is "there is no usable
number here". Adding a country now means adding a dialling code so the number
can be *dialled*, never a rule about who can be reached.

**Every phone rule is a country's rule, never "the" rule.** `is_mobile_number`
hardcoded Malaysia's 01X prefix, and the relay force-prefixed `60` onto any
number lacking one -- so a US lead was both hidden from the queue and, had it
sent, addressed to 6015045550123, a real Malaysian stranger. `phone.py` owns
this now: `line_type` returns **unknown** wherever a numbering plan genuinely
cannot tell (the US and Canada assign no mobile range), and unknown means
*ask WhatsApp*, never *give up*. Never infer a country code that was not given.
Normalising to E.164 belongs in `WhatsAppClient.send_message`, the last place
that knows the lead's country -- making the relay strict without doing that
refused every national-format Malaysian number on the Sheet.

**Buying a cheap answer and then ignoring it is worse than not asking.** The
check reported six leads reachable and Research went through all twenty
anyway, because nothing gated it. Research now skips a definite `no`, and
the dashboard's "leads to research" count applies the same rule -- a count
that disagrees with what the stage will do makes every run look like it
under-delivered. An *unchecked* lead is still researched: gating on `yes`
would make Research silently do nothing for anyone who never ran the check.

**The WhatsApp verdict lives on Leads, not Research.** Research decides what
is left to do by whether a Lead ID appears on the Research tab, so a verdict
written there for a lead that has not been researched -- which is the point
of checking early -- would retire that lead for ever. It is on Leads,
appended after the manual block: append-only is a data rule and manual-last
was only an efficiency note, so a refresh now costs two range writes.
`stored_verdict` still reads Research as a fallback so older answers are not
paid for twice.

**Reachability is answered before the site is built, not after.** Building is
the most expensive thing here, and it used to happen before anyone knew the
lead could be contacted. The check runs at the end of `research` automatically
-- it shipped once as a separate stage with no dashboard button, which meant
it never ran at all and every US lead stayed unchecked. A stage Lyon cannot
reach from `Dashboard.bat` does not exist. It sits between research and the
brief, costs a lookup only where the numbering plan cannot answer, and caches
the verdict in the Sheet. A lead needs a *confirmed* channel to be built for -- WhatsApp `yes`, or an
email. An unchecked lead **holds**: no evidence means wait, not spend. The
report separates held from rejected, because one needs the relay connecting
and the other is a dead end.

**Defensive `getattr` on config hides typos.** `_default_client` read a nested
`outreach.whatsapp` that does not exist -- the real fields are flat,
`whatsapp_enabled` -- so it returned None on a campaign with WhatsApp on, and
every lookup silently did nothing while five US leads were researched,
briefed and built. Read config attributes directly and let a wrong name
raise.

**Ordering is the user's, not ours.** Socials-first is the one reorder we do,
because Lyon asked for it. Within that, sheet order is preserved -- a queue he
can predict beats a marginally better-sorted one. Anything else goes behind
`research.order` as an opt-in.

**Secrets are write-only.** `env_edit.set_key` writes; `is_set` reports presence
and never the value. No key is ever echoed to a page, a log, or a report.

## Conventions

- Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`)
- No AI attribution trailers in commits
- Comments explain *why*, in prose, at the density of the surrounding file
- Config validates at load time and fails with a message naming the fix
- Every stage is resumable: bounded by `--limit`, checkpointed, and skips work
  already done

## The agent team

Five subagents live in `.claude/agents/`. Claude Code picks them up
automatically; ask for one by name to force it.

| Agent | When |
|---|---|
| `planner` | before a change big enough to get wrong -- returns a task plan, writes no code |
| `coder` | implement a planned change, reusing the existing stage/provider patterns |
| `code-reviewer` | after implementing, before pushing. Read-only, reviews a git range |
| `tester` | write or strengthen tests, and prove each one fails without the fix |
| `deployer` | run the suite, commit, push, and update the docs and handbook |

The normal loop for anything non-trivial is planner → coder → tester →
code-reviewer → deployer. For a one-file, sub-30-line change, skip to coder.

Two skills in `.claude/skills/` are adapted from
[superpowers](https://github.com/obra/superpowers) (MIT):
`verification-before-completion` (never claim green without reading the number)
and `systematic-debugging` (root cause before fix #4).

## Not verified from this container

Say so rather than implying otherwise:

- The Windows launchers (`Dashboard.bat`, `run.bat`, `Schedule-Daily.ps1`) have
  never been executed on Windows here.
- The Ollama / OpenRouter transports have never reached a real endpoint --
  outbound egress is blocked. `modelcheck` is how Lyon verifies them himself.
