const API_BASE = "/api"; // Flask serves this frontend too, so same-origin relative paths work

/* ============================================================
   Currency & Travel Budget Agent — chat intake, hands off to the calculator
   ============================================================ */
let travelSessionId = null;

function appendChatMsg(role, text) {
  const body = document.getElementById("travel-body");
  const div = document.createElement("div");
  div.className = `chat-msg ${role}`;
  div.textContent = text;
  body.appendChild(div);
  body.scrollTop = body.scrollHeight;
}

function appendTravelResult(data) {
  const body = document.getElementById("travel-body");
  const { exchange_rate: rate, budget, hotel_suggestions: hotel } = data;

  const withinBudgetNote =
    budget.totals.within_budget === null
      ? ""
      : `<p class="hint">${budget.totals.within_budget ? "✔ Within your total budget." : "⚠ Over your total budget — consider a cheaper hotel tier or fewer days."}</p>`;

  const tiers = hotel?.tiers?.length
    ? `<div class="activity-row">${hotel.tiers.map((t) => `<div class="activity-chip suitable"><span class="dot"></span><span>${t}</span></div>`).join("")}</div>
       ${hotel.tip ? `<p class="hint" style="margin-top:10px">${hotel.tip}</p>` : ""}`
    : "";

  const div = document.createElement("div");
  div.className = "plan-block";
  div.innerHTML = `
    <p class="weather-summary">1 ${rate.base} = ${rate.rate} ${rate.target} (rate date: ${rate.date})</p>
    <div class="weather-figures">
      <div class="weather-figure"><span class="value">${budget.transport.cost_home_currency} ${rate.base}</span><span class="label">Transport (${budget.transport.mode || "n/a"})</span></div>
      <div class="weather-figure"><span class="value">${budget.hotel_budget_per_night.home_currency} ${rate.base}</span><span class="label">Hotel / night</span></div>
      <div class="weather-figure"><span class="value">${budget.totals.total_cost_home_currency} ${rate.base}</span><span class="label">Total cost (${budget.totals.num_days} days)</span></div>
    </div>
    ${withinBudgetNote}
    ${tiers}
  `;
  body.appendChild(div);
  body.scrollTop = body.scrollHeight;
}

async function startTravelChat() {
  const res = await fetch(`${API_BASE}/travel/chat/start`, { method: "POST" });
  const data = await res.json();
  travelSessionId = data.session_id;
  appendChatMsg("assistant", data.reply);
}

document.getElementById("travel-search").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = e.target.querySelector("input");
  const message = input.value.trim();
  if (!message || !travelSessionId) return;

  appendChatMsg("user", message);
  input.value = "";

  try {
    const res = await fetch(`${API_BASE}/travel/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: travelSessionId, message }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error);
    appendChatMsg("assistant", data.reply);
    if (data.complete && data.travel) appendTravelResult(data.travel);
    else if (data.complete && data.travel_error) appendChatMsg("assistant", `⚠ ${data.travel_error}`);
  } catch (err) {
    appendChatMsg("assistant", `⚠ ${err.message}`);
  }
});

startTravelChat();
