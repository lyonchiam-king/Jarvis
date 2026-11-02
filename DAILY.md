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

## The two dashboards

The address opens the **current** dashboard — the dark one, with Today,
Campaigns, Contacts and Results down the side. Everything in this guide works
there.

The original page is still at **`/classic`** (so `http://127.0.0.1:5000/classic`
on your own machine). Nothing has been taken away from it; it is the fallback
if the newer page ever misbehaves. If you want it back as the default, set
`OUTREACH_CLASSIC_DASHBOARD=true` in your `.env`.

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

## Using it from your phone

The dashboard can run as an app on your iPhone. Two things to understand
first.

**Your PC is still doing the work.** The phone is a screen onto it. Discovery,
research, building, the WhatsApp relay — all of that runs on the computer, so
`Dashboard.bat` has to be running and the machine awake. Close the lid and the
phone shows nothing.

**It needs a password before it will leave your machine.** Until now the only
thing protecting the dashboard was that nothing could reach it. It can spend
your API credits and message real businesses, so on a shared wifi it must not
be open. Add a line to your `.env`:

    DASHBOARD_PASSWORD=something-only-you-know

Then start `Dashboard.bat`. **The black window now tells you which state you
are in.** Either:

    On your phone, on the same wifi, open:
        http://192.168.1.42:8765

— with your actual address, no `ipconfig` needed. Or:

    Phone access is OFF: no password is set.

If you see the second one, the password line did not take. Check there is no
space around the `=`.

**When Windows asks about the firewall, tick Private networks and allow it.**
The phone cannot connect otherwise, and this is the most common reason a
correct setup still fails.

**Then, on your phone:**

1. Same wifi as the PC. Open the address the window printed.
2. Enter the password.
3. Share button → **Add to Home Screen**.

It now has its own icon and opens full-screen with no Safari bar. Approving
briefs and sending messages work exactly as they do on the desktop.

**Away from home?** Install [Tailscale](https://tailscale.com) on both the PC
and the phone and sign in with the same account. They join a private network
of your own and the same address works from anywhere, with nothing exposed to
the internet. Free for personal use.

Never put this on a public address or forward the port on your router. The
password is the only thing between the page and whoever finds it.

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
   Drive with every tab already set up. The ID is saved for them — no
   spreadsheet to make by hand, no ID to copy out of a web address. If they
   close the Google window without finishing, nothing is created and they can
   just press it again.
2. **Their own keys.** Pasted into the Settings and Model panels on the page.
3. **Their own WhatsApp.** Section 4 shows a QR code; they scan it from
   WhatsApp → Linked devices, on their own phone, once.

To change which phone is linked — yours or theirs — press **Log out /
switch account** at the right of the connection line in section 4. It
unlinks the account, forgets the saved session, and shows a fresh QR a few
seconds later. Messages already sent stay in the phone; only the link goes.

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

## The other services on every message

Every message now ends with the other things you offer, under the website
link:

> Whether or not the site is right for you, these are the other ways I get
> businesses in front of customers:
> • Google Business Profile optimisation, so you show up in Maps when someone
>   nearby searches
> • Meta ads on Facebook and Instagram, targeted to your area
> • Google ads, so you appear the moment someone searches for what you do

The message opens on the gap rather than a compliment — the people who found
them already rate them highly, and the people who have not cannot find them at
all — then the site as the way to close it, then plainly what happens if they
like it and if they do not. It ends on getting found being the wider job, which
is what the list follows on from: the message finishes on their growth rather
than on a yes-or-no about one website.

> **If you pulled and the services did not appear:** your settings live in
> `penang-my.local.yaml`, which was copied from the shipped file once, the
> first time you saved a setting from the dashboard. Anything added to the
> shipped file after that never reached your copy. That is fixed — your copy
> now inherits any setting it does not mention — so a pull is enough.

**Edit the wording** in the box above step 4, *Other services listed at the
end of every message* — one service per line, plus the line that introduces
them. It is added word for word, so what you type is what they read; the model
never rewrites it. Clear the box and messages end as they did before.

Only new drafts pick it up. To add it to messages already written, press
**Draft messages** again for those leads, or **Rewrite** on a single one in
the send list.

## How much design the prompts ask for

Step 3 has a tick box, **Full art direction**. It is on.

With it on, every site prompt carries the craft rules as well as the brief:
type as one scale rather than picked sizes, section widths that vary instead
of a stack of identical centred boxes, a colour ratio that keeps the accent
for the booking button, and motion given in real durations rather than the
word "slow". It also tells the builder not to ship the component library's
default look, which is most of why generated pages recognisably look
generated.

It changes only the prompt you paste. The research is untouched, so it costs
nothing per lead and you can turn it on and off freely.

Turn it off if the pages come back worse — a longer prompt is not always a
better one, and you are the judge of that. To apply a change to briefs that
already exist, run **site-brief --rewrite**; a normal run skips anything
already briefed, by design.

## The second pitch: a picture instead of a page

A site is one way to open a conversation. The other is a single graphic that
names the few things costing them customers, and what the businesses doing
well do instead. Three cards, their name on it, your name along the bottom.

Nothing about the website track changes. This sits beside it.

### Switching it on

Campaign settings → **Track**:

| Track | What the message carries |
|---|---|
| **Website** | The demo site, linked. The original, and what you run today. |
| **Infographic** | The picture, attached. No site is built, and the message mentions no website at all. |
| **Both** | The picture attached **and** the site link in the text — still one message. |

**Both** is one message, not two. The same business messaged twice in a day is
the thing this whole tool works hardest to avoid, so the picture leads and the
link rides beside it.

You also have to fill in **Signature** — the ribbon along the bottom of every
graphic, e.g. *by Lyon, Business Growth Consultant*. Without it the tool
refuses to start, because a series of graphics with nothing identifying them
is not a series.

### Choosing who to write for

**Do not write sixty.** Each prompt is a visit to Google Flow and a picture to
read, so a batch of sixty is a week, not a morning.

Step 3b opens with **Who to write for**, which splits every lead waiting on a
prompt by how you would actually contact them:

- **On WhatsApp** — the check said yes. The ones you can send to tonight.
- **Email only** — no WhatsApp, but research found an address.
- **Already messaged once** — on WhatsApp, and you have written to them
  before with the website pitch. A picture is a fair second approach, so they
  are offered as their own group rather than mixed in with people who have
  never heard from you.
- **Not checked yet** — nothing can be written for these until the WhatsApp
  check has run, so there is no button on them. Run step 1b first.
- **No way to contact them** — a dead end, shown so the numbers add up.

The first four groups are everyone who has **not** been sent a picture. A lead
drops off this list the moment its graphic goes out, so the counts are work
left to do rather than a running total.

Set **How many** (10 to start), then either press **Write for this group** on
the group worth starting with, or open a group and tick the exact businesses
and press **Write for the ones I ticked**. Either way it writes that many and
stops.

### What a day looks like on this track

1. **Pick a group** in Who to write for and write 10 prompts.
2. **Step 3b, Infographic.** Press **Copy prompt**, paste it into Google Flow,
   generate the picture.
3. **Paste the picture back.** Click the box and press Ctrl+V, drop the file on
   it, or paste the link and press **Fetch it** — whichever you have.
4. **Proofread the picture.** The card shows it at about the size WhatsApp
   gives it in a chat, with the words that are supposed to be *on it* right
   underneath. Check the spelling, then press **The words are right**.
   
   Pressing that also writes the message, if it still needs writing — a lead
   held with no message, or one whose message was written for the website
   pitch before the picture existed. The card then shows the real message
   underneath, and says whether step 4 has it.
5. **Step 4, Send.** The picture is attached to the message. Press Send.
6. **It moves to Sent.** The card leaves the working lists and goes into
   **Sent**, so the three lists above it are always work still to do.

### The proofreading box is not the message

Worth being clear about, because the two sit next to each other.

The box under the picture is **for checking the picture**. Those are the words
the prompt asked Google Flow to draw. They are never sent to anybody — they are
only there so you can compare them against the image and catch a garbled one.

**The message is separate**, and it is written per business from that
business's own research, exactly like the website messages. Once you have
confirmed the picture, the card shows you that message in full, below the
proofreading box.

### What the picture's message says

It is not the website message with the link taken out. It is written from the
picture itself, and it leads with what the owner gets.

1. **The hook** — the thing the first card on the picture says about them.
2. **What is attached** — the few things costing them enquiries, and what the
   businesses doing well do instead, on one page they can read at a glance.
3. **The value** — the picture is theirs, it costs them nothing, and the
   fixes on it are ones they could do themselves. No request in this part at
   all: arriving with something useful and asking for nothing is the reason
   it gets read.
4. **The invitation** — if any of it is useful, or they want to know how one
   of them is done, they can message back and you will explain. No meeting,
   no call, no form, nothing to decide.

It says nothing about a website, because on this track there isn't one. No
link, no demo, no "I built you a page", and no offer to build anything. On
**Both** the site link comes back, in the text beside the picture.

It does **not** ask them which of the three items is wrong. That was the old
wording, and it is a request dressed as an offer.

### A business you have already messaged

Step 3b has a group for them, and the card for one of those leads looks
different: it shows **the message you sent before**, and the date.

That is there because the drafting step will not write to a lead that already
has an Outreach Log row — which is exactly what stops a business being
messaged twice, and also meant a picture made for one of them could never get
a message of its own. The card carries one button, **Write a new message for
this picture**, which clears the old send status and writes the new message
about the graphic. The picture and its prompt are kept; nothing is generated
twice.

The lead then appears in step 4 like any other, with the graphic attached.

### Where the send button is

On the current dashboard the steps are tabs across the top of a campaign:
**List → Reachability → Brief → Build → Infographic → Review & Send**. The
send button lives on that last tab, not on the Infographic one.

You do not have to hunt for it. A card whose message is waiting carries
**Go to Review & Send**, which takes you straight there, and so does the
card right after you press **Put it in Review & Send**.

The tab itself now carries a number for everything waiting there — messages
still to write *and* messages written and waiting to go. It used to count
only the first, so a graphic whose message was ready showed no number at all,
which reads as nothing to do.

### There is no send button on step 3b

On purpose. Everything sends from **step 4**, one queue, whichever track the
lead is on — so you never have two places to check and never message the same
business twice. The card on step 3b says so and links there.

Step 4 now also names which picture it is carrying: **Infographic attached**
or **Site screenshot attached**.

Step 4 holds a lead until step 4 of that list is done. That is deliberate:
until someone has read the words, there is no message, because the message is
written about a picture nobody has checked.

**If a picture you have confirmed is not in step 4**, the card now says so
outright, and says why in step 4's own words — "Not in step 4 yet — it was
drafted as an email, so it is in Gmail drafts", say. That line comes from the
same code that builds step 4's lists, so the two cannot disagree.

Most of those reasons have a button on the card itself:

- **"its Outreach Log row does not say Drafted"** — press **Put it in step 4**.
  That is the same repair as **Send again** in step 4: it clears the send
  status and sets the row back to Drafted, and writes nothing else. It is the
  usual reason, and it happens when a lead was skipped or its Status cell was
  edited.
- **no message yet** — the card carries **Write its message now**. Pressing
  it drafts that one lead, which is the only thing that can create its row;
  if it cannot, the card says which gate stopped it rather than claiming it
  worked. Pressing **The words are right** does the same thing, so a picture
  you confirm from now on gets its message without a second press — the
  button is there for every graphic confirmed before that.
- **already messaged** — drafting leaves that lead alone, which is what stops
  a business being written to twice. Press **Write a new message for this
  picture**.

The rest are real and no button fixes them: no phone number, no Leads row,
drafted as an email. Those need the lead fixing, so no button is offered —
one that pressed and changed nothing would read as the tool being broken.

Every drafted card also carries **Rewrite this message**, whether it is held
or not. It replaces the message with a fresh one written from the same
research and the same picture; the picture and the prompt are untouched.

### Why you have to read the words yourself

Google Flow is outside this tool. Jarvis writes the prompt and takes the
picture back, and at no point can it see what the picture says — so it cannot
spell-check it. A garbled word on a graphic with your name on it is the one
mistake that cannot be fixed after you press Send.

More elaborate layouts garble text more often. The winding road and the flip
cards are the two to watch.

### What a card is allowed to claim

This is the part worth understanding, because it is what keeps the pitch
honest.

A graphic makes three statements about a real business, and the owner can
check every one in about two seconds. So the cards are **not** written by the
model. They come from a fixed list of gaps, each with a test the research
either passes or does not:

- **No website** — Google lists none.
- **Everything lives in the feed** — their only link is a social page.
- **The same question keeps coming up** — drawn from their own reviews.
- **Great reviews, nowhere to send them** — a high rating with no site.
- **Only N reviews to go on** — Google's own count.
- **The phone is the only way in** — the email search found nothing.

Open **Why these cards** on any card and you will see which rule chose each one
and the evidence behind it. That line is stored on the Sheet, so a graphic you
sent months ago can still be defended.

The model's only job is to say the same thing in that trade's words — *term
fees* rather than *prices* for a tuition centre. It cannot add a card, and if it
tries, the extra one is thrown away.

**Two evidenced gaps means two cards.** Three is the format, not a quota.

**Fewer than two and the graphic goes out generic** — no business name, no
claim about them, just three gaps every small business recognises. The card
says **Generic** so you can see which is which before you send it, and the
message is forbidden from pretending it was made for them. A general picture
honestly described beats a specific one that is wrong.

### Changing how they look

Five things vary across the series and the content does not:

- **Style** — flat vector, paper-cut, claymation, chalkboard, retro comic,
  watercolour, pop-art.
- **Layout** — rows, a winding road, flip cards, sticky notes, a ladder.
- **Character** — an owl for tuition, a fox for a trade, a lightbulb for
  beauty. Matched to the trade, not rotated, so it always fits.
- **Palette** — the industry's, for now. Reading a business's real brand
  colours off their logo is not something this does yet, so it does not pretend
  to.
- The coral problem cards, the mint solution cards and your ribbon **never**
  change, so the series still reads as one person's work.

Style and layout rotate per industry and never repeat the previous lead's pair,
so ten salons in a row do not all get the same graphic. Once you know which
looks work, untick the rest in the Infographic step — that narrows every
industry at once.

**Different look** on a card re-rolls that one prompt onto the next pair in the
rotation. The picture and your confirmation are kept; it is the prompt that
changes.

### Where a sent one goes

Once a graphic's message has gone from step 4, its card leaves **Ready to
send** and moves to **Sent**. It is not in **All** either — All is work still
to do, the same reason **Removed** is not in it.

So the three counts at the top are always the work in front of you, not a
running total of everything the campaign has ever produced.

### Keeping the list short

Cards start **closed** — one line each, with the name, what it is waiting for,
and whether it claims anything about that business. Click one to open it.

Above them are five lists. It opens on **Need a picture**, because that is the
only one that needs anything from you:

| List | What is in it |
|---|---|
| Need a picture | Prompt written, nothing pasted back yet |
| Need checking | Picture is in, nobody has read the words on it |
| Ready to send | Checked — it goes out with the message |
| All | The three above |
| Removed | Ones you took off the list |

**Remove**, on an open card, takes it off the list for good — it stops being
held for a picture and is messaged with the page instead. It is not destroyed:
it moves to **Removed**, and **Put it back** undoes it.

**Wrong picture** forgets the image so you can paste a corrected one. The file
stays on disk; it cost a generation, and nothing here deletes your only copy of
it.

### What this does not do yet

- **No animation.** A moving file is a video to WhatsApp, not an image —
  different message type and megabytes rather than kilobytes. Still pictures
  first.
- **No brand colours.** Nothing reads their logo. The palette is the
  industry's.
- **No automatic spell-check.** See above — Flow is outside the tool.

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

### "Waiting for this message. This may take a while."

If messages you sent show this in your own WhatsApp, it is not your account
and not the recipient — it is the relay, and it is fixed as of this version.

WhatsApp encrypts separately for every device. When a device cannot decrypt
something, it asks the sender for that message again. The relay used to throw
the message away the moment it went out, so it had nothing to answer with, and
the message stayed stuck for ever — on the recipient's phone as much as yours.

**Update, then restart the relay window** (close it and run `Dashboard.bat`
again). New messages will be readable. The four already stuck cannot be
recovered; re-send those.

### A message you do not want to send

Every message in the send list has **Not this one** next to Send, and next to
Retry if it failed. It takes that business off the list without sending
anything.

It is not a disqualification — the lead keeps its research, its site and its
written message. The row moves down to the list at the bottom marked
*Skipped*, and **Send again** there puts it straight back. That is why it does
not stop to ask.

### "Sent, but it stayed in the list"

Fixed. WhatsApp often does not confirm delivery straight away — a first
message to a new contact especially — and the relay reports that honestly as
**unconfirmed**. The send list used to treat unconfirmed as "not sent yet", so
the business stayed at the top of the queue looking untouched. Sending again
from there would have messaged them twice.

An unconfirmed message now moves down to the sent list like any other, marked
so you can see it was not acknowledged. If it really did not arrive, use
**Send again**.

### A message says sent but never arrived

At the bottom of section 4, every drafted message you cannot send from there
is listed with the reason. The ones marked **Sent** now carry a **Send again**
button: press it and that business drops back into the send list above, with
the same message and the same screenshot, ready to send.

Nothing is regenerated, so it costs nothing. Use it whenever WhatsApp claimed
a message went out and it did not actually land.

### "Could not re-word ... Read timed out"

**Nothing is wrong and nothing needs redoing.** The prompt was still written.

Writing a prompt is two steps. The cards — the three problems and their fixes
— are chosen from your research by fixed rules, not by a model. Then one small
model call rephrases those cards in that trade's own words: *term fees* rather
than *prices* for a tuition centre.

Only the second step needs the model. When it cannot be reached, the card keeps
its standard wording, which is always true — just slightly less specific to
their trade. The graphic is fine to send.

If it fails twice in a row the run **stops asking** for the rest of that batch
and says so at the end. That is deliberate: each attempt waits half a minute
to produce exactly what not asking produces instantly, so ten leads would
otherwise be a long wait for nothing.

**If you want the tailored phrasing back**, press **Different look** on a card
once the model is answering again. **If you would rather never wait on it**,
set `visual.localise_wording: false` in your campaign file and every prompt
uses the standard wording immediately.

Worth knowing: that error names whatever model provider your campaign uses, so
if it keeps timing out, research and the message drafts will be slow too — it
is not something about the infographics.

### "...was retired on..." when a message is written

The model your campaign names has been withdrawn by whoever hosts it. It is
not a bad minute and it will not clear by waiting — that model is gone.

The error now says so, and passes on the provider's own sentence, which
usually names a replacement.

To fix it: **Settings** → **Change** beside the model → **Show the models my
key has**. That list is fetched from your own provider with your own key, so
it is the only list that is true today. Click **Use** beside the one you
want, then **Save**, then **Test it**.

Nothing in Jarvis guesses a model name for you, and none is printed anywhere
as an example: a name that is right today stops working without warning, and
this tool cannot check one. Clicking a name from that list also means you are
never retyping it — a typo produces a 404 that looks exactly like another
retirement.

### Which model writes what

There are two, and that is all:

| Setting | What it does |
|---|---|
| **Model** | Research, the site brief, and the infographic prompt. |
| **Message Model** | Every outreach message — the website ones **and** the infographic ones. Leave it blank to use the one above. |

So yes: the writing model covers both pitches. There is no separate model for
the picture messages, and nothing else to set up when you switch tracks.

If the message model is blank, the retired-model error is about the **Model**
field. If it is filled in, it is about that one.

Pressing **Write a new message for this picture** again before changing the
model will fail in exactly the same way.

### A message still has the website link in it

The message in step 4 is the one written when that lead was drafted. Changing
the campaign to **Infographic** changes what gets written *next* — it cannot
reach back and rewrite messages already on the Sheet, and those are the ones
step 4 sends.

So a card can show an infographic with a message underneath that offers a
demo site. Step 4 now flags exactly those: *"This message was written for the
website pitch and still links to the demo site."* Press **Rewrite** on that
card and the new message is about the picture, with no link.

Two things it does not flag, on purpose:

- a message with no link to the site built for that business — a compliance
  footer with an address in it is not the website pitch; and
- anything at all on the **Both** track, where the link is the point.

### "Your Sheet has no Visual Briefs tab yet"

Press **Add the missing tabs**, right under that message. That is the whole
fix.

Your Sheet was made before this tab existed, and a spreadsheet does not grow a
tab on its own when the tool updates — so the first thing to read it found
nothing there. Nothing is broken and nothing is lost.

The button adds only the tabs that are missing and rewrites only row 1, the
headers. Every lead, every note and every message you already have is
untouched, so pressing it twice costs nothing and pressing it on a healthy
Sheet does nothing at all.

It is the same button whenever this happens again — any future tab lands the
same way.

### Clearing out the leads with no WhatsApp

Open **Not on WhatsApp** in the WhatsApp check panel. Each row has a
**Remove** button, which disqualifies that business for good — it will never
be researched, briefed or built.

Two things to know. It asks once before acting, because nothing brings a
disqualified lead back. And a row marked **has email** can still be reached
that way, so removing it gives up a lead you could have contacted; the button
says so when that is the case.

### It connects, then drops, over and over

Almost always two relays running at once. WhatsApp allows one connection per
linked device, so a second copy does not sit quietly beside the first — they
take the connection from each other, and what you see is a relay that connects,
works for a moment, and drops.

It happened because closing the dashboard window with the X never stopped the
relay, so every later run started another one. Both halves are fixed:
`Dashboard.bat` now stops any old relay before starting a new one, and a relay
that finds the port already taken stops instead of fighting.

**If it is still happening,** open Task Manager, end every **Node.js** process,
then start `Dashboard.bat` again. Those are relays left over from before this
fix and nothing will clear them but that.

`relay-log.txt` now times every connect and drop, and says how long each
connection held. Under thirty seconds, repeatedly, is the signature of
something else using the same linked device — a second relay, or WhatsApp Web
open in a browser.

### It says "Reconnecting..."

That is the relay working its way back after the connection dropped, and it
needs nothing from you. It retries on its own, waiting a little longer each
time up to a minute, and the line tells you when the next attempt is due. It
goes back to **Connected** by itself.

Previously a dropped connection could leave the relay up but permanently
stuck, saying it was not connected and failing every send until you restarted
it. That is fixed — it now keeps trying for as long as it is running.

### The relay says "not working" and nothing explains why

It now writes everything it prints to **`relay-log.txt`** in your project
folder. The relay runs in a minimised window that closes the instant it
stops, so before this a crash left no trace at all — a relay that had died
and one that was never started looked exactly the same from the dashboard.

Open that file and read the last few lines. The connection line in section 4
also shows the relay's own reason now, rather than "Relay not running" for
every possible cause.


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
