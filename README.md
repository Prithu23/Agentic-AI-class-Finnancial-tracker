# Agentic AI — Financial Tracker

Four small, independent AI agents behind one Flask backend and a local LLM
(served through [Ollama](https://ollama.com) — nothing leaves your machine):

| App | URL | What it does |
|---|---|---|
| **Weather App** | `/weather` (also `/`) | Chat naturally about a city/day/activity; pulls a live forecast and gives a rule-based go/no-go for running, cycling, and hiking |
| **Savings Advisor** | `/advisor` | Chat intake for an ongoing monthly savings goal; hands off to a generated savings plan |
| **Travel Budget** | `/travel` | Chat intake for a trip; pulls a live exchange rate and computes a per-night hotel budget + accommodation tier suggestions |
| **Finance Planner** | `/planner` | Chat intake for a one-time lump sum; generates and scores several allocation strategies (emergency fund / investments / skill development), then recommends the best fit |

Each app is a standalone page — no shared navigation state, no auto-routing
between them. Every page links to the other three, but always in a new
browser tab (`target="_blank"`), so they stay genuinely independent.

## The core idea: chat intake → deterministic calculation

All four agents follow the same pattern, and it's worth understanding once:

1. **You type in plain English.** No forms (except a couple of currency
   codes). "Is it good weather for a run in Chennai today?" or "I have
   ₹1,00,000, medium risk, want an emergency fund and to learn data science,
   but need ₹20,000 accessible within 3 months" — all in one message if you
   want.
2. **The LLM's only job is intake** — extracting structured fields (city,
   income, currencies, risk preference, ...) from your message and asking a
   follow-up for whatever's still missing. It never does the actual
   math/decision-making itself.
3. **Once the required fields are known, Flask runs the real logic in plain
   Python** — a weather-threshold check, a currency calculator, a savings
   formula, or a strategy scorer — and only *that* result is shown as the
   answer. The LLM is never trusted to compute numbers.
4. **Completion is decided by the backend, not the model.** Small local
   models are unreliable at self-reporting "I have everything I need" even
   when told exactly what that means — so every chat endpoint ignores the
   model's own `complete` flag and checks the actual collected fields itself
   before running anything.
5. **Fields only ever get filled in, never erased.** Each turn merges newly
   extracted fields into what's already known, so if the model forgets to
   re-state something it got right two turns ago, that answer isn't lost and
   you're never asked to repeat yourself from scratch.

## Setup

1. **Install and start [Ollama](https://ollama.com)**, then pull a model:
   ```bash
   ollama pull llama3.1:8b
   ollama serve      # usually already running as a background service
   ```
   `llama3.1:8b` is the default (see `OLLAMA_MODEL` below) because it's
   noticeably more reliable at multi-field extraction than smaller models —
   but it's also slower (tens of seconds per reply on CPU-only setups). If
   you'd rather trade accuracy for speed, a 1-2B model (e.g.
   `qwen2.5-coder:1.5b`) responds in a couple of seconds but will more often
   need a second follow-up turn to catch every field.

2. **Install dependencies and run** — one command, one port; Flask serves
   the API and every frontend page together:
   ```bash
   pip install -r requirements.txt
   python app.py     # serves everything on http://localhost:5000
   ```
   Then open `http://localhost:5000/weather`, `/advisor`, `/travel`, or
   `/planner`.

   Optional environment variables:
   - `OLLAMA_URL` (default `http://localhost:11434`)
   - `OLLAMA_MODEL` (default `llama3.1:8b`)

   > Open the page through Flask (`http://localhost:5000/...`), not through
   > a separate static-file server like a VS Code Live Server extension —
   > the frontend calls `/api/...` as a relative path, so it only reaches
   > the backend when Flask is the one serving the page.

## What's in here

```
app.py                    Flask backend — every route and agent lives here
weather.html / .js        Weather App frontend
advisor.html / .js        Savings Advisor frontend
travel.html / .js         Travel Budget frontend
planner.html / .js        Finance Planner frontend
style.css                 Shared styling (one color theme per app)
requirements.txt          Flask, flask-cors, requests
```

There's no build step and no framework — every page is plain HTML/CSS/JS,
talking to Flask over `fetch()`.

## The agents in detail

### Weather App
`POST /api/weather/chat/start` then `POST /api/weather/chat`. Extracts
`location` (required), `date`, and `activity_focus` from conversation, then
geocodes the city (Open-Meteo), pulls that day's forecast, and checks it
against fixed thresholds (`ACTIVITY_THRESHOLDS` in `app.py`) for running,
cycling, and hiking. The LLM only writes a one-line friendly summary on top
of the already-decided verdicts — it can't contradict them. Today's real
date is injected into the prompt every turn so relative dates ("this
Saturday") resolve correctly instead of the model guessing from its training
data.

### Savings Advisor
`POST /api/chat/start` then `POST /api/chat` (plus the older non-chat
`POST /api/plan` and `POST /api/compare` if you want to call it directly with
a JSON body instead of chatting). Collects monthly income/expenses, current
savings, a goal (name/amount/timeframe), and risk tolerance, then generates
a monthly savings plan with an emergency/goal/investment allocation.

### Travel Budget
`POST /api/travel/chat/start` then `POST /api/travel/chat` (plus the direct
`POST /api/travel-budget`). Collects home/destination currency, trip length,
daily spend, and either a total budget or a nightly hotel budget. Pulls a
live exchange rate (Frankfurter), runs a pure-Python calculator to work out
what's left per night for a hotel, then asks the model for 2-3
accommodation *tiers* that fit that figure — never a specific invented hotel
name, since there's no free hotel-search API to back that up.

### Finance Planner
`POST /api/planner/chat/start` then `POST /api/planner/chat`. This one's a
small Tree-of-Thought: once it knows the amount and your risk preference
(plus any goals/constraints you mentioned), it asks the model for **4
distinct allocation strategies** (e.g. Conservative, Balanced,
Growth-oriented, Skill-focused) split across emergency savings / investments
/ skill development. Each strategy is then scored two ways:
- **Deterministically** (liquidity, risk, and expected return come from
  fixed weight tables in `app.py`, never from the model's own arithmetic)
- **By the model** (a `fit_score` judging how well that strategy actually
  satisfies your stated goals/constraints)

Strategies that don't suit your risk preference or score poorly on fit are
**pruned**. If pruning would eliminate every strategy, the planner
**backtracks** and keeps the least-bad one rather than returning nothing.
The response shows every strategy — kept and pruned, with reasons — plus the
one final recommendation.

## Extending it

- **Persistence**: `CHAT_SESSIONS` is an in-memory dict — fine for local,
  single-user dev, but swap it for Redis/SQLite before deploying multi-user.
- **Model choice**: any Ollama model works via `OLLAMA_MODEL`. Smaller
  models need the completion checks and field-merging this app already does
  server-side (see "The core idea" above) even more than large ones do.
- **Finance Planner extensions the spec leaves optional**: a real public
  finance API for live investment return assumptions, cross-session
  preference memory, and a reflection/self-correction pass before
  presenting the final recommendation are all natural next steps that
  aren't implemented yet.
