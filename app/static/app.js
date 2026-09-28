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
const statusBox = document.getElementById("statusBox");
const submitBtn = document.getElementById("submitBtn");
const cancelBtn = document.getElementById("cancelBtn");

function clearPanels() {
  // 任何新的提交/取回尝试前，清除上一次的状态、结论与告警，
  // 但表单编辑内容原样保留。
  resultBox.innerHTML = "";
  statusBox.innerHTML = "";
  statusPanel.classList.add("hidden");
  errorBox.classList.add("hidden");
  errorList.innerHTML = "";
}

function showErrors(payload) {
  errorPanelOnly();
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

// 校验类拒绝独占错误面板；状态面板隐藏（四种状态分别呈现）。
function errorPanelOnly() {
  statusBox.innerHTML = "";
  statusPanel.classList.add("hidden");
  errorBox.classList.remove("hidden");
}

function showStatus(html) {
  errorBox.classList.add("hidden");
  statusBox.innerHTML = html;
  statusPanel.classList.remove("hidden");
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------- 请求标识 ----------
function genRequestId() {
  // 每次提交生成独立的 128 位十六进制请求标识（幂等键）。
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ---------- 会话隔离：旧请求的迟到响应一律不得渲染 ----------
let sessionGen = 0;
let active = null; // { id, session, timer }

function retireSession() {
  sessionGen += 1;
  if (active && active.timer) {
    clearTimeout(active.timer);
    active.timer = null;
  }
  active = null;
  cancelBtn.classList.add("hidden");
  cancelBtn.disabled = false;
  submitBtn.disabled = false;
}

const PHASE_LABEL = {
  left_enumeration: "折半枚举（左半综合征索引）",
  candidate_merge: "左右候选合并",
};

function renderComputing(state) {
  const p = state.progress || {};
  const pct = p.total ? Math.min(100, Math.floor((p.done / p.total) * 100)) : 0;
  const phase = PHASE_LABEL[p.phase] || (p.phase || "准备中");
  showStatus(`
    <p><span class="pill computing">计算中</span>
       <span class="meta">请求标识 <code>${esc(state.request_id)}</code></span></p>
    <div class="progress-wrap"><div class="progress-bar" style="width:${pct}%"></div></div>
    <p class="meta">阶段：${esc(phase)}；进度 ${p.done || 0}/${p.total || 0}（${pct}%）。
       可随时取消本次复核并立即提交另一份观测，旧计算不会覆盖本页。</p>`);
}

function renderCancelled(state) {
  showStatus(`
    <p><span class="pill cancelled">已取消</span>
       <span class="meta">请求标识 <code>${esc(state.request_id)}</code></span></p>
    <p class="state-cancelled">该次复核已在确定检查点
       （${esc(PHASE_LABEL[state.cancel_phase] || state.cancel_phase || "求解检查点")}）
       停止，<strong>未生成复核编号</strong>，也未写入任何复核记录。</p>
    <p class="meta">表单编辑内容已保留，可立即调整后重新提交。</p>`);
}

function renderConflict(data) {
  showStatus(`
    <p><span class="pill rejected">拒绝（标识冲突）</span>
       <span class="meta">请求标识 <code>${esc(data.request_id || "")}</code></span></p>
    <p class="state-conflict">${esc(data.error || "请求标识冲突")}</p>
    <p class="meta">已存输入摘要 <code>${esc((data.stored_digest || "").slice(0, 16))}</code>…，
       本次输入摘要 <code>${esc((data.replay_digest || "").slice(0, 16))}</code>…。
       复用请求标识时必须提交完全相同的输入；新观测请重新发起提交。</p>`);
}

// ---------- 结论渲染 ----------
function conclusionHtml(data) {
  const c = data.conclusion;
  let html = "";

  html += `<p style="margin-top:14px"><span class="pill completed">已完成</span>
           &nbsp;复核编号：<span class="review-id">${esc(data.review_id)}</span>
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
  return html;
}

function renderConclusion(data, container) {
  container.innerHTML = conclusionHtml(data);
}

// ---------- 轮询 ----------
async function pollRequest(id, mySession) {
  let resp;
  try {
    resp = await fetch("/api/request/" + encodeURIComponent(id));
  } catch (err) {
    if (mySession === sessionGen && active && active.id === id) {
      active.timer = setTimeout(() => pollRequest(id, mySession), 1000);
    }
    return;
  }
  // 会话已更替（用户取消后重提等）：旧请求的任何响应都不得渲染。
  if (mySession !== sessionGen || !active || active.id !== id) return;

  const state = await resp.json().catch(() => null);
  if (!resp.ok || !state) {
    active.timer = setTimeout(() => pollRequest(id, mySession), 800);
    return;
  }

  if (state.status === "computing") {
    renderComputing(state);
    active.timer = setTimeout(() => pollRequest(id, mySession), 300);
  } else if (state.status === "cancelled") {
    renderCancelled(state);
    retireSession();
  } else if (state.status === "completed") {
    renderConclusion({
      review_id: state.review_id,
      created_at: state.created_at,
      input: state.input,
      conclusion: state.conclusion,
    }, statusBox);
    // 合法完成：编号写入地址栏，刷新后可直接取回。
    history.replaceState(null, "", "#" + state.review_id);
    document.getElementById("reviewId").value = state.review_id;
    retireSession();
  } else {
    showErrors({ errors: [{ field: "request_id", message: `未知请求状态 ${state.status}` }] });
    retireSession();
  }
}

// ---------- 提交 ----------
document.getElementById("addCheck").addEventListener("click", () => addCheckRow());
submitBtn.addEventListener("click", async () => {
  clearPanels();
  let payload;
  try {
    payload = collectPayload();
  } catch (err) {
    showErrors({ errors: [{ field: "form", message: String(err) }] });
    return;
  }

  // 新会话：即使上一份观测仍在后台计算，其迟到结果也不能覆盖本页。
  retireSession();
  const mySession = ++sessionGen;
  const requestId = genRequestId();

  submitBtn.disabled = true;
  cancelBtn.classList.remove("hidden");
  cancelBtn.disabled = false;
  showStatus(`<p><span class="pill computing">计算中</span>
              <span class="meta">正在冻结排序通道与校验…</span></p>`);

  let resp;
  try {
    resp = await fetch("/api/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: requestId, ...payload }),
    });
  } catch (err) {
    if (mySession === sessionGen) {
      retireSession();
      showErrors({ errors: [{ field: "network", message: `请求失败: ${err}` }] });
    }
    return;
  }
  if (mySession !== sessionGen) return;

  const data = await resp.json().catch(() => null);
  if (resp.status === 409) {
    // 复用标识却改变输入：定位拒绝，页面与既有记录均不被覆盖。
    retireSession();
    renderConflict(data || { error: "请求标识冲突" });
    return;
  }
  if (!resp.ok) {
    retireSession();
    showErrors(data);
    return;
  }

  // 202 新建 或 200 同标识同输入重传：都只呈现该请求自身的状态。
  active = { id: requestId, session: mySession, timer: null };
  pollRequest(requestId, mySession);
});

// ---------- 取消 ----------
cancelBtn.addEventListener("click", async () => {
  if (!active) return;
  const { id, session: mySession } = active;
  cancelBtn.disabled = true;
  try {
    await fetch("/api/request/" + encodeURIComponent(id) + "/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  } catch (err) {
    if (mySession === sessionGen) cancelBtn.disabled = false;
  }
  // 终态由轮询依据持久化裁决呈现（computing -> cancelled），
  // 不做本地乐观翻转，避免与“完成先赢”竞争时显示不一致。
});

// ---------- 按编号取回 ----------
async function loadReview(id) {
  document.getElementById("reviewId").value = id;
  resultBox.innerHTML = "";
  let resp;
  try {
    resp = await fetch("/api/review/" + encodeURIComponent(id));
  } catch (err) {
    showErrors({ errors: [{ field: "network", message: `取回失败: ${err}` }] });
    return;
  }
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    resultBox.innerHTML = "";
    const reason = data && data.reason
      ? `${esc(data.error)}（${esc(data.reason)}；当前状态：${esc(data.status || "")}）`
      : null;
    showErrors(data ? {
      ...data,
      errors: data.errors || [{ field: "review_id", message: reason || data.error || "取回失败" }],
    } : { errors: [{ field: "review_id", message: "取回失败" }] });
    return;
  }
  errorBox.classList.add("hidden");
  renderConclusion(data, resultBox);
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
