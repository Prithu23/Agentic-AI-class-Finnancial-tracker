const API_BASE = "/api"; // Flask serves this frontend too, so same-origin relative paths work

/* ============================================================
   Savings Advisor — chat intake, hands off to the savings plan
   ============================================================ */
let financeSessionId = null;

function appendChatMsg(role, text) {
  const body = document.getElementById("finance-body");
  const div = document.createElement("div");
  div.className = `chat-msg ${role}`;
  div.textContent = text;
  body.appendChild(div);
  body.scrollTop = body.scrollHeight;
}

function appendPlan(plan) {
  const body = document.getElementById("finance-body");
  const div = document.createElement("div");
  div.className = "plan-block";
  div.innerHTML = `
    <p class="plan-figure">₹${Number(plan.monthly_recommended_savings).toLocaleString("en-IN")} / month</p>
    <p class="plan-summary">${plan.summary || ""}</p>
  `;
  body.appendChild(div);
  body.scrollTop = body.scrollHeight;
}

async function startFinanceChat() {
  const res = await fetch(`${API_BASE}/chat/start`, { method: "POST" });
  const data = await res.json();
  financeSessionId = data.session_id;
  appendChatMsg("assistant", data.reply);
}

document.getElementById("finance-search").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = e.target.querySelector("input");
  const message = input.value.trim();
  if (!message || !financeSessionId) return;

  appendChatMsg("user", message);
  input.value = "";

  try {
    const res = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: financeSessionId, message }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error);
    appendChatMsg("assistant", data.reply);
    if (data.complete && data.plan) appendPlan(data.plan);
  } catch (err) {
    appendChatMsg("assistant", `⚠ ${err.message}`);
  }
});

startFinanceChat();
