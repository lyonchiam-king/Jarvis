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

## The honest shape of a day

The tool does about five minutes of work. The other thirty are yours, and
that's the part that makes money. Don't let the automation fool you into
thinking the job is done when the Sheet fills up.

| | What | Who |
|---|---|---|
| **5 min** | Find, research, and draft | the tool |
| **20 min** | Read the drafts, edit, send | **you** |
| **10 min** | Reply to anyone who wrote back | **you** |

---

## Your morning, step by step

### 1. Open the dashboard, press **Run everything**

That finds new leads, researches them, writes website prompts, and drafts your
messages. Takes a few minutes — output streams down the page, and you can walk
away.

When it's done, the **What needs you today** cards refresh. Those are your
to-do list: how many emails are waiting to be sent, how many WhatsApp messages
to copy, and anything that errored.

### 2. Open Gmail → Drafts

Every lead with an email address has a draft waiting. For each one:

- **Read the first sentence.** It should prove you actually looked at their
  business. If it could have been sent to any salon in Penang, delete the
  draft — a generic message is worse than no message.
- Edit anything that reads stiff. It's a starting point, not a finished letter.
- Press send.

**Nothing sends by itself.** That's deliberate, and it's enforced in the code.

### 3. Open your Sheet → Outreach Log tab

Most leads won't have an email — businesses without websites usually don't
publish one. Those have a ready WhatsApp message instead.

- Copy the **WhatsApp Message** cell
- Open WhatsApp, paste it to the number in the **Leads** tab
- Change **Status** to `Sent`

### 4. Handle replies

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
| 2. Research leads | **~6 cents per lead** — the main cost |
| 3. Website prompts | ~2 cents per lead |
| 4. Draft messages | ~2 cents per lead |
| 6, 7. Previews | Option 6 costs the same as option 1. Option 7 costs the same as option 4 |
| 8, 9 | Free |

A hundred leads all the way through is roughly **ten dollars**. One closed
deal pays for a year of it.

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
| A browser opens asking you to log in | Normal if it's been ~6 months. Log in again |
| One lead failed | Check the **Errors** tab. The run continued without it |

The **Errors** tab is worth a glance weekly. A `Retryable` of `TRUE` means
running that stage again will probably fix it.

---

## Two habits worth keeping

**Read one brief a day.** Open the **Research** tab and read a
**Suggested Website Angle**. If it's vague, your emails are vague. That's your
early warning that something needs tuning — tell whoever maintains this.

**Never send a draft you haven't read.** The model is good, not infallible.
It writes to a real person who owns a real business, and your name is on it.
