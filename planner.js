const API_BASE = "/api"; // Flask serves this frontend too, so same-origin relative paths work

/* ============================================================
   Finance Planner — chat intake, hands off to the strategy generator
   ============================================================ */
let plannerSessionId = null;

function appendChatMsg(role, text) {
  const body = document.getElementById("planner-body");
  const div = document.createElement("div");
  div.className = `chat-msg ${role}`;
  div.textContent = text;
  body.appendChild(div);
  body.scrollTop = body.scrollHeight;
}

function formatMoney(n) {
  return Number(n).toLocaleString("en-IN", { maximumFractionDigits: 0 });
}

function renderStrategyCard(s, isRecommended) {
  const badge = isRecommended
    ? `<span class="strategy-badge">Recommended</span>`
    : s.pruned
    ? `<span class="strategy-badge muted">Pruned — ${s.prune_reason}</span>`
    : "";

  return `
    <div class="strategy-card ${isRecommended ? "recommended" : ""} ${s.pruned ? "pruned" : ""}">
      <p class="strategy-name">${s.name} ${badge}</p>
      <p class="strategy-rationale">${s.rationale}</p>
      <div class="strategy-figures">
        <div class="weather-figure"><span class="value">₹${formatMoney(s.amounts.emergency)}</span><span class="label">Emergency (${s.allocation_pct.emergency_pct}%)</span></div>
        <div class="weather-figure"><span class="value">₹${formatMoney(s.amounts.investment)}</span><span class="label">Investment (${s.allocation_pct.investment_pct}%)</span></div>
        <div class="weather-figure"><span class="value">₹${formatMoney(s.amounts.skill)}</span><span class="label">Skill dev. (${s.allocation_pct.skill_pct}%)</span></div>
      </div>
      <div class="strategy-figures">
        <div class="weather-figure"><span class="value">${s.liquidity_score}</span><span class="label">Liquidity score</span></div>
        <div class="weather-figure"><span class="value">${s.risk_score}</span><span class="label">Risk score</span></div>
        <div class="weather-figure"><span class="value">${s.expected_return_pct}%</span><span class="label">Est. annual return</span></div>
        <div class="weather-figure"><span class="value">${s.fit_score}</span><span class="label">Goal/constraint fit</span></div>
      </div>
      <p class="strategy-fit">${s.fit_reasoning}${s.backtrack_note ? ` — ${s.backtrack_note}` : ""}</p>
    </div>
  `;
}

function appendPlanResult(plan) {
  const body = document.getElementById("planner-body");
  const div = document.createElement("div");
  div.className = "plan-block";

  const sorted = [...plan.strategies].sort((a, b) => b.overall_score - a.overall_score);
  const cards = sorted.map((s) => renderStrategyCard(s, s.name === plan.recommended)).join("");

  div.innerHTML = `
    <p class="weather-summary">₹${formatMoney(plan.total_amount)} to allocate, ${plan.risk_preference} risk preference. Generated ${plan.strategies.length} strategies, kept ${plan.strategies.filter((s) => !s.pruned).length} after pruning.</p>
    ${cards}
  `;
  body.appendChild(div);
  body.scrollTop = body.scrollHeight;
}

async function startPlannerChat() {
  const res = await fetch(`${API_BASE}/planner/chat/start`, { method: "POST" });
  const data = await res.json();
  plannerSessionId = data.session_id;
  appendChatMsg("assistant", data.reply);
}

document.getElementById("planner-search").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = e.target.querySelector("input");
  const message = input.value.trim();
  if (!message || !plannerSessionId) return;

  appendChatMsg("user", message);
  input.value = "";

  try {
    const res = await fetch(`${API_BASE}/planner/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: plannerSessionId, message }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error);
    appendChatMsg("assistant", data.reply);
    if (data.complete && data.plan) appendPlanResult(data.plan);
    else if (data.complete && data.plan_error) appendChatMsg("assistant", `⚠ ${data.plan_error}`);
  } catch (err) {
    appendChatMsg("assistant", `⚠ ${err.message}`);
  }
});

startPlannerChat();
