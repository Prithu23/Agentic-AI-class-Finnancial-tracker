"""
Personal Savings Advisor — backend
-----------------------------------
Flask API that:
  1. POST /api/plan             -> generate one savings plan from a set of inputs
  2. POST /api/compare          -> generate + line up plans for several input sets
  3. POST /api/chat             -> a SEPARATE conversational bot that gathers plan
                                    inputs from the user turn by turn (does not give
                                    financial advice itself)
  4. POST /api/weather-activity -> pulls live weather for a location and decides,
                                    with plain rule-based thresholds, whether running,
                                    cycling, and hiking are a good idea
  5. POST /api/travel-budget    -> pulls a live exchange rate, runs a currency +
                                    day-count calculator, and suggests an
                                    accommodation tier that fits the leftover budget
  6. POST /api/planner/chat     -> one-time lump-sum allocator: generates several
                                    named strategies (emergency/investment/skill
                                    split), scores each on liquidity/risk/return/fit,
                                    prunes the ones that don't suit the stated risk
                                    preference or constraints, and backtracks to the
                                    least-bad strategy if pruning would leave none

All AI generation goes through a local Ollama server (default
http://localhost:11434). No data leaves the machine Ollama runs on.
Weather comes from Open-Meteo and exchange rates from Frankfurter — both are
free, keyless public APIs, so nothing else to sign up for.

Run:
    pip install -r requirements.txt
    ollama pull llama3          # or whichever model you set OLLAMA_MODEL to
    python app.py
"""

import datetime
import json
import os
import re
import uuid

import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # harmless to keep even when served same-origin; useful if you ever split frontend/backend again

FRONTEND_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_ASSETS = {"style.css", "weather.js", "advisor.js", "travel.js", "planner.js"}


@app.route("/")
@app.route("/weather")
def serve_weather_app():
    return send_from_directory(FRONTEND_DIR, "weather.html")


@app.route("/advisor")
def serve_advisor_app():
    return send_from_directory(FRONTEND_DIR, "advisor.html")


@app.route("/travel")
def serve_travel_app():
    return send_from_directory(FRONTEND_DIR, "travel.html")


@app.route("/planner")
def serve_planner_app():
    return send_from_directory(FRONTEND_DIR, "planner.html")


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/<path:filename>")
def serve_frontend_asset(filename):
    # Only ever serves the known frontend files (style.css, weather.js,
    # advisor.js, travel.js) — never app.py or anything else sitting in this folder.
    if filename in FRONTEND_ASSETS:
        return send_from_directory(FRONTEND_DIR, filename)
    return jsonify({"ok": False, "error": "Not found"}), 404

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")

REQUIRED_FIELDS = [
    "monthly_income",
    "monthly_expenses",
    "current_savings",
    "goal_name",
    "goal_amount",
    "goal_timeframe_months",
    "risk_tolerance",
]

# In-memory store for chat sessions: { session_id: {"history": [...], "collected": {...}} }
# Fine for a single-user dev app. Swap for a real DB/session store before deploying.
CHAT_SESSIONS = {}

# Free, keyless public APIs used by the weather and currency agents.
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
EXCHANGE_RATE_URL = "https://api.frankfurter.dev/v1/latest"

# Rule-based go/no-go thresholds per activity. Tune these freely — they are
# the actual decision logic; the LLM only ever writes a one-line summary on
# top of whatever these rules already decided.
ACTIVITY_THRESHOLDS = {
    "running": {"min_temp": 5, "max_temp": 32, "max_precip_prob": 60, "max_precip_mm": 4, "max_wind_kmh": 35},
    "cycling": {"min_temp": 5, "max_temp": 34, "max_precip_prob": 50, "max_precip_mm": 3, "max_wind_kmh": 30},
    "hiking": {"min_temp": 0, "max_temp": 30, "max_precip_prob": 70, "max_precip_mm": 6, "max_wind_kmh": 40},
}


# --------------------------------------------------------------------------- #
# Ollama helper
# --------------------------------------------------------------------------- #
def call_ollama(prompt, system=None, json_mode=True, temperature=0.4, timeout=120):
    """Call the local Ollama /api/generate endpoint and return the raw text response."""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if system:
        payload["system"] = system
    if json_mode:
        payload["format"] = "json"

    try:
        resp = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=timeout)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_URL}. Is 'ollama serve' running, "
            f"and have you pulled the '{OLLAMA_MODEL}' model?"
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Ollama request failed: {exc}")

    return resp.json().get("response", "")


def extract_json(text):
    """Best-effort extraction of a JSON object from a model response."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Model did not return valid JSON:\n{text}")


def run_chat_turn(session, system_prompt):
    """
    Shared intake-chat turn: send the conversation + fields collected so far to
    Ollama, let it update the fields and decide whether enough has been
    gathered, and record its reply in the session history. Used by every
    prompt-based agent (advisor, weather, travel) so they all follow the same
    "ask a follow-up until complete" pattern.
    """
    convo_text = "\n".join(f"{turn['role']}: {turn['content']}" for turn in session["history"])
    prompt = (
        f"Fields collected so far (JSON): {json.dumps(session['collected'])}\n\n"
        f"Conversation so far:\n{convo_text}\n\n"
        "Update the collected fields based on the latest user message and respond "
        "with the JSON object described in your instructions."
    )
    # Low temperature: this call is a structured-extraction task, not creative
    # writing — determinism matters far more than variety, and the small local
    # models this app runs on drift more at higher temperatures.
    raw = call_ollama(prompt, system=system_prompt, json_mode=True, temperature=0.15)
    parsed = extract_json(raw)

    reply = parsed.get("reply", "Could you say a bit more?")
    complete = bool(parsed.get("complete", False))

    # Merge rather than overwrite: small models sometimes drop a field they
    # extracted correctly in an earlier turn when composing this turn's JSON.
    # Blindly trusting their output would let an already-answered field
    # silently revert to null, making the bot re-ask questions it already got
    # answers to. A field only ever moves from unknown -> known (or gets
    # explicitly replaced with a new non-empty value), never back to unknown.
    new_collected = parsed.get("collected") or {}
    collected = dict(session["collected"])
    for key, value in new_collected.items():
        if value not in (None, ""):
            collected[key] = value

    session["collected"] = collected
    session["history"].append({"role": "assistant", "content": reply})
    return reply, collected, complete


# --------------------------------------------------------------------------- #
# Savings plan generation
# --------------------------------------------------------------------------- #
PLAN_SYSTEM_PROMPT = """You are a careful, conservative personal savings advisor.
Given a person's finances and one goal, produce a realistic monthly savings plan.
Always respond with ONLY a single JSON object, no prose outside it, matching this shape:

{
  "monthly_recommended_savings": <number>,
  "months_to_goal": <number>,
  "allocation": {
    "emergency_fund_pct": <number 0-100>,
    "goal_fund_pct": <number 0-100>,
    "investment_pct": <number 0-100>
  },
  "investment_suggestions": [<string>, ...],
  "risk_notes": "<one short paragraph>",
  "summary": "<2-3 sentence plain-language summary of the plan>"
}

Rules:
- allocation percentages must sum to 100.
- monthly_recommended_savings must never exceed (monthly_income - monthly_expenses).
- Be conservative and specific to the numbers given. Do not invent extra fields.
"""


def build_plan_prompt(inputs):
    return (
        "Generate a savings plan for this person:\n"
        f"- Monthly income: {inputs.get('monthly_income')}\n"
        f"- Monthly expenses: {inputs.get('monthly_expenses')}\n"
        f"- Current savings: {inputs.get('current_savings')}\n"
        f"- Goal: {inputs.get('goal_name')}\n"
        f"- Goal amount: {inputs.get('goal_amount')}\n"
        f"- Desired timeframe (months): {inputs.get('goal_timeframe_months')}\n"
        f"- Risk tolerance: {inputs.get('risk_tolerance')}\n"
    )


def generate_plan(inputs):
    missing = [f for f in REQUIRED_FIELDS if inputs.get(f) in (None, "")]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")

    prompt = build_plan_prompt(inputs)
    raw = call_ollama(prompt, system=PLAN_SYSTEM_PROMPT, json_mode=True)
    plan = extract_json(raw)
    plan["inputs"] = inputs
    return plan


@app.route("/api/plan", methods=["POST"])
def api_plan():
    inputs = request.get_json(force=True) or {}
    try:
        plan = generate_plan(inputs)
        return jsonify({"ok": True, "plan": plan})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/api/compare", methods=["POST"])
def api_compare():
    """
    Body: { "scenarios": [ {label: "...", ...inputs}, {label: "...", ...inputs}, ... ] }
    Returns a plan per scenario so the frontend can render them side by side.
    """
    body = request.get_json(force=True) or {}
    scenarios = body.get("scenarios", [])
    if not scenarios or len(scenarios) < 2:
        return jsonify({"ok": False, "error": "Provide at least 2 scenarios to compare."}), 400

    results = []
    for i, scenario in enumerate(scenarios):
        label = scenario.get("label") or f"Scenario {i + 1}"
        inputs = {k: v for k, v in scenario.items() if k != "label"}
        try:
            plan = generate_plan(inputs)
            results.append({"label": label, "ok": True, "plan": plan})
        except (ValueError, RuntimeError) as e:
            results.append({"label": label, "ok": False, "error": str(e)})

    return jsonify({"ok": True, "results": results})


# --------------------------------------------------------------------------- #
# Separate intake chatbot — ONLY gathers inputs, never advises
# --------------------------------------------------------------------------- #
CHAT_SYSTEM_PROMPT = f"""You are a friendly intake assistant for a savings-plan tool.
Your ONLY job is to collect these fields from the user through natural,
conversational back-and-forth:
{', '.join(REQUIRED_FIELDS)}
(risk_tolerance must end up as one of: "low", "medium", "high")

IMPORTANT: the user's latest message may contain several of these fields at
once, even ones you didn't just ask about — for example a single message
might give income, expenses, AND a savings goal together. Read the WHOLE
message carefully and pull out every field you can find in it, not just the
one you most recently asked about. Never ask again for a field the user has
already given a real value for anywhere in the conversation.

Example — if the user says:
"I make 60000 a month, spend 35000, and want to save 150000 for a laptop in 6 months"
you must extract ALL of: monthly_income=60000, monthly_expenses=35000,
goal_name="laptop", goal_amount=150000, goal_timeframe_months=6 — all from that
one message — then ask only about whatever is still missing (here: current_savings,
risk_tolerance).

Do NOT give financial advice, opinions, or a savings plan yourself — a separate
tool does that once intake is complete. Just be warm and efficient. Ask about
one or two still-missing fields at a time.

You will be given the fields already collected so far as JSON. Ask only about
what's still missing. Once every field has a real value, set "complete": true.
Your "reply" text must be consistent with "collected" — never ask about a
field that already has a non-null value in "collected".

Respond with ONLY a JSON object of this exact shape, no other text:
{{
  "reply": "<what to say to the user next>",
  "collected": {{ ...every field above, null if still unknown... }},
  "complete": <true or false>
}}
"""


@app.route("/api/chat/start", methods=["POST"])
def chat_start():
    session_id = str(uuid.uuid4())
    CHAT_SESSIONS[session_id] = {"history": [], "collected": {f: None for f in REQUIRED_FIELDS}}
    greeting = (
        "Hi! I'll ask a few quick questions so we can build your savings plan. "
        "First up — what's your monthly income, and roughly what are your monthly expenses?"
    )
    CHAT_SESSIONS[session_id]["history"].append({"role": "assistant", "content": greeting})
    return jsonify({"ok": True, "session_id": session_id, "reply": greeting, "complete": False})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    body = request.get_json(force=True) or {}
    session_id = body.get("session_id")
    user_message = (body.get("message") or "").strip()

    if not session_id or session_id not in CHAT_SESSIONS:
        return jsonify({"ok": False, "error": "Unknown or missing session_id. Call /api/chat/start first."}), 400
    if not user_message:
        return jsonify({"ok": False, "error": "message is required"}), 400

    session = CHAT_SESSIONS[session_id]
    session["history"].append({"role": "user", "content": user_message})

    try:
        reply, collected, _ = run_chat_turn(session, CHAT_SYSTEM_PROMPT)
    except (RuntimeError, ValueError) as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    # Don't trust the model's own "complete" flag — models (especially small
    # local ones) are inconsistent about setting it even when told exactly
    # when to. We already know precisely what "complete" means here, so check
    # it ourselves instead of hoping the model agrees.
    complete = all(collected.get(f) not in (None, "") for f in REQUIRED_FIELDS)
    response = {"ok": True, "reply": reply, "collected": collected, "complete": complete}

    if complete:
        try:
            plan = generate_plan(collected)
            response["plan"] = plan
        except (ValueError, RuntimeError) as e:
            response["plan_error"] = str(e)

    return jsonify(response)


# --------------------------------------------------------------------------- #
# Weather-based activity agent
# --------------------------------------------------------------------------- #
def geocode_location(location):
    resp = requests.get(GEOCODE_URL, params={"name": location, "count": 1}, timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results")
    if not results:
        raise ValueError(f"Could not find a location matching '{location}'")
    r = results[0]
    return {
        "name": r["name"],
        "country": r.get("country"),
        "latitude": r["latitude"],
        "longitude": r["longitude"],
    }


def fetch_daily_weather(latitude, longitude, date=None):
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,"
                 "precipitation_probability_max,windspeed_10m_max,weathercode",
        "timezone": "auto",
    }
    resp = requests.get(FORECAST_URL, params=params, timeout=15)
    resp.raise_for_status()
    daily = resp.json().get("daily", {})
    dates = daily.get("time", [])
    if not dates:
        raise ValueError("Weather service returned no forecast data for this location.")

    idx = 0
    if date:
        if date not in dates:
            raise ValueError(f"No forecast for {date}. Available range: {dates[0]} to {dates[-1]}.")
        idx = dates.index(date)

    return {
        "date": dates[idx],
        "temperature_max": daily["temperature_2m_max"][idx],
        "temperature_min": daily["temperature_2m_min"][idx],
        "precipitation_sum": daily["precipitation_sum"][idx],
        "precipitation_probability_max": daily["precipitation_probability_max"][idx],
        "windspeed_max": daily["windspeed_10m_max"][idx],
    }


def evaluate_activity(thresholds, c):
    reasons = []
    if c["temperature_max"] > thresholds["max_temp"]:
        reasons.append(f"too hot ({c['temperature_max']}°C > {thresholds['max_temp']}°C)")
    if c["temperature_min"] < thresholds["min_temp"]:
        reasons.append(f"too cold ({c['temperature_min']}°C < {thresholds['min_temp']}°C)")
    if c["precipitation_probability_max"] > thresholds["max_precip_prob"]:
        reasons.append(f"high chance of rain ({c['precipitation_probability_max']}%)")
    if c["precipitation_sum"] > thresholds["max_precip_mm"]:
        reasons.append(f"too much rain expected ({c['precipitation_sum']}mm)")
    if c["windspeed_max"] > thresholds["max_wind_kmh"]:
        reasons.append(f"too windy ({c['windspeed_max']} km/h)")
    return {"suitable": len(reasons) == 0, "reasons": reasons}


def build_weather_result(location, date=None):
    place = geocode_location(location)
    conditions = fetch_daily_weather(place["latitude"], place["longitude"], date)
    activities = {name: evaluate_activity(t, conditions) for name, t in ACTIVITY_THRESHOLDS.items()}
    result = {"location": place, "conditions": conditions, "activities": activities}

    # A short natural-language wrap-up on top of the rule-based verdicts above.
    # Purely cosmetic — if Ollama is down, the verdicts above are already complete
    # and correct, so this failure is swallowed rather than surfaced as an error.
    try:
        prompt = (
            f"Weather in {place['name']} on {conditions['date']}: "
            f"{conditions['temperature_min']}-{conditions['temperature_max']}°C, "
            f"{conditions['precipitation_sum']}mm rain ({conditions['precipitation_probability_max']}% chance), "
            f"wind up to {conditions['windspeed_max']} km/h.\n"
            f"Verdicts already decided: {json.dumps({k: v['suitable'] for k, v in activities.items()})}\n"
            "Write ONE short, friendly sentence summarizing the day for someone deciding "
            "whether to go outside. Do not contradict the verdicts given."
        )
        result["summary"] = call_ollama(prompt, json_mode=False, temperature=0.5).strip()
    except RuntimeError:
        pass

    return result


@app.route("/api/weather-activity", methods=["POST"])
def api_weather_activity():
    """
    Body: { "location": "Bengaluru", "date": "2026-09-25" }   (date optional, defaults to today)
    """
    body = request.get_json(force=True) or {}
    location = body.get("location")
    date = body.get("date")
    if not location:
        return jsonify({"ok": False, "error": "location is required"}), 400

    try:
        result = build_weather_result(location, date)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except requests.exceptions.RequestException as e:
        return jsonify({"ok": False, "error": f"Weather service error: {e}"}), 502

    return jsonify({"ok": True, **result})


# --------------------------------------------------------------------------- #
# Weather chat — prompt-based intake, same "ask a follow-up" pattern as the
# savings advisor. Only a location is strictly required; date/activity are
# nice-to-haves the model can leave null.
# --------------------------------------------------------------------------- #
def weather_chat_system_prompt():
    today = datetime.date.today()
    return f"""You are an intake assistant in front of a weather-and-activity tool.
Today's date is {today.isoformat()} ({today.strftime('%A')}).

Your ONLY job is to figure out these three fields from natural conversation:
- location: a city name. REQUIRED — ask a follow-up if you don't have one yet.
- date: a specific date as YYYY-MM-DD if the user mentions one (e.g. "tomorrow",
  "this Saturday" — resolve it relative to today's date given above); leave null
  if unspecified (meaning "today").
- activity_focus: one of "running", "cycling", "hiking" if the user asked about
  a specific one; otherwise null (meaning "check all three").

You do NOT have live weather data and must NEVER ask the user for the
temperature, rain chance, wind, or any other weather condition — a separate
tool looks up the real forecast automatically, right after you, the instant
you have a location. Your only questions to the user are about location, date,
or activity focus — nothing else.

Never invent a location. Never guess a date from anything other than today's
actual date given above — do not fall back on a year or date from your
training data. Only ask about what's still missing — do not ask about date or
activity_focus if the user hasn't brought them up and a location is already
known.

IMPORTANT — what "complete" means here: it does NOT mean you have answered the
user's question or told them the weather. It ONLY means "I have a location
value in collected." As soon as collected.location is a real city name
(non-null), you MUST set "complete": true, even if date and activity_focus are
still null and even though you have not said anything about actual weather
conditions yet.

Respond with ONLY a JSON object of this exact shape, no other text:
{{
  "reply": "<what to say to the user next>",
  "collected": {{ "location": <string or null>, "date": <string or null>, "activity_focus": <string or null> }},
  "complete": <true or false>
}}
"""


@app.route("/api/weather/chat/start", methods=["POST"])
def weather_chat_start():
    session_id = str(uuid.uuid4())
    CHAT_SESSIONS[session_id] = {
        "history": [],
        "collected": {"location": None, "date": None, "activity_focus": None},
    }
    greeting = (
        "Hey! Where are you, and is there a day or an activity — running, cycling, "
        "hiking — you're thinking about?"
    )
    CHAT_SESSIONS[session_id]["history"].append({"role": "assistant", "content": greeting})
    return jsonify({"ok": True, "session_id": session_id, "reply": greeting, "complete": False})


@app.route("/api/weather/chat", methods=["POST"])
def weather_chat():
    body = request.get_json(force=True) or {}
    session_id = body.get("session_id")
    user_message = (body.get("message") or "").strip()

    if not session_id or session_id not in CHAT_SESSIONS:
        return jsonify({"ok": False, "error": "Unknown or missing session_id. Call /api/weather/chat/start first."}), 400
    if not user_message:
        return jsonify({"ok": False, "error": "message is required"}), 400

    session = CHAT_SESSIONS[session_id]
    session["history"].append({"role": "user", "content": user_message})

    try:
        reply, collected, _ = run_chat_turn(session, weather_chat_system_prompt())
    except (RuntimeError, ValueError) as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    # Don't trust the model's own "complete" flag — see the note in api_chat.
    complete = bool(collected.get("location"))
    response = {"ok": True, "reply": reply, "collected": collected, "complete": complete}

    if complete:
        try:
            response["weather"] = build_weather_result(collected["location"], collected.get("date"))
        except (ValueError, RuntimeError, requests.exceptions.RequestException) as e:
            response["weather_error"] = str(e)

    return jsonify(response)


# --------------------------------------------------------------------------- #
# Currency & travel budget agent
# --------------------------------------------------------------------------- #
def get_exchange_rate(base, target):
    resp = requests.get(EXCHANGE_RATE_URL, params={"from": base, "to": target}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    rate = data.get("rates", {}).get(target)
    if rate is None:
        raise ValueError(f"Could not get an exchange rate from {base} to {target}. Check the currency codes.")
    return {"date": data.get("date"), "base": base, "target": target, "rate": rate}


def compute_travel_budget(inputs, rate_info):
    """
    Pure calculator — no AI involved. Converts everything using the live rate
    and, if a total budget is given, backs out how much is left per night for
    a hotel; otherwise it totals up a plan from a per-night hotel guess.
    """
    days = int(inputs["num_days"])
    if days <= 0:
        raise ValueError("num_days must be at least 1")
    rate = rate_info["rate"]  # 1 unit of home_currency = `rate` units of destination_currency

    transport = inputs.get("transport", {}) or {}
    transport_cost_home = float(transport.get("cost_home_currency", 0) or 0)
    if transport.get("round_trip") is False:
        transport_cost_home *= 2  # a one-way fare was given; double it for the return leg

    daily_spend_dest = float(inputs.get("daily_spend_destination_currency", 0) or 0)
    daily_spend_home = daily_spend_dest / rate if rate else 0
    daily_spend_total_home = daily_spend_home * days

    total_budget_home = inputs.get("total_budget_home_currency")
    if total_budget_home is not None:
        total_budget_home = float(total_budget_home)
        remaining_home = total_budget_home - transport_cost_home - daily_spend_total_home
        hotel_budget_per_night_home = remaining_home / days
        within_budget = hotel_budget_per_night_home > 0
    else:
        remaining_home = None
        within_budget = None
        given_hotel_dest = float(inputs.get("hotel_budget_per_night_destination_currency", 0) or 0)
        hotel_budget_per_night_home = given_hotel_dest / rate if rate else 0

    hotel_budget_per_night_dest = hotel_budget_per_night_home * rate
    total_cost_home = transport_cost_home + daily_spend_total_home + (hotel_budget_per_night_home * days)

    return {
        "transport": {
            "mode": transport.get("mode"),
            "cost_home_currency": round(transport_cost_home, 2),
            "cost_destination_currency": round(transport_cost_home * rate, 2),
        },
        "daily_spend": {
            "home_currency_per_day": round(daily_spend_home, 2),
            "destination_currency_per_day": round(daily_spend_dest, 2),
        },
        "hotel_budget_per_night": {
            "home_currency": round(hotel_budget_per_night_home, 2),
            "destination_currency": round(hotel_budget_per_night_dest, 2),
        },
        "totals": {
            "num_days": days,
            "total_cost_home_currency": round(total_cost_home, 2),
            "total_cost_destination_currency": round(total_cost_home * rate, 2),
            "total_budget_home_currency": total_budget_home,
            "within_budget": within_budget,
        },
    }


HOTEL_SYSTEM_PROMPT = """You suggest accommodation TYPES, never specific named
hotels (you have no live inventory, so a made-up name would mislead someone
booking a real trip). Given a nightly budget, respond with ONLY this JSON:
{ "tiers": [<string>, ...], "tip": "<one practical booking sentence>" }
tiers should be 2-3 realistic options for that budget (e.g. "hostel dorm bed",
"budget 2-star hotel", "mid-range 3-star hotel", "boutique/4-star").
"""


def suggest_hotel_tiers(budget_per_night_dest, destination_currency, destination_hint=None):
    prompt = (
        f"Nightly accommodation budget: {budget_per_night_dest:.2f} {destination_currency}"
        + (f", destination: {destination_hint}" if destination_hint else "")
        + "."
    )
    raw = call_ollama(prompt, system=HOTEL_SYSTEM_PROMPT, json_mode=True, temperature=0.5)
    return extract_json(raw)


def build_travel_result(inputs):
    home = str(inputs["home_currency"]).upper()
    dest = str(inputs["destination_currency"]).upper()

    rate_info = get_exchange_rate(home, dest)
    budget = compute_travel_budget(inputs, rate_info)

    result = {"exchange_rate": rate_info, "budget": budget}
    try:
        result["hotel_suggestions"] = suggest_hotel_tiers(
            budget["hotel_budget_per_night"]["destination_currency"],
            dest,
            inputs.get("destination_city"),
        )
    except (RuntimeError, ValueError):
        pass  # nice-to-have; the numeric budget above is already complete without it

    return result


@app.route("/api/travel-budget", methods=["POST"])
def api_travel_budget():
    """
    Body:
    {
      "home_currency": "INR", "destination_currency": "USD",
      "num_days": 5,
      "transport": {"mode": "flight", "cost_home_currency": 45000, "round_trip": true},
      "daily_spend_destination_currency": 80,
      "total_budget_home_currency": 150000,          // optional — omit to use hotel_budget_per_night_destination_currency instead
      "hotel_budget_per_night_destination_currency": 60,  // used only if total_budget_home_currency is omitted
      "destination_city": "Bangkok"                  // optional, just for the hotel-tier suggestion
    }
    """
    body = request.get_json(force=True) or {}
    missing = [f for f in ("home_currency", "destination_currency", "num_days") if not body.get(f)]
    if missing:
        return jsonify({"ok": False, "error": f"Missing required fields: {', '.join(missing)}"}), 400

    try:
        result = build_travel_result(body)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except requests.exceptions.RequestException as e:
        return jsonify({"ok": False, "error": f"Exchange rate service error: {e}"}), 502
    except (KeyError, TypeError) as e:
        return jsonify({"ok": False, "error": f"Invalid input: {e}"}), 400

    return jsonify({"ok": True, **result})


# --------------------------------------------------------------------------- #
# Travel budget chat — prompt-based intake, same "ask a follow-up" pattern as
# the savings advisor. Complete once currencies, trip length, daily spend, and
# EITHER a total budget OR a per-night hotel budget are known.
# --------------------------------------------------------------------------- #
TRAVEL_CHAT_SYSTEM_PROMPT = """You are a friendly travel-budget assistant. From
natural conversation, figure out these fields:
- home_currency: 3-letter code, e.g. "INR" (the traveler's own currency)
- destination_currency: 3-letter code, e.g. "USD"
- destination_city: city name if mentioned, else null
- num_days: number of days of the trip
- transport_mode: e.g. "flight", "train", else null
- transport_cost_home_currency: transport cost in home_currency, else null
- round_trip: true/false if the transport cost mentioned is round-trip (assume
  true unless the user says one-way), else null
- daily_spend_destination_currency: rough daily spending money in destination_currency
- total_budget_home_currency: total trip budget in home_currency, IF the user
  gives one
- hotel_budget_per_night_destination_currency: nightly hotel budget in
  destination_currency, ONLY ask for this if the user has NOT given a
  total_budget_home_currency

Ask about only what's still missing, one or two things at a time. Do not ask
for both total_budget_home_currency and hotel_budget_per_night_destination_currency
— once you have either one, stop asking about budget.

IMPORTANT — what "complete" means here: it does NOT mean you have finished the
conversation or calculated anything — a separate tool does the actual budget
math after you, using whatever you have in "collected". It ONLY means: do you
have real (non-null) values for home_currency, destination_currency, num_days,
daily_spend_destination_currency, AND at least one of
(total_budget_home_currency, hotel_budget_per_night_destination_currency)?
The moment all of those are non-null, you MUST set "complete": true — even if
transport_mode, transport_cost_home_currency, round_trip, or destination_city
are still null. Those four fields never block completion.

Respond with ONLY a JSON object of this exact shape, no other text:
{
  "reply": "<what to say to the user next>",
  "collected": {
    "home_currency": <string or null>, "destination_currency": <string or null>,
    "destination_city": <string or null>, "num_days": <number or null>,
    "transport_mode": <string or null>, "transport_cost_home_currency": <number or null>,
    "round_trip": <true, false, or null>, "daily_spend_destination_currency": <number or null>,
    "total_budget_home_currency": <number or null>,
    "hotel_budget_per_night_destination_currency": <number or null>
  },
  "complete": <true or false>
}
"""


def travel_chat_is_complete(collected):
    required_ok = all(
        collected.get(f) not in (None, "")
        for f in ("home_currency", "destination_currency", "num_days", "daily_spend_destination_currency")
    )
    budget_ok = collected.get("total_budget_home_currency") not in (None, "") or collected.get(
        "hotel_budget_per_night_destination_currency"
    ) not in (None, "")
    return required_ok and budget_ok


def travel_inputs_from_collected(collected):
    inputs = {
        "home_currency": collected["home_currency"],
        "destination_currency": collected["destination_currency"],
        "destination_city": collected.get("destination_city"),
        "num_days": collected["num_days"],
        "daily_spend_destination_currency": collected.get("daily_spend_destination_currency", 0),
        "transport": {
            "mode": collected.get("transport_mode"),
            "cost_home_currency": collected.get("transport_cost_home_currency", 0),
            "round_trip": collected.get("round_trip", True),
        },
    }
    if collected.get("total_budget_home_currency") not in (None, ""):
        inputs["total_budget_home_currency"] = collected["total_budget_home_currency"]
    elif collected.get("hotel_budget_per_night_destination_currency") not in (None, ""):
        inputs["hotel_budget_per_night_destination_currency"] = collected["hotel_budget_per_night_destination_currency"]
    return inputs


@app.route("/api/travel/chat/start", methods=["POST"])
def travel_chat_start():
    session_id = str(uuid.uuid4())
    CHAT_SESSIONS[session_id] = {
        "history": [],
        "collected": {
            "home_currency": None,
            "destination_currency": None,
            "destination_city": None,
            "num_days": None,
            "transport_mode": None,
            "transport_cost_home_currency": None,
            "round_trip": None,
            "daily_spend_destination_currency": None,
            "total_budget_home_currency": None,
            "hotel_budget_per_night_destination_currency": None,
        },
    }
    greeting = (
        "Hi! Tell me about the trip — where you're headed, from where, and how "
        "many days — and I'll work out a budget."
    )
    CHAT_SESSIONS[session_id]["history"].append({"role": "assistant", "content": greeting})
    return jsonify({"ok": True, "session_id": session_id, "reply": greeting, "complete": False})


@app.route("/api/travel/chat", methods=["POST"])
def travel_chat():
    body = request.get_json(force=True) or {}
    session_id = body.get("session_id")
    user_message = (body.get("message") or "").strip()

    if not session_id or session_id not in CHAT_SESSIONS:
        return jsonify({"ok": False, "error": "Unknown or missing session_id. Call /api/travel/chat/start first."}), 400
    if not user_message:
        return jsonify({"ok": False, "error": "message is required"}), 400

    session = CHAT_SESSIONS[session_id]
    session["history"].append({"role": "user", "content": user_message})

    try:
        reply, collected, _ = run_chat_turn(session, TRAVEL_CHAT_SYSTEM_PROMPT)
    except (RuntimeError, ValueError) as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    # Don't trust the model's own "complete" flag — see the note in api_chat.
    complete = travel_chat_is_complete(collected)
    response = {"ok": True, "reply": reply, "collected": collected, "complete": complete}

    if complete:
        try:
            response["travel"] = build_travel_result(travel_inputs_from_collected(collected))
        except (ValueError, RuntimeError, requests.exceptions.RequestException, KeyError, TypeError) as e:
            response["travel_error"] = str(e)

    return jsonify(response)


# --------------------------------------------------------------------------- #
# Intelligent Personal Finance Planner — a one-time lump-sum allocator, not to
# be confused with the ongoing-savings-goal chat above. Generates several
# named allocation strategies (a small Tree-of-Thought: each strategy is one
# branch), scores every branch with a deterministic calculator plus an
# LLM-judged fit against the user's stated constraints, prunes branches that
# don't suit the user's risk preference or constraints, and — if pruning would
# wipe out every branch — backtracks by keeping the least-bad one instead of
# returning nothing.
# --------------------------------------------------------------------------- #
PLANNER_FIELDS = ["total_amount", "risk_preference"]

# Deterministic "calculator tool": liquidity/risk/return are never left to the
# LLM to compute — small local models are unreliable at arithmetic, so every
# number here comes from fixed, auditable weights instead.
PLANNER_BUCKET_LIQUIDITY = {"emergency": 100, "investment": 40, "skill": 0}
PLANNER_BUCKET_RISK = {"emergency": 5, "skill": 20}
PLANNER_INVESTMENT_RISK_SCORE = {"low": 30, "medium": 55, "high": 80}
PLANNER_INVESTMENT_RETURN_PCT = {"low": 6.0, "medium": 9.0, "high": 14.0}
PLANNER_EMERGENCY_RETURN_PCT = 4.0
PLANNER_SKILL_RETURN_PCT = 0.0  # no direct monetary return; value is in the rationale/growth potential

# How much risk each stated preference tolerates before a branch gets pruned.
PLANNER_RISK_CEILING = {"low": 35, "medium": 60, "high": 100}


def planner_chat_system_prompt():
    return """You are an intake assistant for a personal-finance allocation
planner. From natural conversation, figure out:
- total_amount: a lump sum of money the user wants to allocate, as a plain
  number (e.g. "1,00,000" or "1.5 lakh" -> 150000). REQUIRED.
- risk_preference: one of "low", "medium", "high". REQUIRED — infer it from
  words like "safe"/"cautious" (low), "balanced"/"moderate" (medium), or
  "aggressive"/"growth"/"willing to take risks" (high) if the user doesn't
  say the word directly.
- goals: a short free-text summary of what the user wants the money to do
  (e.g. "build an emergency cushion and learn a new skill"), or null.
- constraints: any specific limits the user mentions (e.g. "need 20000
  accessible within 3 months", "don't want to invest in crypto"), or null.

IMPORTANT: read the user's WHOLE message, not just the part answering your
last question — a single message often states the amount, risk preference,
goals, AND constraints all at once. Extract every field you can find in it
each time, not just the one or two you most recently asked about. The
strategies you get back will only be as good as goals/constraints you
actually captured here, so don't leave a stated goal or constraint as null
just because you weren't strictly required to ask for it.

You do NOT design allocation strategies yourself — a separate tool generates
and scores those once you have total_amount and risk_preference. Only ask
about what's still missing, one or two things at a time.

IMPORTANT — what "complete" means here: it does NOT mean you've finished the
conversation. It ONLY means total_amount and risk_preference are both known
(non-null). Set "complete": true the instant both of those are known, even if
goals and constraints are still null.

Respond with ONLY a JSON object of this exact shape, no other text:
{
  "reply": "<what to say to the user next>",
  "collected": {
    "total_amount": <number or null>, "risk_preference": <string or null>,
    "goals": <string or null>, "constraints": <string or null>
  },
  "complete": <true or false>
}
"""


PLANNER_STRATEGY_SYSTEM_PROMPT = """You design allocation strategies for a
lump sum of money split across exactly three buckets: emergency savings,
investments, and skill development. You will be given the amount, the
person's risk preference, their goals, and any constraints.

Propose exactly 4 distinct named strategies — conventionally something like
"Conservative", "Balanced", "Growth-oriented", and "Skill-focused", but adapt
the mix and rationale to what the user actually said. For each strategy give:
- name: short label
- emergency_pct, investment_pct, skill_pct: numbers that sum to 100
- investment_risk_level: "low", "medium", or "high" — how risky THIS
  strategy's investment slice is (independent of the user's own stated
  risk_preference — a "Growth-oriented" strategy should use "high" here even
  for a cautious user, since scoring it against their preference happens
  elsewhere)
- rationale: one sentence on why this mix makes sense
- fit_score: 0-100, YOUR honest judgment of how well this specific strategy
  satisfies the user's stated goals and constraints (not their risk
  preference — just goals/constraints fit). Be discriminating: strategies
  that ignore a stated constraint should score low, not all score 70-90.
- fit_reasoning: one sentence explaining the fit_score

Respond with ONLY a JSON object of this exact shape, no other text:
{
  "strategies": [
    {
      "name": "<string>", "emergency_pct": <number>, "investment_pct": <number>,
      "skill_pct": <number>, "investment_risk_level": "<low|medium|high>",
      "rationale": "<string>", "fit_score": <number 0-100>, "fit_reasoning": "<string>"
    },
    ...
  ]
}
"""


def _planner_normalize_pcts(strategy):
    keys = ("emergency_pct", "investment_pct", "skill_pct")
    raw = [max(0.0, float(strategy.get(k) or 0)) for k in keys]
    total = sum(raw)
    if total <= 0:
        raw = [34.0, 33.0, 33.0]
        total = 100.0
    return {k: round(v / total * 100, 1) for k, v in zip(keys, raw)}


def _planner_score_strategy(strategy, total_amount, risk_preference):
    pcts = _planner_normalize_pcts(strategy)
    risk_level = strategy.get("investment_risk_level") if strategy.get("investment_risk_level") in PLANNER_INVESTMENT_RISK_SCORE else "medium"

    liquidity_score = (
        pcts["emergency_pct"] * PLANNER_BUCKET_LIQUIDITY["emergency"]
        + pcts["investment_pct"] * PLANNER_BUCKET_LIQUIDITY["investment"]
        + pcts["skill_pct"] * PLANNER_BUCKET_LIQUIDITY["skill"]
    ) / 100
    risk_score = (
        pcts["emergency_pct"] * PLANNER_BUCKET_RISK["emergency"]
        + pcts["investment_pct"] * PLANNER_INVESTMENT_RISK_SCORE[risk_level]
        + pcts["skill_pct"] * PLANNER_BUCKET_RISK["skill"]
    ) / 100
    expected_return_pct = (
        pcts["emergency_pct"] * PLANNER_EMERGENCY_RETURN_PCT
        + pcts["investment_pct"] * PLANNER_INVESTMENT_RETURN_PCT[risk_level]
        + pcts["skill_pct"] * PLANNER_SKILL_RETURN_PCT
    ) / 100

    risk_ceiling = PLANNER_RISK_CEILING.get(risk_preference, PLANNER_RISK_CEILING["medium"])
    mismatch_penalty = max(0.0, risk_score - risk_ceiling)
    fit_score = min(100.0, max(0.0, float(strategy.get("fit_score", 50) or 50)))
    overall_score = round(fit_score - mismatch_penalty, 1)
    pruned = mismatch_penalty > 15 or fit_score < 40

    return {
        "name": strategy.get("name") or "Unnamed strategy",
        "rationale": strategy.get("rationale", ""),
        "fit_reasoning": strategy.get("fit_reasoning", ""),
        "investment_risk_level": risk_level,
        "allocation_pct": pcts,
        "amounts": {
            "emergency": round(total_amount * pcts["emergency_pct"] / 100, 2),
            "investment": round(total_amount * pcts["investment_pct"] / 100, 2),
            "skill": round(total_amount * pcts["skill_pct"] / 100, 2),
        },
        "liquidity_score": round(liquidity_score, 1),
        "risk_score": round(risk_score, 1),
        "expected_return_pct": round(expected_return_pct, 2),
        "fit_score": fit_score,
        "risk_mismatch_penalty": round(mismatch_penalty, 1),
        "overall_score": overall_score,
        "pruned": pruned,
        "prune_reason": (
            "risk too high for a "
            f"{risk_preference}-risk preference" if mismatch_penalty > 15 else "poor fit with stated goals/constraints"
        )
        if pruned
        else None,
    }


def generate_finance_plan(collected):
    total_amount = float(collected["total_amount"])
    risk_preference = str(collected["risk_preference"]).lower()
    if risk_preference not in PLANNER_RISK_CEILING:
        risk_preference = "medium"

    prompt = (
        f"Amount to allocate: {total_amount}\n"
        f"Risk preference: {risk_preference}\n"
        f"Goals: {collected.get('goals') or 'not specified'}\n"
        f"Constraints: {collected.get('constraints') or 'none stated'}\n\n"
        "Propose the 4 strategies as instructed."
    )
    # 4 detailed strategies with several fields each is a lot more output than
    # a normal chat turn — the default timeout is tuned for short replies and
    # isn't enough here on slower/CPU-only Ollama setups.
    raw = call_ollama(prompt, system=PLANNER_STRATEGY_SYSTEM_PROMPT, json_mode=True, temperature=0.5, timeout=300)
    parsed = extract_json(raw)
    raw_strategies = parsed.get("strategies") or []
    if not raw_strategies:
        raise ValueError("Model did not propose any strategies.")

    strategies = [_planner_score_strategy(s, total_amount, risk_preference) for s in raw_strategies]

    # Prune, then backtrack if pruning wiped out every branch — keep the
    # least-bad one rather than presenting the user with nothing.
    kept = [s for s in strategies if not s["pruned"]]
    backtracked = False
    if not kept:
        backtracked = True
        best = max(strategies, key=lambda s: s["overall_score"])
        best["pruned"] = False
        best["prune_reason"] = None
        best["backtrack_note"] = (
            "Every proposed strategy exceeded your risk tolerance or fit poorly — "
            "kept this one anyway as the closest match after backtracking."
        )
        kept = [best]

    kept.sort(key=lambda s: s["overall_score"], reverse=True)
    recommended_name = kept[0]["name"]

    return {
        "total_amount": total_amount,
        "risk_preference": risk_preference,
        "goals": collected.get("goals"),
        "constraints": collected.get("constraints"),
        "strategies": strategies,
        "recommended": recommended_name,
        "backtracked": backtracked,
    }


@app.route("/api/planner/chat/start", methods=["POST"])
def planner_chat_start():
    session_id = str(uuid.uuid4())
    CHAT_SESSIONS[session_id] = {
        "history": [],
        "collected": {"total_amount": None, "risk_preference": None, "goals": None, "constraints": None},
    }
    greeting = (
        "Hi! I'll help you split a lump sum across emergency savings, investments, "
        "and skill development. How much do you want to allocate, and how would you "
        "describe your risk appetite — low, medium, or high?"
    )
    CHAT_SESSIONS[session_id]["history"].append({"role": "assistant", "content": greeting})
    return jsonify({"ok": True, "session_id": session_id, "reply": greeting, "complete": False})


@app.route("/api/planner/chat", methods=["POST"])
def planner_chat():
    body = request.get_json(force=True) or {}
    session_id = body.get("session_id")
    user_message = (body.get("message") or "").strip()

    if not session_id or session_id not in CHAT_SESSIONS:
        return jsonify({"ok": False, "error": "Unknown or missing session_id. Call /api/planner/chat/start first."}), 400
    if not user_message:
        return jsonify({"ok": False, "error": "message is required"}), 400

    session = CHAT_SESSIONS[session_id]
    session["history"].append({"role": "user", "content": user_message})

    try:
        reply, collected, _ = run_chat_turn(session, planner_chat_system_prompt())
    except (RuntimeError, ValueError) as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    # Don't trust the model's own "complete" flag — see the note in api_chat.
    complete = all(collected.get(f) not in (None, "") for f in PLANNER_FIELDS)
    response = {"ok": True, "reply": reply, "collected": collected, "complete": complete}

    if complete:
        try:
            response["plan"] = generate_finance_plan(collected)
        except (ValueError, RuntimeError, KeyError, TypeError) as e:
            response["plan_error"] = str(e)

    return jsonify(response)


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "model": OLLAMA_MODEL, "ollama_url": OLLAMA_URL})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
