# Jarvis

Your personal consultant: a mastermind of virtual mentors built from the public
work of people you actually want to learn from. Point it at someone's YouTube
channel, and it turns hundreds of videos into a structured, queryable persona
you can chat with or get daily nudges from.

## Why not just use NotebookLM?

NotebookLM is fine for one-off Q&A over a pile of transcripts, but it's a black
box: you can't structure the output into a reusable persona, version it, combine
several mentors into one system, or wire it into something you talk to daily.
Jarvis instead runs a pipeline you fully control:

```
YouTube channel
  -> list videos                (yt-dlp, metadata only, no download)
  -> pull caption transcripts   (youtube-transcript-api)
  -> extract key points/video   (Claude: principles, frameworks, advice, quotes)
  -> synthesize a persona       (Claude: rolls all videos into one knowledge_base.md)
  -> chat / mastermind panel    (Claude, grounded in that knowledge base)
  -> daily digest/nudge         (Claude, one grounded nudge per day)
```

Every stage writes plain files to `data/`, so the result is inspectable,
diffable, and greppable — not locked in a notebook.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # fill in ANTHROPIC_API_KEY
```

Ingestion (`jarvis ingest`) needs normal internet access to reach YouTube —
run it from your own machine, not a network-restricted sandbox.

## Adding a mentor

```bash
jarvis mentor add \
  --slug iman-gadzhi \
  --name "Iman Gadzhi" \
  --channel-url https://www.youtube.com/@ImanGadzhi \
  --focus entrepreneurship --focus "agency/business building" --focus "marketing and sales"
```

Iman Gadzhi is already seeded in `data/mentors/iman-gadzhi/profile.yaml` as the
pilot mentor — add more the same way as you pick your next elite to follow.

## Building a mentor's knowledge base

```bash
jarvis ingest iman-gadzhi --limit 20     # start small; drop --limit for the whole channel
jarvis synthesize iman-gadzhi            # rolls every ingested video into knowledge_base.md
```

`ingest` skips videos it's already processed, so re-running it later only
picks up new uploads. `synthesize` can be re-run any time you want to fold in
freshly ingested videos.

## Talking to your mentors

```bash
jarvis chat iman-gadzhi                        # one-on-one
jarvis chat iman-gadzhi naval-ravikant          # mastermind panel: both respond to each question
```

Each mentor stays grounded in their own `knowledge_base.md` and will say so
honestly if a question falls outside what they've actually covered on camera,
rather than inventing an opinion.

## Daily nudge

```bash
jarvis digest                 # every mentor with a knowledge base, prints + appends to data/digests/
jarvis digest iman-gadzhi      # just one mentor
```

Digests are pluggable (`src/jarvis/digest.py`, the `Notifier` protocol) — stdout
and a `data/digests/YYYY-MM-DD.md` file ship today. Wire in Telegram or email by
implementing `Notifier.send()` and passing it to `generate_digest`, then call
`jarvis digest` from cron or a scheduled GitHub Action.

## Project layout

```
src/jarvis/
  ingest/youtube.py   channel listing + transcript fetch (no video download)
  ingest/extract.py   per-video key point extraction (LLM)
  synthesize.py       rolls all of a mentor's videos into one persona doc
  persona.py          builds chat system prompts (single mentor + mastermind panel)
  chat.py             chat session + REPL
  digest.py           daily nudge generator + notifiers
  mentors.py          mentor registry (data/mentors/<slug>/profile.yaml)
  llm.py              thin Claude API wrapper (swappable for tests)
data/mentors/<slug>/
  profile.yaml          name, channel, focus areas
  videos/<id>.json       per-video extracted key points (committed — this is your index)
  knowledge_base.md      synthesized persona (committed)
data/digests/YYYY-MM-DD.md   daily nudge log (committed)
```

Raw transcripts are cached locally in `data/raw_transcripts/` but gitignored —
they're bulky and fully reconstructable from `ingest`; what's committed is the
distilled key points and synthesized profile, which is the actual reusable
index.

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q
```

Every pipeline stage is tested offline against a fake LLM client
(`tests/fakes.py`) — no API key or network access required.
