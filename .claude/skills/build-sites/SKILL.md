---
name: build-sites
description: >
  Build the approved lead websites in Lovable. Use when the user asks to build
  sites, build the approved briefs, run the Lovable step, or make the websites
  for their leads. Reads the approved queue from the outreach pipeline, creates
  each project in Lovable via MCP, and records the result back to the Sheet.
---

# Building approved lead sites in Lovable

The outreach pipeline generates a Lovable prompt per lead but cannot build
anything itself: Lovable authenticates with OAuth only and publishes no
headless credential, so a local program has no way to sign in. You do, through
the Lovable MCP connector. That is the entire reason this skill exists.

## Before you start

Check the Lovable MCP tools are actually available to you. If `create_project`
is not in your tool list, stop and tell the user to connect it:

```
claude mcp add --transport http lovable https://mcp.lovable.dev/
```

Then `/mcp` to sign in. Do not attempt any workaround — without the connector
there is no path to Lovable, and a half-measure wastes their time.

## The loop

### 1. Read the queue

```bash
outreach briefs pending --json
```

Returns a JSON array of `{lead_id, business_name, prompt}` — the briefs marked
`Approved` in the Sheet that have no project yet. **An empty array means stop**
— say so and do nothing else. It does not mean "go find something to build".

Note the campaign may run `lovable.auto_approve: true`, which seeds new briefs
as `Approved` rather than `Pending`. That is a deliberate demo-first strategy —
build a site for every lead, because the demo is the pitch — and it means this
queue can be long without the user having clicked anything. It does **not**
make the queue self-authorising: step 2 is the gate, and it matters more, not
less, when the list arrives pre-approved.

### 2. Confirm before spending

`create_project` consumes Lovable credits. Show the user the list of business
names and how many projects you are about to create, and get a yes.

If they approved 40 briefs, ask whether they want all 40 now or a first batch
of 5 to check quality. Suggest the smaller batch on the first run — a prompt
flaw multiplied across 40 sites is an expensive way to discover it.

### 3. Build each one

For each item in the queue, call `create_project` with the `prompt` exactly as
given. **Do not improve, shorten, or reword the prompt.** It was generated from
research on that specific business and already carries the constraints that
stop the builder inventing prices, staff names, or opening hours. Your edits
would strip that grounding without you knowing what you removed.

Use the `business_name` as the project name.

### 4. Record the result

After each successful build:

```bash
outreach briefs record --lead-id <lead_id> --preview-url <url> --project-id <id>
```

This writes the URL to the Sheet and advances the lead to `Site Built`.

**Record after each build, not in a batch at the end.** If the run dies at
project 12, the first 11 must already be recorded or the user pays to build
them again.

The preview URL is not bookkeeping — the outreach email includes it, and a lead
who can click a live site built for their own business is a far stronger
message than one who cannot.

### 5. Report

Tell the user what was built, with the URLs, and what failed. If a build
failed, say which lead and why. Do not retry silently more than once — a
repeated failure usually means the prompt or the account needs attention, and
burning credits on retries is the wrong response.

## What not to do

- **Never build a brief that is not in the `pending` output.** A row set to
  `Skip` is a decision, and under auto-approve it is the *only* way the user
  says no. Honour it.
- **Never spend without an explicit yes in this conversation.** The approval
  column may have been seeded by config; the confirmation in step 2 cannot be.
- **Never edit the prompt.** See step 3.
- **Never invent a preview URL.** If Lovable did not return one, record with
  `--project-id` alone and say the URL is missing.
- **Never mark something built that was not.** The Sheet is the user's source
  of truth for who has been contacted with what.
