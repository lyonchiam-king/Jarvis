# Jarvis -- SME outreach pipeline

Finds small businesses with no website, researches them, writes a website brief,
builds a demo site, and drafts the outreach message. The demo site **is** the
pitch: nothing is sent to a business until their site exists.

**Owner:** Lyon Chiam (COAI, Penang). Works on **Windows/PowerShell**, is not a
programmer, and drives the tool through `Dashboard.bat` -- not the CLI. Anything
that only works on Linux, or only works from a terminal, is not finished.

**Stack:** Python 3.11+, few dependencies. Google Sheets is the database.

The dashboard is a single Flask file serving **two** front ends. `/` is the
original page: one Python string of hand-written HTML/JS, no build step, and
still the one Lyon uses. `/app` is a React page built from `frontend/` --
TypeScript, Tailwind, Vite -- whose output is committed to
`src/outreach/web/` so that a Windows checkout with Python alone can serve
it. **Lyon never runs npm.** The build runs here; if `frontend/` changes and
`src/outreach/web/` does not, the page he loads is the old one.

## Commands

```bash
.venv/bin/pytest -q          # the whole suite -- must be green before any push
.venv/bin/pytest tests/test_research.py -q
python -m outreach run discovery --limit 10
python -m outreach dashboard

cd frontend && npm install && npm run build   # only after editing frontend/
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
  stages/          discovery, research, reachability, site_brief,
                   outreach_draft, publish
  providers/       places, sheets auth, llm, social, web, lovable, sitegen,
                   whatsapp, cloudflare
  images.py        generated page imagery -- mood and texture, never the premises
  sitecheck.py     refuses a generated page that would embarrass you
  screenshot.py    photographs the built page for the WhatsApp message
  slug.py          deterministic, unguessable per-business URL paths
  web/             the built React page -- generated, committed, never edited
config/campaigns/  penang-my.yaml is the live campaign
frontend/          React source for /app; `npm run build` writes src/outreach/web
tests/             1215 tests, pytest, no CI -- the suite is the only gate
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

**A dead `CAMPAIGN_CONFIG` must not take the tool down.** Saving a setting
writes that key into `.env` aimed at a `*.local.yaml` twin. When the twin is
missing, every command loads config first, so every command died -- drafting
included -- with an error naming config and nothing about the dashboard.
Load now falls back to the tracked campaign and warns that saved settings
are not being applied. An explicit `--config` is never second-guessed.

**Tests must not damage the machine they run on.** A run left
`CAMPAIGN_CONFIG` in the real `.env` pointing at a pytest temp directory,
which broke every command long after the run finished. `tests/conftest.py`
fingerprints `.env` and the `.local.yaml` and fails the test that changes
one, and restores a leaked `CAMPAIGN_CONFIG` env var between tests.

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

**A Pages deployment is a snapshot, not an addition.** Uploading one
business's folder would publish a site containing only that business, and
every page published before it would vanish from the main address. The whole
sites repo goes up in one deployment and each business keeps a stable path,
`<base>/<slug>/`. Workers is the wrong tool here for the same reason it is
the wrong shape: these are static pages with no logic to run, and routing
them by hand would be work Pages does for nothing.

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

**A browser resolves `--screenshot` against its own working directory.** A
relative path wrote nothing and reported only "the system cannot find the
path specified", from one of three modes -- the other two showed unrelated
Chrome sync noise. Always pass an absolute path, and create the directory
first: the browser will not.

**Which headless mode honours `--screenshot` depends on the build.**
`--headless=new` exited 0 on Lyon's Edge and wrote nothing. The modes are
tried in turn -- new, old, bare -- and a build that ignores the path and
drops the file in the working directory has it claimed. A timeout is not
retried: that is the page hanging, which no flag fixes, and three attempts
would triple the budget per lead. `outreach shot-check` answers "can this
machine screenshot at all" in one command. Chrome's stderr noise
("Every renderer should have at least one task...") is filtered out of the
error, having once been reported as though it were the cause.

**The screenshot is never worth the message.** Every way photographing the
site can fail -- no browser, no page, a hung browser, a browser that exits 0
having written nothing -- ends with the text still sending. It drives Edge or
Chrome headless rather than adding Playwright: a 150MB browser download is a
real cost to a non-programmer on Windows, and every Windows machine already
has Edge. A zero exit code is not proof; bytes on disk are.

**A relay body limit is a feature's real constraint.** `express.json()`
defaults to 100kb. A text message never approached it and a 900x1200 PNG
always exceeds it -- base64 adds a third on top -- so Express answered 413
before the send route ran, and sending broke the day messages started
carrying a picture. The limit is explicit now, and an oversized body comes
back as JSON naming the screenshot rather than Express's HTML error page,
which the client could only report as a transport failure.

**An acknowledgement is evidence, not a gate.** `sendMessage()` resolving
only means the socket accepted the message, so the relay waits for a
delivery ack -- but *requiring* one stopped the queue dead, because WhatsApp
confirms late often enough (a first message to a new contact especially)
that there is frequently nothing to hear inside the window. Silence is
missing evidence, not evidence of nothing: the send stands, the status is
`unconfirmed`, and the page says so. The `onWhatsApp` pre-send check is the
same shape -- it reports, it does not refuse, since the queue only offers
leads already cached as `yes` and a live `no` contradicting that is more
likely a hiccup than the truth.

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

**The first thing a new user does must not be a manual step.** Setup asked
someone who is not a programmer to create a spreadsheet by hand, find its ID
inside a web address, and paste it into a file -- three chances to fail before
anything worked, and the one step nobody could be talked through over a
message. Signing in already proves whose Google account it is, so
`/api/create-sheet` makes the Sheet in *their* Drive, adds every tab, and
writes `SHEET_ID` itself. It refuses once a Sheet exists: a second, empty one
would make every lead already gathered look lost.

**Consent in a request thread must be bounded.** `run_local_server` opens a
browser and blocks until somebody finishes -- forever, if they close the tab.
Everywhere the dashboard reuses an existing token it passes
`allow_consent=False`, but the first-run button cannot: there is no token yet.
It passes `consent_timeout` instead, so an abandoned sign-in becomes an error
on the page and a button they can press again, rather than a panel that
quietly stops answering.

**A message nobody can decrypt is a missing `getMessage`, not a bad send.**
Four messages arrived as "Waiting for this message. This may take a while."
-- in Lyon's own chat list, which is where it shows first. Nothing about the
send looked wrong, because encryption is not what failed: WhatsApp is
end-to-end per device, and a device that cannot decrypt asks for the message
*again* with a retry receipt. Baileys answers by calling `getMessage(key)`
and re-encrypting. The relay passed no `getMessage`, so every retry went
unanswered and the message stuck for ever -- on the recipient's phone as much
as ours. `sentMessages` keeps the content of recent sends, bounded, purely so
that retry can be served.

**`sock.logout()` does not delete the credentials.** It ends the session at
WhatsApp's end and leaves `.whatsapp-session/` untouched, so the next
`useMultiFileAuthState` reads the dead session straight back, WhatsApp answers
401, and the relay reconnect-loops for ever without emitting a QR -- which
looks like a hung relay, not a logout. `clearSession()` deletes the folder, and
a `resetting` flag stops the close handler reconnecting underneath the reset
and rebuilding what is being deleted. Being unlinked *from the phone* takes the
same path, for the same reason. A failed delete is reported, never reported as
success: the relay would silently come back on the old account.

**A socket does not close when you tell it to.** `logout()` returns before
the WebSocket has finished closing, so the old socket's `connection.update`
arrives *after* the replacement is already connecting. It reached the live
handler, was read as a fresh "logged out", and deleted the credential folder
the new socket had just written -- whose next `saveCreds` then threw ENOENT
out of an event handler and ended the process. Pressing Log out gave exactly
that: logged out, no QR, then "relay not working". A boolean flag cannot fix
this; the ordering is not knowable. Every socket captures a `generation` and
every handler it registers is inert once that number moves on, so a
superseded socket cannot act at all.

**The relay must degrade, never disappear.** Baileys emits from timers and
socket callbacks, where an unhandled throw ends the process -- and the window
is started with `cmd /c`, so it closes and takes the stack trace with it. A
crashed relay and one never started looked identical, and the only thing the
page could say was "relay not running". `uncaughtException` and
`unhandledRejection` now log and keep serving, `saveCreds` cannot throw out of
its handler, output goes to `relay-log.txt`, and `/status` carries
`last_error` so the page shows a reason instead of a symptom.

**"Sent" is a claim the Sheet makes, not a fact.** WhatsApp acknowledges a
message it later fails to deliver -- the "Waiting for this message" ones were
acknowledged and unreadable. Correcting that meant opening the Sheet and
clearing a cell by hand, which is the thing the dashboard exists to avoid, so
**Send again** on an excluded lead clears `WhatsApp Status` and `WhatsApp Sent
At` and puts it back in the queue. It writes nothing else: the message and its
screenshot cost money to produce, and Send afterwards must send the same one.
It is offered only for `already_sent` and `status_not_drafted`, the two
exclusions a cleared cell actually fixes -- no phone, no Leads row and
drafted-as-email each need a different repair, and a button promising to undo
them would not work.

**A dead end should be removable where it is read.** A lead the check answered
"not on WhatsApp" is a settled question, so that list carries a per-row
**Remove** that disqualifies it and stops it being researched or built.
Confined to that list on purpose: an *unchecked* lead is not a dead end, it is
one the relay has not answered for yet, and removing those would throw away
work the check has not done. A rejected lead holding an email is still
buildable and still counted in "can be followed up", so removing one says so
before it acts.

**Art direction is a craft rule, so it is a campaign setting, not a schema
field.** Every business gets the same answer about type scale, container
widths, colour ratio and easing curves, so asking the model to restate them
per lead spends tokens on a chance to forget one -- and the required-field
count is capped at 19 anyway. `build.design_level` switches the craft blocks
in `render_lovable_prompt` instead: `showcase` (the default) adds them,
`standard` is the prompt as it was. It changes only the text handed to the
site builder, never the spec, so no research model sees it and Ollama is not
penalised for it. All four `render_lovable_prompt` call sites pass it -- run,
`--rewrite`, `generate_sites` and `regenerate_one` -- and each has its own
test, because three of them were unwired and every other test still passed:
the prompt was valid, just not the one the campaign asked for.

**`unconfirmed` means the message left.** It was missing from
`WA_SENT_STATUSES`, so a send WhatsApp had not acknowledged inside the relay's
window stayed at the top of the send queue with nothing to show it had gone --
inviting the same real business to be messaged twice. The ack was softened
from a gate into evidence for good reasons; the queue never learned. `pending`
is the same state under the relay's older name and counts too.

**Transient page state must not live on objects the page replaces.**
`loadWhatsAppInline` rebuilds `waQueue` from the server, on a 30-second timer,
so a `_confirming` flag set on a queue object was thrown away by the next
refresh: the first press armed the confirmation, a tick discarded it, and the
second press only armed it again -- "I clicked twice and nothing went out".
The same discard is why Sending and Sent never appeared. It lives in `waUi`,
keyed by Lead ID, and the timer stands aside while a card is mid-decision so
an edited message is not lost to a tick either. Address a lead by its ID, never
by its index: the queue is rebuilt underneath the buttons.

**A duplicate function name on the page fails silently.** A `dropLead` added
for the WhatsApp-check panel replaced the one the lead preview calls, with a
different signature -- so that button began passing an index where a lead ID
belonged, and nothing failed because the page still parsed.
`test_no_two_page_functions_share_a_name` holds it.

**Parsing the page is not running it.** Both send bugs above were invisible to
`node --check`: the script was valid throughout. The page's script is now
executed under node against a stub DOM, driving the send state machine
directly -- which is what caught the duplicate `dropLead` in the same run.

**Swallowing a rejection is only half of "never disappear".** The
`unhandledRejection` handler stopped the relay dying -- and turned a passing
network blip into permanent death. `connect()` awaits
`fetchLatestBaileysVersion()`, a network call, and every caller dropped the
promise: `setTimeout(connect, 3000)` keeps no reference to it. Before the
handler existed that rejection killed the process, which at least made the
failure obvious and got it restarted; after, the relay stayed up, never
reconnected, and answered every send with "not connected" until someone
restarted it by hand. Degrading has to mean *recovering*: `connectSafely()`
catches and `scheduleReconnect` backs off to a cap, resetting the delay on
open, with one timer pending at a time. `app.listen` comes first and the
connection second -- a failed first attempt used to leave no HTTP server at
all, so the page could not even ask what was wrong.

**"Reconnecting" and "not connected" are different sentences.** One needs the
user to do nothing and the other needs a restart, and describing both the same
way made a blip look like a broken install.

**Not this one.** A drafted message you do not want to send now has a button,
which sets the Outreach Log's manual `Status` to `Skipped` -- with
`user_edit=True`, the exception the manual-column rule names. Nothing else is
written: the message and its screenshot cost money to make, so skipping keeps
them and **Send again** is one press. That reversibility is why it does not
stop to confirm.

**One relay per WhatsApp session, enforced by the port.** WhatsApp allows one
connection per linked device, so a second relay on the same
`.whatsapp-session/` does not sit harmlessly beside the first -- they take the
connection from each other, and the symptom is a relay that connects, works
for a moment and drops, over and over. Two things made that easy to reach:
`Dashboard.bat` only killed the relay when the dashboard exited *normally*, so
closing the window with the X left one behind and every later run added
another; and the `uncaughtException` handler kept the loser of the race alive,
because an EADDRINUSE thrown from `listen` is exactly the sort of error it was
written to swallow. The bind is the lock now -- `server.on("error")` exits on
EADDRINUSE and says which relay this is -- and the launcher kills the old tree
before starting, with `/t` so the node process goes with its cmd wrapper.
This is the one place where disappearing is right: a second relay is not a
working relay, it is the thing breaking the first.

**"Connecting" is a third state.** Between the retry timer firing and the
socket opening, `connected`, `reconnecting` and `resetting` were all false with
no error -- which the page could only render as "Relay not running", so every
normal reconnect looked like a dead relay for a few seconds.

**A drop is only diagnosable from the pattern.** How long the connection held,
what code closed it, how often. `stamp()` timestamps every transition into
`relay-log.txt`, and a connection that lasted under thirty seconds says so,
because that is the signature of something else holding the device slot.

**The service list is written once, not asked of the model.** Every message
now ends with the other things on offer -- Google Business Profile work,
funnels, social ads. That is the same three lines for every business, so it
is `outreach.services` in the campaign file and `render_services_block`
appends it verbatim, exactly as the compliance footer is. A model told to
"mention the other services" rewords one, drops one, or invents a fourth, and
an invented service is something nobody sells. The system prompt is told not
to list them for the same reason, and the MODEL beat instead ends on one
clause saying there are other ways to help, so the list does not arrive out
of nowhere. Empty by default in code: a campaign naming none gets the message
byte for byte as it was. Four call sites carry it -- run and regenerate, each
for WhatsApp and email -- and each has its own test, `regenerate_one`'s email
asserted through the call because it stores only a truncated preview.

**A fork inherits every setting it has never heard of.** `fork_to_local`
copies the tracked campaign once and never again, so a `*.local.yaml` is a
snapshot of whatever shipped the day the user first saved a setting. Every
key added to the template afterwards was invisible to them: their file has no
such key, so they get the code default. That is fine where the default is the
intended value and silently wrong where it is not -- `outreach.services`
defaults to empty on purpose, so the pull that added three services to the
template changed nothing at all for the person who asked for them, and the
feature looked simply broken. `_inherit_from_template` merges the template
under the fork, one level at a time. What the fork says always wins,
**including a key it sets empty**, so clearing the services list on the page
writes `services: []` rather than deleting the key -- omitting it would make
"clear" mean "restore whatever shipped". Lists replace rather than
concatenate: someone who trimmed their areas to two meant two.

**Every key in `research` equals its code default.** So a test asserting an
inherited research value passes whatever the merge does -- it cannot tell
inheritance from fallback. Pin merge assertions to `outreach`, where the
template genuinely differs from the dataclass.

**The specific detail is evidence, not a compliment.** A hook told only to
prove it looked wrote a nice observation about the business and never reached
the reason anyone would reply. It now has to land on the gap -- the people who
found them rate them highly, the people who have not cannot find them at all
-- so the next sentence is about customers they are missing rather than about
the sender. The solution beat forbids "built a website" outright: that is a
sentence about the sender, and the subject has to be their customers.

**The localhost binding was the only protection there was.** The dashboard
had no login of any kind -- it was safe purely because nothing else could
reach it. So putting it on a phone is not a networking change, it is an
authentication change, and the ordering is not negotiable:
`DASHBOARD_PASSWORD` guards the page whenever it is set, and `serve` **refuses**
to bind anywhere but `127.0.0.1` without one rather than warning about it. On a
cafe or hotel network everyone in the building is on that network, and the page
spends money, messages real businesses and reads the Sheet. The old safety test
parsed the source for a literal `"127.0.0.1"`; it now calls `serve` and asserts
the behaviour, because the host stopped being a constant the moment a phone had
to reach it.

**Below 16px, Safari zooms and does not zoom back.** Tapping the message box to
fix a typo left the whole page magnified with no obvious way out. Inputs go to
16px on small screens for that reason alone, and the login page -- the first
thing a phone sees -- was built at 16px from the start.

**Batch asks for a feature; Python decides whether it is allowed.** Phone
access was decided in `Dashboard.bat` by a `findstr` regex against `.env` --
untestable from a machine without Windows, and when it was wrong it did
nothing at all: no flag passed, no phone access, and no line of output saying
why. `--phone` is now a request rather than an address, and `serve` picks the
binding from a password it has already read. The launcher has no logic left.
Anything that must be right on Windows and cannot be run here belongs on the
Python side of that line.

**A refusal has to be audible.** The same failure was silent twice over: the
launcher's check failed quietly, and the dashboard then started normally
because nothing had asked it for anything. It now always says which state it
is in -- the address to type, or the reason phone access is off and the line
that fixes it. `lan_address()` prints the address itself so nobody reads the
wrong line out of `ipconfig`, and the firewall prompt is named, being the
most common reason a correct setup still fails.

**A batch `if` block expands `%VAR%` when it is parsed.** The setup routine at
the top of `Dashboard.bat` is a subroutine to avoid exactly this, and the phone
check reintroduced it: a nested `if "%ERRORLEVEL%"=="0"` reads the value from
*before* the block. Chain with `&&` instead. The test strips `REM` lines before
asserting, because the comment explaining the trap names the very thing the
assertion forbids and otherwise matches the prose.

**Secrets are write-only.** `env_edit.set_key` writes; `is_set` reports presence
and never the value. No key is ever echoed to a page, a log, or a report.

**A typecheck with no types is a green light wired to nothing.** The React
front end arrived from Google AI Studio with `@types/react` missing and
`strict` off, so every component was `any` -- `tsc --noEmit` exited 0 over a
view reading three fields that did not exist on its own type, and over a
`saveModel({model_key})` the server has never accepted. It was cited as proof
the wiring was right. The types are installed and `strict` is on; `npm run
build` runs `tsc` first so a build cannot ship what the checker rejects. This
is the same failure as the 646 green tests beside a dead dashboard: ask what
the check would have caught.

**TypeScript cannot see Python.** It proves a view calls a method the client
has; nothing proves that method names a URL Flask answers. A path typed one
character wrong compiles, builds, ships, and 404s to a page that shows an
empty queue and calls it "nothing to do".
`test_every_endpoint_the_page_calls_is_a_route_the_app_serves` reads the
endpoint strings out of `client.ts` and checks each against the real URL map,
and `test_the_built_page_asks_for_assets_the_app_can_serve` follows the asset
URLs in the built HTML -- Vite's default base is `/`, which the dashboard does
not serve, and the symptom is a blank white page with nothing in the Python
log.

**The new page translates; it does not invent.** `client.ts` is the only place
that knows Flask answers `name` where a view wants `business_name`, or
`dialled` where it wants `e164_phone`. Where Jarvis genuinely does not know
something the field is left empty. The generated page shipped a hardcoded
`+1 (504) 555-0199` as the "connected" WhatsApp number, a claim that keys were
"encrypted on the backend" when they are plain text in `.env`, and a promise
that a failed send would be "copied to clipboard" -- three sentences that read
as facts and were not. A dashboard that spends money and messages real
businesses is the worst place for a convincing guess.

**A generated page states things it cannot support.** The React front end
arrived with a made-up phone number shown as the linked WhatsApp account,
per-lead prices nobody measured ("about 0.2¢ each"), a model name
(`gemini-3.8-flash`) written into two placeholders and two defaults, a
version string matching nothing, a hardcoded `127.0.0.1:8787` for a port the
campaign file owns, a promise that failed sends are "copied to clipboard",
and a **Download Local Relay app** link pointing at an unrelated repository
-- there is no download; `Dashboard.bat` starts it. Each reads as fact.
`scripts/verify/no-invented-facts.mjs` bans the shapes: model names, prices,
phone numbers, version strings, download links, the word *encrypt*, and a
literal loopback address. It carries a control that fires every pattern
against text built to trip it, because a negative check with a broken regex
reports a clean page for ever.

**Rendering is the only proof a page works.** `tsc` proves types, Vite proves
it bundles, pytest proves the routes answer -- and all three pass over a
throw in the first render, where the server logs two clean 200s and the
person sees white. `scripts/verify/render-check.mjs` drives real Chromium
over the DevTools protocol: what React committed, every console error and
failed request, whether anything sits past the right edge at phone width,
and a screenshot. It found the false claims still shipping in the bundle
after they were fixed in one file and missed in another.

**Set the viewport over the protocol, not with `--window-size`.** The flag is
ignored in the new headless mode -- it reported 500px whatever was asked for,
so every phone measurement was taken at the wrong width and the layout looked
fine. `Emulation.setDeviceMetricsOverride` is exact, and the check now asserts
the width it got before trusting anything measured at it.

**Measure overflow from where things land, not from a container's
`overflow`.** `<main>` carries `overflow-y-auto`, which computes `overflow-x`
to `auto` as well, so a `scrollWidth > clientWidth` test skipped the very
element doing the clipping. What a person sees is a button past the right
edge, so that is what is measured -- rightward only, since the closed
navigation drawer is parked off the left edge on purpose and a symmetric
test reports six false failures.

**`/app` is additive, and `/` does not move.** The React page is new, has never
run on Lyon's machine, and replacing the working dashboard with it would make
a bad first render into a lost morning. Both pages get their own
`start_url` manifest so each has its own home-screen icon; the old one stays
the default until he says otherwise.

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

A third, `unlazy` ([source](https://github.com/Leonxlnx/unlazy), MIT), is
vendored whole at a pinned commit -- see `.claude/skills/unlazy/VENDORED.md`.
It is the runnable version of the first: acceptance gates written before the
work, each proved by a command whose output is checked, and a Stop hook that
refuses a "done" claim the evidence does not support. Invoke it with
`/unlazy` for work big enough to report half-finished.

**Its safety boundary is review, not sandboxing.** `CHECK:` lines are shell
code run with your permissions and environment -- the project says so itself,
plainly. Read every command before approving one, and never approve a gate
file that arrived from somewhere else without reading it. Lyon cannot audit a
shell line, so gates written for this repo are mine to justify, and
`--status` (which never executes) is the mode to reach for first.

The Stop hook is **not** installed. It is opt-in, per project or global, via
`scripts/install-hooks.mjs`, and it changes how every session on that machine
ends -- so it goes on deliberately, not as a side effect of a `git pull`.

## Not verified from this container

Say so rather than implying otherwise:

- The Windows launchers (`Dashboard.bat`, `run.bat`, `Schedule-Daily.ps1`) have
  never been executed on Windows here.
- The React page at `/app` renders under headless Chromium here
  (`scripts/verify/render-check.mjs`), which covers the first paint, console
  errors, failed requests and the phone-width layout. It has never run in
  real Safari, no check drives a click through to a result, and every count
  read zero because this container has no Sheet credentials.
- The Ollama / OpenRouter transports have never reached a real endpoint --
  outbound egress is blocked. `modelcheck` is how Lyon verifies them himself.
