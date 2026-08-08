# Running this every day

**Double-click `Dashboard.bat`.** Your browser opens on one page laid out in
the order you actually work:

| | | |
|---|---|---|
| **1** | **Gather** | One button. Finds a fresh batch, researches them, writes their briefs |
| **2** | **Review** | One business at a time. Approve or Skip |
| **3** | **Build** | The approved ones, ready to paste into Lovable |
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

It never sends and never spends Lovable credits. Logs land in `logs\`.

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

> **Expect `0 need messages drafted` on your first run.** Nothing has a demo
> yet, so there is nothing to write about. That corrects itself from tomorrow.

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

Lovable can't be driven from here — it only supports OAuth sign-in, so there's
no key a program on your laptop could use. The dashboard gets you as close as
possible. For each row in step 3:

1. **Copy prompt** → **Open Lovable** → paste → let it build
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
| Draft messages | ~2 cents per lead |
| **Review (step 2)** | **Free — and Skip here saves the expensive part** |
| **Build (step 3)** | **Lovable credits — by far the largest line** |
| Previews, Check setup | Free or near it |

A batch of 60 through everything *except* the build is roughly **six dollars**.
The builds are the number that matters, and they're charged by Lovable, not by
this tool — check your plan's credit allowance before you approve a large batch.

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
