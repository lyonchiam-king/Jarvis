# Running this every day

**Double-click `Dashboard.bat`.** Your browser opens on one page laid out in
the order you actually work:

| | | |
|---|---|---|
| **1** | **Gather** | One button. Finds a fresh batch, researches them, writes their briefs |
| **2** | **Review** | One business at a time. Approve or Skip |
| **3** | **Build** | The approved ones, ready to paste into your site builder |
| **4** | **Send** | Drafts waiting in Gmail, WhatsApp messages to copy |

At the top, **What you're hunting** (what you're targeting right now, and a
**Change** button) and **What needs you today** — the same four things as
counts, so you know before scrolling whether this is a five-minute morning or
an hour.

---

## Getting it onto your computer

You already have this folder and a working `.env`. To pick up the newest
version, open **PowerShell** in the project folder and run these four lines
once:

```powershell
git pull
.\.venv\Scripts\Activate.ps1
pip install -e .
python -m outreach preflight
```

> **If `git pull` says "your local changes would be overwritten":** you had
> changed settings in the dashboard, which used to write into a file git also
> tracks. Run `git stash`, then `git pull`, then `git stash pop` to keep your
> settings. From this version on it cannot happen again — the dashboard writes
> to `penang-my.local.yaml`, which git ignores, and leaves the tracked file as
> the starting template.

`preflight` is the one that matters: it checks Google, Sheets, Gmail and
Anthropic and prints exactly what's wrong if anything is. Get it to
**Preflight passed** and you're done — nothing else to install.

> **Starting on a brand-new machine?** The full first-time setup — Python, the
> Google Cloud project, OAuth, the Sheet — is in `README.md` under
> *First-time setup*. Come back here once `preflight` passes.

### Put it on your desktop, once

Right-click `Dashboard.bat` → **Send to → Desktop (create shortcut)**. Rename
the shortcut to *SME Outreach*. From then on it's a desktop icon like any other
app: double-click, your browser opens, and you never type a command again.

Double-clicking it starts a small web server on your own machine and opens the
page. **Leave the black window open while you work** — closing it stops the
dashboard. Closing the browser tab is fine; reopen it at
`http://127.0.0.1:8765`.

> **Prefer a plain menu?** `run.bat` gives you a numbered list in a terminal
> window instead. Same actions, no browser.
>
> **On a Mac:** rename `run.sh` to `run.command` and run `chmod +x run.command`
> once. Or `outreach dashboard` for the browser version.

**It is yours alone.** The dashboard binds to `127.0.0.1`, which means it is
reachable only from your own machine — not from your network, not from the
internet, even on café wifi. It holds no passwords of its own; it reuses the
same `.env` and Google login the commands use. A test in the suite fails the
build if anyone ever changes that binding.

---

## Giving it to someone else

Everything in this tool is per-person. Someone you hand it to runs their own
copy end to end: their Google account, their Sheet, their keys, their
WhatsApp. Nothing is shared, and nothing of theirs comes back to you.

Send them the folder (or the repository) and `SETUP.md`. **Never send your
`.env`, your `credentials/` folder, or your `.whatsapp-session/` folder** —
those are your keys, your Google account and your WhatsApp, and anyone
holding them can spend your money and message as you.

On their first run the dashboard opens onto a short setup list instead of the
usual numbers:

1. **Their own Google Sheet.** They press **Create my Google Sheet**, sign in
   with their own Google account, and the spreadsheet is created in *their*
   Drive with all five tabs already set up. The ID is saved for them — no
   spreadsheet to make by hand, no ID to copy out of a web address. If they
   close the Google window without finishing, nothing is created and they can
   just press it again.
2. **Their own keys.** Pasted into the Settings and Model panels on the page.
3. **Their own WhatsApp.** Section 4 shows a QR code; they scan it from
   WhatsApp → Linked devices, on their own phone, once.

The button refuses to run a second time once a Sheet is set up — a second,
empty Sheet would make every lead already gathered look lost.

---

## Your first test run

About twenty minutes, and it costs roughly a dollar. Do it once before you
trust it with a real batch.

### 0. One-time, after this update

The Research tab gained a **Social Profiles** column. Add it to your existing
Sheet by opening **Run one step on its own** → **Check setup**, then running
this once in PowerShell:

```powershell
.\.venv\Scripts\outreach.exe init-sheet
```

It only touches row 1 — every lead, brief and draft you already have is left
exactly as it is. Leads researched before today will have that column blank;
they are not re-researched automatically, which is deliberate — re-running
research on them would cost money for a field you can also just leave empty.

### 1. Check the wiring — free

Double-click your new desktop icon. In step 1, open **Run one step on its own**
and press **Check setup**.

Read the output. It tests Google Places, Sheets, Gmail and Anthropic one at a
time and names anything that's wrong. **Don't go further until this passes** —
every later step depends on it, and a failure here is much easier to read than
the same failure buried in a batch of 60.

### 2. Look before you spend — free

Press **Preview leads**. It searches, applies your filters, and prints the
businesses it *would* save. Nothing is written and nothing is charged beyond a
few cents of search.

Look at the names. Are these real businesses you'd want as customers? If
they're the wrong sort entirely, fix the category in **What you're hunting**
before spending anything.

### 3. A small real batch — about 60 cents

Type **10** in the **How many** box, then press **Find leads**, **Research**,
and **Write briefs** in turn. Ten is plenty for a first look, and with the
number set you cannot accidentally spend on sixty.

Now open your Sheet (link at the top) and read the **Research** tab. This is
the honest test: is *Suggested Website Angle* specific and true, or could it
have been written about any salon in Penang? Everything downstream is built on
this, so if it reads thin, say so and we'll tune it before you scale up.

### 4. Review one, build one — the real cost starts here

In step 2, read one brief properly. **Approve** just that one, **Decide later**
on the rest.

In step 3, **Copy prompt** → **Open your site builder** (Google AI Studio by
default) → paste → let it build. Then paste the URL back and press **Mark as
built**.

**Open the finished site on your phone.** This is the moment of truth — it's
what a business owner will see. Is the name right? Does the phone number work?
Would you be happy sending this to a stranger?

### 5. See the message — free

Press **Draft messages**, then open Gmail → Drafts. Read it as if you were the
owner: does the first line prove someone looked at their business, and does the
demo link open?

**Don't send it yet.** Sit on it for an hour, read it again, then decide.

### What to come back with

- The angle from the Research tab — sharp, or generic?
- The demo site — right, or does it need different instructions?
- The email — would you actually send it as written?

Those three answers are what tell us what to fix next.

---

## The demo is the pitch

**Every lead gets a site built before you write to them.** Not a description of
a site. The actual thing, with their name on it, at a URL they can tap.

These owners have never had a website. Telling one that "a booking page would
help you" asks them to picture something they have no reference for — and it
sounds like every other script in their inbox. Sending them *their own salon,
already built*, doesn't ask them to imagine anything. That's a different
conversation, and it's the only version of this that works now.

So the pipeline is wired that way and won't let you skip it.
`outreach.require_site` is `true` in `config\campaigns\penang-my.yaml`: no
message is drafted for a lead until that lead's site exists. The email is never
sent without the thing that makes it worth reading.

**The cost is real, so know the number.** The build is your most expensive
step. That's exactly what step 2 is for — you see each business and decide,
and the ones you Skip cost nothing and are never contacted. The filter is *you
choosing which businesses are worth it*, which is a better filter than sending
everyone a thinner message.

**Nothing builds behind your back.** `Approved` means "queued", not "built".
Building only ever happens when you press **Build approved** or tell Claude
Code to do it, and it asks before spending.

---

## Zero-touch mornings

Right-click **`Schedule-Daily.ps1`** → *Run with PowerShell*, once.

From then on it runs at 8am every day: finds leads, researches them, writes
their design briefs, and drafts messages for every lead **whose demo you built
yesterday**. Capped at 20 leads a day — roughly USD 2 — so it can't run away
with your money overnight.

That cap is deliberately lower than the 60 you get pressing the button
yourself. Overnight runs are unattended; 20 is a size you'll actually get
through in a morning. Raise it with `-LeadsPerDay` once you know your pace.

That one-day lag is the whole design. The overnight run does everything that is
cheap and safe; the two steps that cost real money or touch a real person —
building the sites and sending the messages — wait for you. So a fresh lead
found this morning gets its demo built by you today and its message drafted by
the run tomorrow. Nothing stalls; it just runs a day behind, permanently.

It never sends and never builds anything. Logs land in `logs\`.

To change the cap or the time:

```powershell
.\Schedule-Daily.ps1 -LeadsPerDay 40 -Time "07:30"
```

To stop it:

```powershell
Unregister-ScheduledTask -TaskName "SME Outreach - morning run" -Confirm:$false
```

---

## The honest shape of a day

The tool does about five minutes of work. The other thirty are yours, and
that's the part that makes money. Don't let the automation fool you into
thinking the job is done when the Sheet fills up.

| | What | Who |
|---|---|---|
| **5 min** | Find, research, write briefs, draft messages | the tool |
| **10 min** | Review the briefs — Approve or Skip | **you** |
| **20 min** | Build today's demo sites | **you** (or Claude Code) |
| **20 min** | Read yesterday's drafts, edit, send | **you** |
| **10 min** | Reply to anyone who wrote back | **you** |

---

## Your morning, step by step

### 1. Press **Run this morning's batch**

That finds new businesses, researches them, writes each one's design brief, and
drafts messages for the leads whose demos are already built. Takes a few
minutes — output streams down the page, and you can walk away.

**It never gives you the same business twice.** Every run skips everything
already on your Sheet, so today's 60 are 60 you have never seen. You cannot
double-charge yourself or double-message anyone by running it again.

#### Businesses with a Facebook or Instagram page go first

The checkbox under the button, on by default. Those leads have more to work
with — a bio, photos, posts in their own voice — so the research is richer and
the demo comes out better. And they have already decided they want to be found
online, which is most of the argument made for you.

Leads with no web presence at all aren't dropped, just queued behind. So with
**How many** set to 20, your money goes on the best 20 available rather than
whichever happen to sit highest in the Sheet.

**That's the only reordering.** Beyond the socials-first split, the queue walks
your Sheet top to bottom — which is Google's own relevance order, and the order
you can actually see. So the next-up list should never look random.

> If you'd rather work the best-reviewed first — richer briefs, since reviews
> are what they're built from — set `order: reviews` under `research:` in your
> campaign file. The trade is predictability: the queue stops following the
> Sheet.

It applies to **researching, briefing and drafting alike** — so a lead can't
jump the queue at a later step just because it was researched earlier.

Untick it to work in plain Sheet order.

**You never need to delete anything.** The ranking looks at your *whole* Sheet
every time, old leads and new together. Add 10 fresh leads of which 5 have a
Facebook page, press **Write briefs**, and those 5 go first — the older
website-less ones simply wait their turn. There is nothing to clear and nothing
to select.

Under the step buttons you'll see **what the next run would take**, by name,
with a ★ against any lead that has a social page. That's the ranking made
visible: check it before you press anything.

#### Doing fewer than a full batch

Next to the button is a **How many** box. Leave it blank for the full batch, or
type a number to do fewer — and it applies to **whichever button you press**,
including the single steps under *Run one step on its own*.

| You want | Do this |
|---|---|
| The usual 60 of everything | Leave it blank, press **Run this morning's batch** |
| Research only 5, to check quality | Type `5`, press **Research** |
| 20 new leads today, nothing else | Type `20`, press **Find leads** |
| Briefs for just 10 | Type `10`, press **Write briefs** |

This is the cheapest habit in the whole tool. Before any expensive step, run it
at `5` first, read the output, *then* run the rest.

The number can go below your batch cap but never above it — so a slipped digit
can't turn `6` into `60`. To change the cap itself, press **Change** in
*What you're hunting* and set **Most a single run may do**.

> **Expect `0 need messages drafted` on your first run.** Nothing has a demo
> yet, so there is nothing to write about. That corrects itself from tomorrow.

#### Rewriting a brief you already have

**Write briefs** only ever writes a *new* prompt — a lead that already has a
row in the **Site Briefs** tab is skipped, forever, so re-running the button
never costs you twice. That's normally exactly right, but it means an
improvement to how the prompt itself is written (richer imagery instructions,
better motion, whatever) never reaches a brief that already exists. That's
what **Rewrite existing briefs**, under *Run one step on its own*, is for.

**Same cost as Write briefs — about 2 cents and one model call per lead —
and the result is a different prompt, not an edited one.** The model is not
deterministic: it does not touch up the old copy, it writes a fresh brief
from scratch against the current template. If you already read and
approved a brief, rewriting it makes that approval stale — **go back and
read it again** before you build from it.

It skips anything already built (a **Lovable Project ID** on the row), so it
can never orphan a site you already have live. Everything else about that
row — **Approval Status**, the built link, when it was first created — is
left exactly as it was; only the prompt and the business name change.

If it's interrupted partway through a big batch, a re-run may rewrite a
handful of leads twice — there's no marker on the row for "already rewritten
today", so use **How many** the same way you would for any other expensive
step: small first, then the rest.

### 2. Review the briefs — one at a time

The card shows one business: its rating, what customers praise, the problem a
site would fix, and the full design brief behind a *Read the full brief*
toggle.

- **Approve — build this** → joins the build queue in step 3
- **Skip** → never built, never written to. This is how you say no
- **Decide later** → moves on without recording anything

**Approving spends nothing.** It only moves the row into step 3, where building
is a separate, deliberate action. So you can go through sixty quickly and still
have a second look before any money moves.

What to look for, in about ten seconds each: is the angle actually true of this
business, and would you be comfortable if the owner read the brief? If either
answer is no, Skip — a bad demo is worse than no demo.

### 3. Build the ones you approved

**This is the day's real work.** The card reads *sites to build*.

The site builder is a separate tool — the dashboard can't drive it directly, so
it gets you as close as possible. For each row in step 3:

1. **Copy prompt** → **Open your site builder** (Google AI Studio by default) → paste → let it build
2. Paste the finished site URL back into the box and press **Mark as built**

**Marking it built is not bookkeeping.** That URL is what goes in the email,
and marking it is what releases the lead for drafting — forget it and the
message never gets written.

**Give each one about a minute of your eyes.** Is the business name right? Any
`[TO CONFIRM]` left in the copy? You are about to send this to the owner; it
does not have to be perfect, but it has to be theirs.

Changed your mind about one? Press **Skip** on it back in step 2, or set its
**Approval Status** to `Skip` in the **Site Briefs** tab.

#### Or let Claude Code do the whole build step

If you have **Claude Code** installed on this machine, it can do all of step 3
for you. It's the one place that can reach both your Sheet *and* Lovable — your
local Python can't sign into Lovable, and a chat on claude.ai can't reach your
Sheet.

Connect Lovable once:

```powershell
claude mcp add --transport http lovable https://mcp.lovable.dev/
```

Then `/mcp` to sign in. After that, open Claude Code in this folder and say:

> build the approved sites

It reads the approved queue, asks you to confirm before spending credits,
builds each one in Lovable, and writes the URLs back to your Sheet. The rules
it follows live in `.claude/skills/build-sites/SKILL.md` — including never
editing a prompt and never building anything you haven't approved.

### 4. Open Gmail → Drafts

These are the leads whose demos were built earlier — every one of them has a
site to link to. For each draft:

- **Check the link is in there and it opens.** That link is the entire pitch.
  A broken one is worse than not writing at all.
- **Read the first sentence.** It should prove you actually looked at their
  business. If it could have been sent to any salon in Penang, rewrite it.
- Edit anything that reads stiff. It's a starting point, not a finished letter.
- Press send.

**Nothing sends by itself.** That's deliberate, and it's enforced in the code.

### 5. Open your Sheet → Outreach Log tab

Most leads won't have an email — businesses without websites usually don't
publish one. Those have a ready WhatsApp message instead, carrying the same
link. For these, WhatsApp is the better channel anyway: the preview unfurls
right in the chat, so they see the site without deciding to click anything.

- Copy the **WhatsApp Message** cell
- Open WhatsApp, paste it to the number in the **Leads** tab
- Change **Status** to `Sent`

### 6. Handle replies

When someone writes back, open the **Leads** tab and change their **Stage**:

- `Replied` — they answered
- `Closed` — they became a customer
- `Disqualified` — they asked you to stop, or they're not a fit

**`Disqualified` is permanent.** Nothing will ever contact them again. Use it
the moment anyone asks to be left alone — that's not just courtesy, it's the
opt-out obligation from the compliance rules.

---

## Using a different model

The pipeline ships on Claude Opus 5, but the model is a setting you change
from the dashboard. The **Model** card near the top shows what's in use:

> **claude-opus-5** — via anthropic · ANTHROPIC_API_KEY is set
> [Test it] [Change]

Press **Change** and you get three fields:

- **Where it runs** — `anthropic`, `ollama` (this machine), `ollama-cloud`,
  `openrouter`, `gemini`, or `openai-compatible`
- **Model** — e.g. `kimi-k2.6` on ollama-cloud,
  `moonshotai/kimi-k2-thinking` on openrouter, `gemini-2.5-flash` on gemini
- **API key** — saved into your `.env`

**The key is write-only.** It goes into `.env`, is never shown again, and is
never sent back to the page. The dashboard only ever remembers *whether* one is
set. Leave the field blank to keep the key you already have.

Local Ollama needs no key at all. For a self-hosted box on your network, fill
in **Endpoint** with something like `http://192.168.1.10:11434/v1`.

### How to be sure it actually works — press **Test it**

**Anthropic guarantees the reply matches the required shape. The others don't.**

Every stage needs strict JSON back — a research brief with six named fields, a
site brief with **nineteen**, including a palette and a motion language.
Anthropic enforces that at the API level. Ollama and OpenRouter *ask* for it;
good models comply, some treat it as a suggestion, and a small local model may
just reply in prose.

So don't take my word for it — **Test it** makes three real calls against an
invented barber shop and tells you:

```
Testing kimi-k2.6 via ollama-cloud

[ OK ]   2.1s  Research brief (6 fields): valid, every required field present
[ OK ]   1.8s  Outreach copy (3 fields): valid, every required field present
[FAIL]   4.0s  Site spec (19 fields -- the hard one): reply was missing
               required field(s): motion_language, micro_interactions

Not ready. What went wrong:
  ...
    fields it left out: motion_language, micro_interactions
```

That third line is the one that matters. **A model that passes the brief and
fails the spec will research your leads happily and then fail every single site
brief** — you'd only find out after paying for the research. Costs a few cents;
run it after every model change.

### If a model half-works

Every non-Anthropic reply is checked. Missing fields get one corrective retry
naming exactly what was absent. Fail twice and **that lead goes to the Errors
tab** rather than a half-filled brief reaching your Sheet — a stub becomes a
demo site with holes in it and an email built on nothing.

So the debugging loop is:

1. **Test it** — does the model hold all three schemas at all?
2. If yes, run Research on **5 leads** and read the angles. Passing the test
   proves the *shape*, not the quality of the thinking.
3. Watch the **Errors** tab. A couple is normal; a lot means the model isn't up
   to it. Switch back to `anthropic`, or try a larger model.

---

## Where tomorrow's 60 come from

Worth understanding, because it's the one thing that will eventually stop the
machine.

**Google gives at most about 60 results for any one search**, no matter how you
ask. Search "hair salon in Georgetown" and you get those 60 — and Google will
keep handing you the *same* 60 forever. That's Google's limit, not a setting
anyone can raise.

So the campaign file lists **18 areas across Penang**. Discovery walks down
that list, skipping every business already on your Sheet, until it has a full
batch. Day one drinks Georgetown dry; day two moves to Pulau Tikus; and so on.
That's what makes "60 new ones a day" real rather than a slogan.

Roughly speaking that's a few weeks of hair salons in Penang. When it runs out,
the run tells you plainly:

> Every area in this campaign is used up — Google has no more results to give
> for these searches.

That is your cue, and you never need to leave the dashboard for it. In
**What you're hunting** at the top, press **Change**:

- **Type of business** — `hair salon` → `barber shop`. Changing this reopens
  every area at once, since Google treats it as an entirely different search.
- **Search areas** — one per line. Add `Sungai Petani, Kedah, Malaysia`,
  `Ipoh, Perak, Malaysia`, anything you'd type into Google Maps.

Press **Save**, then **Run this morning's batch**. Same pipeline, new market.

**Nothing you've already done is touched.** Old leads keep their stage, their
sites, and their drafts. The Sheet grows a new hunt alongside the old one — so
changing your mind costs nothing and loses nothing.

> Save writes it into `config\campaigns\penang-my.yaml` for you, comments and
> all. Editing that file in Notepad still works exactly as before if you prefer
> it; there's just no longer any need to.

**Good categories to try:** hair salon, barber shop, cafe, dental clinic,
tuition centre, car workshop, florist, bakery, pet grooming, physiotherapy.

Businesses that live on walk-ins and word of mouth, where a customer would
plausibly search online first. Skip anything where the buyer never Googles.

> **One thing to know:** changing the category does **not** wipe anything. Old
> leads keep their stage and their sites. The Sheet just grows a second
> campaign alongside the first.

---

## What each step costs

| Step | Money |
|---|---|
| Find leads | A few cents in Google searches |
| Research | ~6 cents per lead |
| Write briefs | ~2 cents per lead |
| Rewrite existing briefs | ~2 cents per lead, same as Write briefs |
| Draft messages | ~2 cents per lead |
| **Review (step 2)** | **Free — and Skip here saves the expensive part** |
| **Build (step 3)** | **Your site builder's credits — by far the largest line** |
| Previews, Check setup | Free or near it |

A batch of 60 through everything *except* the build is roughly **six dollars**.
The builds are the number that matters, and they're charged by your site
builder, not by this tool — check your plan's credit allowance before you
approve a large batch.

That's the trade you chose: a demo costs real money, and it's the reason the
message lands at all. If the volume gets expensive, cut the number of leads —
not the demo. Twenty businesses who each get their own site will beat a hundred
who get a paragraph.

**Running the same option twice is free.** Every stage skips work it has
already done. If you're unsure whether something ran, just run it again —
you cannot double-charge yourself or double-message anyone.

---

## When something goes wrong

Open **Run one step on its own** in step 1 and press **Check setup**. It tests
every connection and says what's broken in plain English.

| What you see | What it means |
|---|---|
| `Places API returned 403` | Google billing lapsed, or the API got switched off |
| `Sheets API is not enabled` | Click the link in the message, wait 2 minutes, retry |
| `SHEET_ID is not set` | Your `.env` lost its Sheet ID |
| `postal_address is empty` | Someone cleared it from the campaign file |
| `N lead(s) are waiting on a demo site` | Working as intended. Build those sites, mark them built, then draft |
| `Every area in this campaign is used up` | Not a fault. Add areas or change the category — see *Where tomorrow's 60 come from* |
| Step 2 says `Nothing to review` | Either you've decided on them all, or step 1 hasn't written briefs yet |
| A browser opens asking you to log in | Normal if it's been ~6 months. Log in again |
| One lead failed | Check the **Errors** tab. The run continued without it |

The **Errors** tab is worth a glance weekly. A `Retryable` of `TRUE` means
running that stage again will probably fix it.

---

## Two habits worth keeping

**Open one finished demo a day, as the owner would.** On your phone, from the
link in the message you're about to send. If it's slow, generic, or has their
name wrong, the whole pitch inverts — you've just shown a stranger you don't
pay attention. This is the one check worth doing every single day.

**Read one brief a day.** Open the **Research** tab and read a
**Suggested Website Angle**. If it's vague, the sites built from it are vague.
That's your early warning that something needs tuning — tell whoever maintains
this.

**Never send a draft you haven't read.** The model is good, not infallible.
It writes to a real person who owns a real business, and your name is on it.
