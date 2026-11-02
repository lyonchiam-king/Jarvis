# Putting the dashboard on the internet

After this, the dashboard has its own web address. You open it from your
phone, in a hotel, on mobile data, with your PC switched off -- and it runs
discovery, research, the briefs and the messages without you.

This is the version for someone who has never used a terminal. Every step
says what you should see before you move on. Budget half an hour.

**I have never run any of this.** Nothing in this container can reach Fly,
build the image, or open a shell on a machine. The files are written and
checked against each other by the test suite; the first real deploy is
yours. If a step does something other than what it says here, stop and send
me the exact text on screen -- do not work around it.

---

## What it costs

The machine sleeps when nobody is using it and wakes on the first request,
so you pay for roughly the hour a day it works, not the month. Expect a few
dollars a month, plus a dollar or so for the small disk that keeps your
Google sign-in. Fly asks for a card at signup.

The first page load after it has been asleep takes a few seconds. Every one
after that is immediate.

## What does **not** move

**Sending WhatsApp messages.** The relay needs a phone linked to a machine
that stays awake, and running that in a data centre puts your number at more
risk rather than less. The hosted dashboard drafts everything and gives you
a **tap to send** link per lead that opens WhatsApp on your phone with the
message already written. You press send.

---

## 1. Install the Fly command

Open **PowerShell** (press Start, type `powershell`, press Enter) and paste
this one line:

```powershell
iwr https://fly.io/install.ps1 -useb | iex
```

**Close PowerShell and open it again** -- it will not find the command until
you do.

Check it worked:

```powershell
fly version
```

You should see a version number. If you see "not recognised", the reopen
step was missed.

## 2. Make an account

```powershell
fly auth signup
```

A browser opens. Sign up, add a card. Come back to PowerShell when it says
you are logged in.

> Already have an account: `fly auth login`.

## 3. Go to the project folder

```powershell
cd $HOME\Documents\Jarvis
```

Use whatever folder holds `Dashboard.bat`. Then:

```powershell
git pull
```

## 4. Pick a name

Open `fly.toml` in Notepad. The first line is:

```toml
app = "jarvis-outreach"
```

That name has to be unique across all of Fly, so it is probably taken. Put
something of your own there -- `coai-outreach-lyon`, say. Letters, numbers
and dashes only. Save the file.

Then create it:

```powershell
fly apps create coai-outreach-lyon
```

Use your name, not mine. If it says the name is taken, choose another and
change `fly.toml` to match.

## 5. Make the disk

This is the one thing that survives a redeploy -- your Google sign-in lives
on it.

```powershell
fly volumes create outreach_data --size 1 --region sin
```

Say yes when it warns about running a single volume. `sin` is Singapore;
change it only if you are not in this part of the world.

## 6. Put the keys in

These are **not** written into any file. They go into Fly's own secret
store, and the only way to change one afterwards is to set it again.

Set the password first, on its own, because the app refuses to start
without it:

```powershell
fly secrets set DASHBOARD_PASSWORD="choose-a-long-one-here"
```

Make it long. Twelve characters is the minimum the app accepts and it is a
low bar: this address is on the internet and gets scanned within minutes of
existing. A phrase you can type on a phone keyboard beats a short jumble.

Then the rest, in one go -- the same values that are in your `.env` at home:

```powershell
fly secrets set PLACES_API_KEY="..." SHEET_ID="..." ANTHROPIC_API_KEY="..."
```

Leave out any you do not use. Add `OLLAMA_API_KEY` or `OPENROUTER_API_KEY`
the same way if your campaign uses one.

> If a value contains a `#` or a space, keep the quotes exactly as shown.

## 7. Deploy

```powershell
fly deploy
```

It builds on Fly's machines, not yours -- you do not need Docker. Five to
ten minutes the first time, because it installs a browser for the
screenshots.

When it finishes:

```powershell
fly open
```

Your browser opens the address. You should see the login page. **Sign in
with the password from step 6.**

Every panel will be empty and every count zero. That is correct -- it cannot
reach your Sheet yet. That is the next step, and the last one.

## 8. Give it your Google sign-in

A server cannot sign in to Google. Signing in means a browser opens and a
person clicks Allow, and there is no browser and no person on a machine in
Singapore. So you sign in **on your own PC**, once, and copy the result up.

**On your PC**, if you have not already: double-click `run.bat` and press
**9** -- "Check everything works". A browser opens, you approve, and it
writes `credentials\token.json`. If that file is already there, skip
straight on.

Now make the folder on the server:

```powershell
fly ssh console -C "mkdir -p /data/credentials"
```

Copy both files up:

```powershell
fly ssh sftp shell
```

At the `»` prompt, type these two lines, then `quit`:

```
put credentials/oauth_client.json /data/credentials/oauth_client.json
put credentials/token.json /data/credentials/token.json
```

Then hand them to the user the app runs as:

```powershell
fly ssh console -C "chown -R 10001:10001 /data"
```

Restart so it picks them up:

```powershell
fly apps restart coai-outreach-lyon
```

Reload the page. The counts should now be real.

> **Never send these two files to anyone, including me.** They are your
> Google account. Anyone holding them can read and write the Sheet as you.

---

## Put it on your phone

Open the address in **Safari** on the iPhone. Sign in. Press the **share**
button (the square with the arrow), scroll down, **Add to Home Screen**.

It gets its own icon and opens without Safari's address bar.

The address opens the current dashboard. The original page is still there at
`/classic` if you ever want it -- it installs separately and gets its own
icon, so you can have both on the home screen. To make `/classic` what the
plain address opens, set `OUTREACH_CLASSIC_DASHBOARD=true`.

---

## Running it every morning by itself

Off by default, on purpose: a scheduled run spends money on lookups,
research, a brief and a message for every lead it finds, and nobody should
learn that from a bill.

To turn it on you must do **both** of these, or it will never run:

```powershell
fly secrets set OUTREACH_DAILY_RUN="07:30"
```

and in `fly.toml`, change:

```toml
  min_machines_running = 0
```

to `1`, then `fly deploy` again. A sleeping machine runs nothing, so
auto-stop and a morning run cancel each other out -- and the symptom is
simply that nothing ever happens.

It runs discovery, research, the briefs and the messages. It stops before
building sites and before sending anything: those two steps cost the most
and reach a real business, so they still wait for you.

The time is read in Malaysian time (`TZ` in `fly.toml`). The startup line in
`fly logs` prints the exact date and time it worked out, so you can check it
rather than trust it.

---

## When something is wrong

```powershell
fly logs
```

That is the first thing to look at, always, and usually the last.

| What you see | What it means |
|---|---|
| "Refusing to start with no password set" | Step 6 was missed, or the secret name is misspelt |
| The page loads but every count is zero | Step 8 -- no token on the disk |
| "The Google sign-in is missing" | Same, and the log names the exact path it looked at |
| "could not be renewed" | The token was there and Google rejected it. Delete `credentials\token.json` on your PC, run `run.bat` option 9 again, re-upload the new one |
| Restarts over and over | `fly logs` will say why. It is not the health check: `/healthz` is the one address that answers without signing in |

To stop paying for it:

```powershell
fly apps destroy coai-outreach-lyon
```

That deletes the disk and the Google token with it. Your Sheet is
untouched -- everything the pipeline has produced is in it.
