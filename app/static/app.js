"use strict";

// ---------- 校验行的动态编辑 ----------
const checksBox = document.getElementById("checks");

function addCheckRow(membersText = "", parity = "0") {
  const row = document.createElement("div");
  row.className = "check-row";
  const idx = checksBox.children.length;
  row.innerHTML = `
    <span class="idx">#<span class="n">${idx + 1}</span></span>
    <input class="members" type="text" placeholder="引用通道，逗号/空格分隔" value="">
    <input class="parity" type="text" maxlength="1" value="0" title="观测奇偶值 0/1">
    <button type="button" class="del" title="删除此校验">✕</button>`;
  row.querySelector(".members").value = membersText;
  row.querySelector(".parity").value = String(parity);
  row.querySelector(".del").addEventListener("click", () => {
    row.remove();
    renumber();
  });
  checksBox.appendChild(row);
}

function renumber() {
  [...checksBox.children].forEach((r, i) => {
    r.querySelector(".n").textContent = i + 1;
  });
}

function splitTokens(text) {
  return text.split(/[\s,，;；]+/).map((t) => t.trim()).filter(Boolean);
}

function collectPayload() {
  const channels = splitTokens(document.getElementById("channels").value);
  const checks = [];
  for (const row of checksBox.querySelectorAll(".check-row")) {
    const members = splitTokens(row.querySelector(".members").value);
    // 未引用任何通道的行视为未添加的空白行，直接跳过。
    if (members.length === 0) continue;
    const rawParity = row.querySelector(".parity").value.trim();
    // 0/1 以数字提交；其它内容原样发送，由接口给出可定位的拒绝信息。
    const parity = rawParity === "0" || rawParity === "1" ? Number(rawParity) : rawParity;
    checks.push({ channels: members, parity });
  }
  return { channels, checks };
}

// ---------- 证据区与错误区 ----------
const resultBox = document.getElementById("result");
const errorBox = document.getElementById("errors");
const errorList = document.getElementById("errorList");
const statusPanel = document.getElementById("statusPanel");
const statusTitle = document.getElementById("statusTitle");
const statusBody = document.getElementById("statusBody");
const submitBtn = document.getElementById("submitBtn");
const cancelBtn = document.getElementById("cancelBtn");

// 当前页面会话：只有最新一次提交的轮询令牌允许改写页面，
// 旧请求即使稍后结束也不得覆盖新请求的展示。
let sessionToken = 0;
let currentRequestId = null;
let pollTimer = null;

function clearEvidence() {
  // 任何新的提交/取回尝试前，清除上一次的结论与告警，
  // 但表单编辑内容原样保留。
  resultBox.innerHTML = "";
  errorBox.classList.add("hidden");
  errorList.innerHTML = "";
  statusPanel.classList.add("hidden");
  statusBody.innerHTML = "";
}

function showErrors(payload) {
  clearEvidence();
  errorBox.classList.remove("hidden");
  const list = (payload && payload.errors) || [
    { field: "body", message: (payload && payload.error) || "请求被拒绝" },
  ];
  for (const e of list) {
    const div = document.createElement("div");
    div.className = "err";
    div.innerHTML = `<span class="field"></span> <span class="msg"></span>`;
    div.querySelector(".field").textContent = `[${e.field}]`;
    div.querySelector(".msg").textContent = e.message;
    errorList.appendChild(div);
  }
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------- 结论渲染 ----------
function renderConclusion(data) {
  const c = data.conclusion;
  const inp = data.input || {};
  let html = "";

  html += `<p style="margin-top:14px">复核编号：<span class="review-id">${esc(data.review_id)}</span>
           <span class="meta">（提交时间 ${esc(data.created_at || "")}；刷新后可凭编号取回）</span></p>`;

  if (c.feasible) {
    const chips = c.faulty.map((f) => `<span class="chip">${esc(f)}</span>`).join("");
    html += `
      <p class="verdict-ok" style="font-size:15px">
        ✓ 可行最优解：最小汉明重量 <strong>${c.weight}</strong>，
        故障通道 ${c.faulty.length ? chips : "（无，全板零故障即满足全部校验）"}
      </p>
      <table>
        <thead><tr><th>通道（升序）</th><th>选择向量 x</th></tr></thead><tbody>`;
    for (const [ch, bit] of Object.entries(c.vector)) {
      html += `<tr><td>${esc(ch)}</td><td>${bit}</td></tr>`;
    }
    html += `</tbody></table>
      <p class="meta">折半：左半 ${c.left_size} 个通道，左半综合征索引 ${c.left_index_size} 条；
           按总重量、再按选择向量字典序精确裁决。</p>
      <h2 style="font-size:14px;color:var(--accent);margin-top:18px">逐校验复算</h2>
      <table>
        <thead><tr><th>#</th><th>引用通道</th><th>观测奇偶</th><th>复算 XOR</th><th>结果</th></tr></thead><tbody>`;
    c.recompute.forEach((r, i) => {
      html += `<tr>
        <td>${i + 1}</td>
        <td>${r.members.map((m) => esc(m)).join(" ⊕ ")}</td>
        <td>${r.observed}</td>
        <td>${r.recomputed}</td>
        <td><span class="badge ${r.pass ? "pass" : "fail"}">${r.pass ? "一致" : "不一致"}</span></td>
      </tr>`;
    });
    html += "</tbody></table>";
  } else {
    html += `
      <p class="verdict-bad" style="font-size:15px">
        ✗ ${esc(c.message)}
      </p>
      <p class="meta">已保存不可行结论（复核编号见上）。系统未返回任何近似或部分满足的通道集合。
        折半：左半 ${c.left_size} 个通道，左半综合征索引 ${c.left_index_size} 条，
        两侧候选精确合并后无匹配。</p>`;
  }

  resultBox.innerHTML = html;
}

// ---------- 请求状态 ----------
function setButtonsRunning(running) {
  submitBtn.disabled = running;
  submitBtn.textContent = running ? "求解中…" : "提交求解";
  cancelBtn.classList.toggle("hidden", !running);
}

function escStatus(s) {
  return ({
    pending: "排队中", running: "计算中", done: "已完成",
    cancelled: "已取消", failed: "求解失败",
  })[s] || s;
}

function renderStatus(data, token) {
  if (token !== sessionToken) return;  // 旧请求不得覆盖页面
  statusPanel.classList.remove("hidden");
  statusPanel.classList.remove("computing", "cancelled", "done", "failed");
  const st = data.status;
  let title = `请求 ${escStatus(st)}`;
  if (st === "pending" || st === "running") {
    title = st === "pending" ? "请求已排队" : "计算中（可取消）";
    statusPanel.classList.add("computing");
  } else if (st === "cancelled") {
    statusPanel.classList.add("cancelled");
  } else if (st === "done") {
    statusPanel.classList.add("done");
  } else {
    statusPanel.classList.add("failed");
  }
  statusTitle.textContent = title;

  const pct = data.progress && typeof data.progress.percent === "number"
    ? data.progress.percent : 0;
  const phase = data.progress && data.progress.phase === "right"
    ? "左右候选合并" : "折半枚举";
  let html = `<p class="meta">请求标识：<span class="req-id">${esc(data.request_id)}</span>`;
  if (data.review_id) {
    html += `　复核编号：<span class="review-id">${esc(data.review_id)}</span>`;
  }
  html += "</p>";
  if (st === "pending" || st === "running") {
    html += `<div class="progress-track"><div class="progress-fill"
                 style="width:${st === "pending" ? 0 : Math.max(pct, 2)}%"></div></div>
             <p class="meta">${phase}（约 ${st === "pending" ? 0 : pct}%）；
             取消后不生成复核编号，可在同页立即提交另一份观测。</p>`;
  } else if (st === "cancelled") {
    html += `<p class="meta">该次复核已在折半枚举/候选合并的确定检查点响应取消，
             未生成复核编号，结论未写入复核记录。</p>`;
  } else if (st === "failed") {
    html += `<p class="meta">求解过程异常终止，请核对输入后重新提交。</p>`;
  } else {
    html += `<p class="meta">计算完成，结论如下（刷新页面后可凭复核编号取回）。</p>`;
  }
  statusBody.innerHTML = html;

  if (st === "done") {
    renderConclusion(data);
  } else {
    resultBox.innerHTML = "";
  }
}

async function pollRequest(requestId, token) {
  let data;
  try {
    const resp = await fetch("/api/request/" + encodeURIComponent(requestId));
    data = await resp.json().catch(() => null);
    if (!resp.ok) throw new Error("状态查询失败");
  } catch (err) {
    if (token === sessionToken) {
      pollTimer = setTimeout(() => pollRequest(requestId, token), 1000);
    }
    return;
  }
  if (token !== sessionToken) return;  // 已被取消后重提或新提交取代
  renderStatus(data, token);
  if (data.status === "pending" || data.status === "running") {
    pollTimer = setTimeout(() => pollRequest(requestId, token), 400);
  } else {
    setButtonsRunning(false);
    pollTimer = null;
  }
}

async function startRequest(payload) {
  // 旧会话作废：任何迟到的旧请求回调都不得再改写页面。
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  const token = ++sessionToken;
  currentRequestId = null;
  setButtonsRunning(true);
  clearEvidence();
  let data;
  try {
    const resp = await fetch("/api/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    data = await resp.json().catch(() => null);
    if (!resp.ok) throw { status: resp.status, data };
  } catch (err) {
    if (token !== sessionToken) return;
    setButtonsRunning(false);
    showErrors((err && err.data) || {
      errors: [{ field: "network", message: `请求失败: ${err}` }],
    });
    return;
  }
  if (token !== sessionToken) return;
  currentRequestId = data.request_id;
  if (data.status === "pending" || data.status === "running") {
    renderStatus(data, token);
    pollRequest(data.request_id, token);
  } else {
    // 相同输入重传：直接返回既有终态。
    renderStatus(data, token);
    setButtonsRunning(false);
    if (data.status === "done" && data.review_id) {
      history.replaceState(null, "", "#" + data.review_id);
      document.getElementById("reviewId").value = data.review_id;
    }
  }
}

// ---------- 提交 ----------
document.getElementById("addCheck").addEventListener("click", () => addCheckRow());
document.getElementById("submitBtn").addEventListener("click", () => {
  let payload;
  try {
    payload = collectPayload();
  } catch (err) {
    showErrors({ errors: [{ field: "form", message: String(err) }] });
    return;
  }
  startRequest(payload);
});

// ---------- 取消 ----------
cancelBtn.addEventListener("click", async () => {
  const requestId = currentRequestId;
  if (!requestId) return;
  cancelBtn.disabled = true;
  try {
    const resp = await fetch("/api/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: requestId }),
    });
    const data = await resp.json().catch(() => null);
    if (resp.ok) {
      renderStatus(data, sessionToken);
      // 取消已持久化裁决（可能稍早/稍晚于完成）：恢复提交按钮，
      // 允许在同页立即提交另一份观测；终态由在途轮询最终确认。
      if (data.status === "cancelled") setButtonsRunning(false);
    }
  } finally {
    cancelBtn.disabled = false;
  }
});

// ---------- 按编号取回 ----------
async function loadReview(id) {
  // 取回动作接管页面会话：迟到的旧轮询不得覆盖取回的记录。
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  const token = ++sessionToken;
  currentRequestId = null;
  setButtonsRunning(false);
  clearEvidence();
  document.getElementById("reviewId").value = id;
  const resp = await fetch("/api/review/" + encodeURIComponent(id));
  const data = await resp.json().catch(() => null);
  if (token !== sessionToken) return;
  if (!resp.ok) {
    showErrors(data || { errors: [{ field: "review_id", message: "取回失败" }] });
    return;
  }
  renderConclusion(data);
}

document.getElementById("loadBtn").addEventListener("click", () => {
  const id = document.getElementById("reviewId").value.trim();
  if (id) loadReview(id);
});
document.getElementById("reviewId").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    const id = e.target.value.trim();
    if (id) loadReview(id);
  }
});

// ---------- 示例 ----------
document.getElementById("sampleBtn").addEventListener("click", () => {
  document.getElementById("channels").value = "CH0 CH1 CH2 CH3 CH4 CH5";
  checksBox.innerHTML = "";
  // 唯一故障解：仅 CH3 失效。
  addCheckRow("CH0 CH1 CH3", "1");
  addCheckRow("CH2 CH3 CH4", "1");
  addCheckRow("CH3 CH5", "1");
  addCheckRow("CH0 CH2 CH4", "0");
});

// ---------- 初始化：至少 3 条空校验行；带编号哈希时刷新即取回 ----------
for (let i = 0; i < 3; i++) addCheckRow();
const hashId = decodeURIComponent(location.hash || "").replace(/^#/, "");
if (hashId) loadReview(hashId);
