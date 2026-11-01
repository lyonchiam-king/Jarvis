# Setting this up on your computer

For someone who has never used a terminal. Follow it in order; every step
says what you should see before you move on.

You will need about 30 minutes, most of it waiting for Google.

---

## 1. Get the files

**Ask Lyon to add you to the repository**, then on the repository page in
GitHub press the green **Code** button and choose **Download ZIP**.

Unzip it somewhere under your **Documents** folder. Not the Desktop, and
not inside OneDrive if you can avoid it -- both cause permission problems
later.

You should end up with a folder containing `Dashboard.bat`.

> If Lyon sends you a ZIP directly, the same applies -- unzip it under
> Documents.

---

## 2. Install two free programs

**Python** -- https://www.python.org/downloads/

On the **first screen of the installer**, tick **"Add python.exe to PATH"**
before pressing Install. This one tick causes most first-time failures when
it is missed.

**Node.js** -- https://nodejs.org (take the "LTS" button)

Accept the defaults. This one is only needed for sending WhatsApp messages.

Restart your computer after both. Windows does not always notice new
programs until you do.

---

## 3. Start it once

Double-click **`Dashboard.bat`**.

The first run takes a few minutes: it builds its own private Python folder,
installs everything, and installs the WhatsApp part. A black window will
show what it is doing. **Leave that window open** -- closing it stops the
dashboard.

It will then open Notepad on a file called `.env` and stop, saying your keys
are not filled in yet. That is expected. Leave it for the next step.

> **"Python is not installed"** -- you missed the PATH tick in step 2.
> Reinstall Python and tick it.

---

## 4. Your own Google setup

Everything here is yours, not Lyon's. Your own leads, your own bill, your
own Google account.

Follow **README.md, section "First-time Google Cloud setup"**, steps 1 to 5.
It creates:

- a Google Cloud project with four APIs turned on
- a **Places API key** -- for finding businesses
- an **OAuth client** -- so the tool can use *your* Google Sheet and Gmail

Save the OAuth file it gives you as `credentials/oauth_client.json` inside
your project folder.

You also need a model key, either:

- **Anthropic** -- https://console.anthropic.com -> API Keys, or
- **Gemini** -- https://aistudio.google.com/apikey

---

## 5. Make your own Sheet

Double-click `Dashboard.bat` again. This time the dashboard opens in your
browser, and the first thing on the page is a short setup list.

Press **Create my Google Sheet**. A Google window opens; sign in with your
own account and allow access. The spreadsheet is made **in your Drive**,
with every tab already set up, and the dashboard remembers it. You never
have to create a spreadsheet or copy an ID out of a web address.

> If you close that Google window without finishing, nothing is created.
> Just press the button again.

Your leads live in that Sheet. Nobody else can see it -- not Lyon, not
anyone you got this from.

---

## 6. Put the keys in

Use the **Settings** and **Model** panels on the page to paste your keys.
You do not need to edit any file -- the page writes them for you.

Then press **Check setup**. It tells you, in plain English, anything still
missing. Keep fixing until it is happy.

---

## 7. Connect your WhatsApp

In section 4 of the dashboard, a **QR code** appears.

On your phone: WhatsApp -> Settings -> **Linked devices** -> **Link a
device** -> scan the code.

That is your own WhatsApp, linked to your own copy. It stays linked; you
only do this once.

---

## 8. Your first run

Set **How many** to `5` -- small, so a mistake is cheap.

Then, in order:

1. **Find leads**
2. **Check WhatsApp** -- who can actually be messaged
3. **Research** -- only the reachable ones
4. **Write briefs**, then approve the ones you like
5. Build the sites, **Draft messages**, and send

---

## Things worth knowing

**Never share your `.env` file, your `credentials` folder, or your
`.whatsapp-session` folder.** They are your keys, your Google account and
your WhatsApp. Anyone with them can spend your money and message as you.

**Keep the black window open** while you work.

**To update later**, ask Lyon for the new ZIP and replace everything
*except* `.env`, `credentials/`, and `.whatsapp-session/` -- those are
yours and must survive.

**Costs are yours.** Google gives new accounts free credit; Places and the
model both bill after that. Start at 5 leads a day until you know what it
costs you.

---

# The sign-in that never expires

If your Google sign-in keeps dying — "expired and could not be renewed",
every week or two — this replaces it for good. No consent screen, nothing to
publish, no "unverified app" warning, and nothing to sign in to again.

A **service account** is a robot Google account. It is not you, it has its own
address, and it refreshes itself from a file on your computer. It owns
nothing until you share something with it, which is the one step people
forget.

## 1. Make the robot account

Open this, in the project the tool already uses:

https://console.cloud.google.com/iam-admin/serviceaccounts?project=sme-outreach-504919

1. **CREATE SERVICE ACCOUNT**
2. Name it **jarvis-sheets**. Press **CREATE AND CONTINUE**
3. It asks for a role — **skip it**, press **CONTINUE**, then **DONE**.
   It needs no project role at all; only the Sheet matters

## 2. Download its key

1. Click the account you just made
2. **KEYS** tab → **ADD KEY** → **Create new key** → **JSON** → **CREATE**
3. A `.json` file downloads. That file is a password — treat it like one.
   Never email it, never paste it into a chat

## 3. Put it where the tool looks

Move the downloaded file into your Jarvis folder, inside **`credentials`**,
and rename it to exactly:

```
service_account.json
```

That is the whole switch. The file being there is what turns this on; delete
it and the old sign-in is used again.

## 4. Share your Sheet with it

The robot cannot see your Sheet until you invite it.

**Which Sheet?** The one the tool made for you during setup — it is the
database. Every lead, every message and every status lives in it. Open it
from the dashboard's **Open Your Google Sheet** button, or from the link
`run.bat` → **9** prints.

1. Open the service account's page in the Console and copy its **Email** —
   it looks like `jarvis-sheets@your-project.iam.gserviceaccount.com`
2. Open that Sheet → **Share**
3. Paste that address, give it **Editor**, press **Send**
4. Ignore the warning about sharing outside your organisation. That address
   is yours

## 5. Check it

Double-click `run.bat`, press **9**. You should see:

```
[ OK ] Reading the Sheet as jarvis-sheets@...
       This one does not expire. Nothing to sign in.
[ OK ] Google Sheets API enabled
[ OK ] The Sheet is shared with it
```

If the last line fails, step 4 was missed — it tells you the address to
share with.

## What this does not cover

**Email drafts.** A service account cannot write a draft in your mailbox;
only you can. Those still use the Google sign-in, so while it is expired you
get a warning and no email drafts — and everything else carries on. WhatsApp
copy is written into the Sheet and needs no Google identity at all.

If you want email drafts back, sign in once more with `run.bat` → 9. If you
never draft emails, you can ignore the sign-in from now on.
