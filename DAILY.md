# Running this every day

**Double-click `Dashboard.bat`.** Your browser opens with everything on one
page: what needs you today, where every lead sits, and buttons to run each
stage.

### Put it on your desktop, once

Right-click `Dashboard.bat` → **Send to → Desktop (create shortcut)**. Rename
the shortcut to *SME Outreach*. From then on it's a desktop icon like any
other app.

> **Prefer a plain menu?** `run.bat` gives you a numbered list in a terminal
> window instead. Same actions, no browser.
>
> **On a Mac:** rename `run.sh` to `run.command` and run `chmod +x run.command`
> once. Or `outreach dashboard` for the browser version.

The dashboard runs on your machine only — nobody else can reach it, and it
holds no passwords of its own.

---

## The demo is the pitch

**Every lead gets a site built before you write to them.** Not a description of
a site. The actual thing, with their name on it, at a URL they can tap.

These owners have never had a website. Telling one that "a booking page would
help you" asks them to picture something they have no reference for — and it
sounds like every other script in their inbox. Sending them *their own salon,
already built*, doesn't ask them to imagine anything. That's a different
conversation, and it's the only version of this that works now.

So the pipeline is wired that way and won't let you skip it:

| Setting | Value | What it does |
|---|---|---|
| `outreach.require_site` | `true` | No message is drafted until that lead's site exists |
| `lovable.auto_approve` | `true` | New briefs arrive `Approved` — every lead gets built |

Both live in `config\campaigns\penang-my.yaml`.

**The cost is real, so know the number.** The build is your most expensive
step. If you'd rather not build for everyone, don't flip `require_site` off —
just open the **Site Briefs** tab and set `Approval Status` to `Skip` on the
ones you don't want. A `Skip` is never built, and a lead with no site is never
written to. That way the filter is *you choosing which businesses are worth
it*, which is a better filter than sending everyone a thinner message.

**Nothing builds behind your back.** `Approved` means "queued", not "built".
Building only ever happens when you press **Build approved sites** or tell
Claude Code to do it, and it asks before spending.

---

## Zero-touch mornings

Right-click **`Schedule-Daily.ps1`** → *Run with PowerShell*, once.

From then on it runs at 8am every day: finds leads, researches them, writes
their site prompts, and drafts messages for every lead **whose demo you built
yesterday**. Capped at 20 leads a day — roughly USD 2 — so it can't run away
with your money overnight.

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
| **5 min** | Find, research, write prompts, draft messages | the tool |
| **20 min** | Build today's demo sites | **you** (or Claude Code) |
| **20 min** | Read yesterday's drafts, edit, send | **you** |
| **10 min** | Reply to anyone who wrote back | **you** |

---

## Your morning, step by step

### 1. Open the dashboard, press **Run everything**

That finds new leads, researches them, writes each one's site prompt, and
drafts messages for the leads whose demos are already built. Takes a few
minutes — output streams down the page, and you can walk away.

When it's done, the **What needs you today** cards refresh. That's your
to-do list, in the order you should work it.

> **Expect `0 need messages drafted` on your first run.** Nothing has a demo
> yet, so there is nothing to write about. Build the sites in step 2 and press
> **Draft messages** — or just leave it for tomorrow's run.

### 2. Build today's demo sites

**This is the day's real work.** The card reads *approved, not built*.

Lovable can't be driven from here — it only supports OAuth sign-in, so there's
no key a program on your laptop could use. The dashboard gets you as close as
possible. For each row in **Ready to build**:

1. **Copy prompt** → **Open Lovable** → paste → let it build
2. Paste the finished site URL back into the box and press **Mark as built**

Step 2 is not bookkeeping. That URL is what goes in the email, and marking it
is what releases the lead for drafting — skip it and the message never gets
written.

**Give each one about a minute of your eyes.** Is the business name right? Any
`[TO CONFIRM]` left in the copy? You are about to send this to the owner; it
does not have to be perfect, but it has to be theirs.

Don't want to build one? Set its **Approval Status** to `Skip` in the
**Site Briefs** tab. That lead is then never built and never written to.

#### Or let Claude Code do the whole build step

If you have **Claude Code** installed on this machine, it can do all of step 2
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

### 3. Open Gmail → Drafts

These are the leads whose demos were built earlier — every one of them has a
site to link to. For each draft:

- **Check the link is in there and it opens.** That link is the entire pitch.
  A broken one is worse than not writing at all.
- **Read the first sentence.** It should prove you actually looked at their
  business. If it could have been sent to any salon in Penang, rewrite it.
- Edit anything that reads stiff. It's a starting point, not a finished letter.
- Press send.

**Nothing sends by itself.** That's deliberate, and it's enforced in the code.

### 4. Open your Sheet → Outreach Log tab

Most leads won't have an email — businesses without websites usually don't
publish one. Those have a ready WhatsApp message instead, carrying the same
link. For these, WhatsApp is the better channel anyway: the preview unfurls
right in the chat, so they see the site without deciding to click anything.

- Copy the **WhatsApp Message** cell
- Open WhatsApp, paste it to the number in the **Leads** tab
- Change **Status** to `Sent`

### 5. Handle replies

When someone writes back, open the **Leads** tab and change their **Stage**:

- `Replied` — they answered
- `Closed` — they became a customer
- `Disqualified` — they asked you to stop, or they're not a fit

**`Disqualified` is permanent.** Nothing will ever contact them again. Use it
the moment anyone asks to be left alone — that's not just courtesy, it's the
opt-out obligation from the compliance rules.

---

## Weekly, not daily

Georgetown has a finite number of hair salons. Once discovery stops finding
new ones, change the search.

Open `config\campaigns\penang-my.yaml` in Notepad and edit two lines:

```yaml
campaign:
  category: "hair salon"                          # try: cafe, dental clinic, tuition centre
  location: "Georgetown, Penang, Malaysia"        # try: Bayan Lepas, Butterworth, Ipoh
```

Save, then press `1` in the menu. Same pipeline, new market.

**Good categories to try:** hair salon, barber shop, cafe, dental clinic,
tuition centre, car workshop, florist, bakery, pet grooming, physiotherapy.

Businesses that live on walk-ins and word of mouth, where a customer would
plausibly search online first. Skip anything where the buyer never Googles.

---

## What each menu option costs

| Option | Money |
|---|---|
| 1. Find new leads | A few cents in Google searches |
| 2. Research leads | ~6 cents per lead |
| 3. Website prompts | ~2 cents per lead |
| 4. Draft messages | ~2 cents per lead |
| **Building the sites** | **Lovable credits — by far the largest line** |
| 6, 7. Previews | Option 6 costs the same as option 1. Option 7 costs the same as option 4 |
| 8, 9 | Free |

A hundred leads through everything *except* the build is roughly **ten
dollars**. The builds are the number that matters, and they're charged by
Lovable, not by this tool — check your plan's credit allowance before you queue
up a hundred of them.

That's the trade you chose: a demo costs real money, and it's the reason the
message lands at all. If the volume gets expensive, cut the number of leads —
not the demo. Twenty businesses who each get their own site will beat a hundred
who get a paragraph.

**Running the same option twice is free.** Every stage skips work it has
already done. If you're unsure whether something ran, just run it again —
you cannot double-charge yourself or double-message anyone.

---

## When something goes wrong

Press `8` first. It checks everything and says what's broken in plain English.

| What you see | What it means |
|---|---|
| `Places API returned 403` | Google billing lapsed, or the API got switched off |
| `Sheets API is not enabled` | Click the link in the message, wait 2 minutes, retry |
| `SHEET_ID is not set` | Your `.env` lost its Sheet ID. Press `9` won't work either |
| `postal_address is empty` | Someone cleared it from the campaign file |
| `N lead(s) are waiting on a demo site` | Working as intended. Build those sites, mark them built, then draft |
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
