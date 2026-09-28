const API_BASE = "/api"; // Flask serves this frontend too, so same-origin relative paths work

/* ============================================================
   Weather App — chat intake, hands off to the weather+activity check
   ============================================================ */
let weatherSessionId = null;

function appendChatMsg(role, text) {
  const body = document.getElementById("weather-body");
  const div = document.createElement("div");
  div.className = `chat-msg ${role}`;
  div.textContent = text;
  body.appendChild(div);
  body.scrollTop = body.scrollHeight;
}

function appendWeatherResult(data) {
  const body = document.getElementById("weather-body");
  const c = data.conditions;
  const activityLabels = { running: "Running", cycling: "Cycling", hiking: "Hiking" };

  const chips = Object.entries(data.activities)
    .map(([key, verdict]) => {
      const cls = verdict.suitable ? "suitable" : "unsuitable";
      const reason = verdict.reasons.length ? `<span class="reasons">${verdict.reasons.join(", ")}</span>` : "";
      return `<div class="activity-chip ${cls}"><span class="dot"></span><span>${activityLabels[key]}${reason}</span></div>`;
    })
    .join("");

  const div = document.createElement("div");
  div.className = "plan-block";
  div.innerHTML = `
    ${data.summary ? `<p class="weather-summary">${data.summary}</p>` : ""}
    <div class="weather-figures">
      <div class="weather-figure"><span class="value">${c.temperature_min}–${c.temperature_max}°C</span><span class="label">${data.location.name}, ${c.date}</span></div>
      <div class="weather-figure"><span class="value">${c.precipitation_probability_max}%</span><span class="label">Chance of rain</span></div>
      <div class="weather-figure"><span class="value">${c.windspeed_max} km/h</span><span class="label">Wind</span></div>
    </div>
    <div class="activity-row">${chips}</div>
  `;
  body.appendChild(div);
  body.scrollTop = body.scrollHeight;
}

async function startWeatherChat() {
  const res = await fetch(`${API_BASE}/weather/chat/start`, { method: "POST" });
  const data = await res.json();
  weatherSessionId = data.session_id;
  appendChatMsg("assistant", data.reply);
}

document.getElementById("weather-search").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = e.target.querySelector("input");
  const message = input.value.trim();
  if (!message || !weatherSessionId) return;

  appendChatMsg("user", message);
  input.value = "";

  try {
    const res = await fetch(`${API_BASE}/weather/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: weatherSessionId, message }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error);
    appendChatMsg("assistant", data.reply);
    if (data.complete && data.weather) appendWeatherResult(data.weather);
    else if (data.complete && data.weather_error) appendChatMsg("assistant", `⚠ ${data.weather_error}`);
  } catch (err) {
    appendChatMsg("assistant", `⚠ ${err.message}`);
  }
});

startWeatherChat();
