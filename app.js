import { api } from "./api.js";
import { EngagementTracker } from "./engagement.js";

const app = document.getElementById("app");
const navEl = document.getElementById("nav");
const toastEl = document.getElementById("toast");
let currentUser = null;
let tracker = null;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtTime(s) {
  s = Math.max(0, Number(s) || 0);
  const m = Math.floor(s / 60), r = s % 60;
  return m > 0 ? `${m}:${String(r).padStart(2, "0")}` : `${r}s`;
}
function toast(msg, ms = 4200) {
  toastEl.textContent = msg;
  toastEl.hidden = false;
  clearTimeout(toastEl._t);
  toastEl._t = setTimeout(() => { toastEl.hidden = true; }, ms);
}
function stopTracker() { if (tracker) { tracker.stop(); tracker = null; } }
function route() {
  stopTracker();
  const hash = location.hash || "#/dashboard";
  const mLecture = hash.match(/^#\/lecture\/([\w\-]+)/);
  const mQuiz = hash.match(/^#\/quiz\/([\w\-]+)/);
  if (hash.startsWith("#/login")) return viewLogin();
  if (hash.startsWith("#/register")) return viewRegister();
  if (hash.startsWith("#/admin")) return viewAdmin();
  if (mLecture) return viewLecture(mLecture[1]);
  if (mQuiz) return viewQuiz(mQuiz[1]);
  return viewDashboard();
}
window.addEventListener("hashchange", route);

async function refreshMe() {
  try { const d = await api("/api/auth/me"); currentUser = d.user; }
  catch { currentUser = null; }
  renderNav();
}
function renderNav() {
  if (!currentUser) {
    navEl.innerHTML = `<a href="#/login">Login</a><a href="#/register">Register</a>`;
    return;
  }
  const admin = currentUser.role === "admin" ? `<a href="#/admin">Admin</a>` : "";
  navEl.innerHTML = `${admin}<a href="#/dashboard">Dashboard</a><span class="muted small">${esc(currentUser.username)} (${esc(currentUser.role)})</span><a href="#" id="logout">Logout</a>`;
  document.getElementById("logout").onclick = async (e) => {
    e.preventDefault();
    try { await api("/api/auth/logout", { method: "POST", body: {} }); } catch {}
    currentUser = null;
    location.hash = "#/login";
    renderNav();
  };
}

// ------------------------------------------------------------------ views
async function viewDashboard() {
  if (!currentUser) return (location.hash = "#/login");
  renderNav();
  app.innerHTML = `<h1>Learning path</h1><p class="muted">Progressive, verified modules — each unlocks only after verified reading time + ≥80% quiz.</p><div id="root"><p class="muted">Loading…</p></div>`;
  try {
    const d = await api("/api/modules");
    document.getElementById("root").innerHTML = `
      <div class="card">
        <div class="row spread"><strong>${d.completed}/${d.total} modules completed</strong><span class="badge ok">${d.progress_pct}%</span></div>
        <div class="progressbar"><div style="width:${d.progress_pct}%"></div></div>
        <p class="muted small">Logged in as <b>${esc(currentUser.username)}</b>. Timed pages pause when idle (&gt;30s without mouse/scroll/key activity or when the tab loses focus). Verified active seconds are enforced server-side.</p>
      </div>
      ${d.modules.map(m => `
        <div class="card ${m.unlocked ? "" : "locked"}">
          <div class="row spread">
            <div><h3>${m.ord + 1}. ${esc(m.title)}</h3><p class="muted small">${esc(m.description)}</p>
            <p class="small muted">Lecture ${fmtTime(m.tmin_s)}–${fmtTime(m.tmax_s)} active · Quiz ${fmtTime(m.quiz_tmin_s)}–${fmtTime(m.quiz_tmax_s)} · Pass ${m.pass_score}%${m.attempts ? ` · Best ${m.best_score}% (${m.attempts} attempt${m.attempts > 1 ? "s" : ""})` : ""}</p></div>
            <div style="min-width:190px;text-align:right">
              ${m.completed ? `<span class="badge ok">Completed</span>` : m.unlocked ? `<span class="badge">Unlocked</span>` : `<span class="badge bad">🔒 Locked</span>`}
              <div style="margin-top:8px">
              ${m.unlocked
                ? `<a class="btn" href="#/lecture/${m.id}">${m.lecture_verified ? "Review lecture" : "Start lecture"}</a>
                   ${m.lecture_verified && !m.completed ? `<a class="btn ghost" href="#/quiz/${m.id}">Take quiz</a>` : ""}`
                : `<button class="btn" disabled>Complete previous module</button>`}
              </div>
            </div>
          </div>
        </div>`).join("")}`;
  } catch (e) { document.getElementById("root").innerHTML = `<p>${esc(e.message)}</p>`; }
}

async function viewLecture(moduleId) {
  if (!currentUser) return (location.hash = "#/login");
  app.innerHTML = `<p class="muted">Loading module…</p>`;
  let mod;
  try { mod = await api(`/api/modules/${moduleId}`); }
  catch (e) {
    app.innerHTML = `<div class="card"><p>${esc(e.message)}</p><a class="btn" href="#/dashboard">Back to dashboard</a></div>`;
    return;
  }
  const startBtn = `<div class="card" id="startcard"><h1>${esc(mod.title)}</h1>
    <p class="muted">Verified reading time required: <b>${fmtTime(mod.tmin_s)}</b> active seconds (idle pauses the timer). Session window: <b>${fmtTime(mod.tmax_s)}</b> wall-clock. Keep the tab focused and move/scroll/press keys to stay active.</p>
    <button class="btn" id="start">Start lecture & timer</button></div>`;
  if (mod.lecture_verified) {
    app.innerHTML = startBtn.replace('id="start"', 'disabled id="start"');
    document.getElementById("startcard").insertAdjacentHTML("beforeend",
      `<p><span class="badge ok">Lecture verified</span> <a class="btn" href="#/quiz/${moduleId}">Continue to quiz →</a></p>`);
    return;
  }
  app.innerHTML = startBtn;
  document.getElementById("start").onclick = async () => {
    const s = await api("/api/sessions/start", { method: "POST", body: { module_id: moduleId, kind: "lecture" } });
    tracker = new EngagementTracker({ onTick: (st) => renderTimer(st), onPauseChange: () => renderTimer(tracker.state()) });
    mountLecture(mod, s);
  };
}

function mountLecture(mod, session) {
  document.getElementById("startcard").innerHTML = `
    <h1>${esc(mod.title)}</h1>
    <div class="timerbar" id="timerbar">
      <div class="row spread"><strong>⏱ Verified active reading</strong><span id="pausebadge"></span></div>
      <div class="timergrid">
        <div><div class="muted small">Server-verified active</div><div class="num" id="tActive">0s</div><div class="small muted">target ${fmtTime(mod.tmin_s)}</div></div>
        <div><div class="muted small">Wall time</div><div class="num" id="tWall">0s</div><div class="small muted">max ${fmtTime(mod.tmax_s)} · <span id="tRemain"></span></div></div>
        <div><div class="muted small">Engagement</div><div class="num" id="tIdle">0s</div><div class="small muted">idle &gt;30s pauses · <span id="tEvents"></span></div></div>
      </div>
      <div class="progressbar"><div id="tBar" style="width:0%"></div></div>
      <div id="tVerdict" class="small muted"></div>
      <div id="tTimeout" hidden><p class="badge bad">⏰ Tmax exceeded — this section must be restarted.</p><button class="btn" id="restart">Restart section</button></div>
      <div class="row" style="margin-top:8px">
        <button class="btn" id="finish" disabled>Verify & complete lecture</button>
        <a class="btn ghost" id="toQuiz" hidden href="#/quiz/${mod.id}">Proceed to quiz →</a>
        <span class="small muted" id="needNote"></span>
      </div>
      <div id="tError" class="small muted" style="margin-top:6px"></div>
    </div>
    <article class="lecture-body">${mod.lecture_body}</article>
  `;
  const render = (st) => {
    const bar = document.getElementById("timerbar");
    if (!bar) return;
    if (st.paused) bar.classList.add("paused"); else bar.classList.remove("paused");
    if (st.timedOut) bar.classList.add("timeout"); else bar.classList.remove("timeout");
    document.getElementById("pausebadge").innerHTML = st.timedOut
      ? `<span class="badge bad">Timed out</span>`
      : st.paused ? `<span class="badge warn">⏸ Paused — idle ${st.idleS}s</span>` : `<span class="badge ok">● Active</span>`;
    document.getElementById("tActive").textContent = fmtTime(st.serverActiveS ?? st.activeS) + (st.serverActiveS !== null ? ` (client ${fmtTime(st.activeS)})` : "");
    document.getElementById("tWall").textContent = fmtTime(st.serverWallS ?? st.wallS);
    document.getElementById("tRemain").textContent = st.remainingS !== null ? `${fmtTime(st.remainingS)} left` : "";
    document.getElementById("tIdle").textContent = `${st.idleS}s`;
    document.getElementById("tEvents").textContent = `🖱 ${st.mouseMoves} · ⇅ ${st.scrolls} · ⌨ ${st.keys}`;
    const pct = Math.min(100, Math.round(100 * (st.serverActiveS ?? st.activeS) / mod.tmin_s));
    document.getElementById("tBar").style.width = pct + "%";
    document.getElementById("tVerdict").textContent = st.verdict ? `Server verdict: ${st.verdict} — server counters are authoritative for completion.` : "Connecting to server…";
    const ready = (st.serverActiveS ?? 0) >= mod.tmin_s && !st.timedOut;
    document.getElementById("finish").disabled = !ready;
    document.getElementById("needNote").textContent = ready ? "Ready. Verifying will unlock the quiz." : `Keep reading — ${fmtTime(mod.tmin_s - (st.serverActiveS ?? st.activeS))} active seconds to go (server-verified).`;
    if (tracker && tracker.lastError) document.getElementById("tError").textContent = `Heartbeat error: ${tracker.lastError.message} — keep the tab open.`;
    if (st.timedOut) document.getElementById("tTimeout").hidden = false;
  };
  tracker.start({
    heartbeatEveryMs: 5000,
    idleThresholdMs: session.idle_threshold_s * 1000,
    heartbeat: async (snap) => api("/api/telemetry/heartbeat", { method: "POST", body: { session_id: session.session_id, ...snap } }),
    // onTick wired via constructor reload below
  });
  tracker.onTick = render;
  tracker.onPauseChange = () => render(tracker.state());
  render(tracker.state());
  document.getElementById("finish").onclick = async () => {
    try {
      await new Promise((r) => setTimeout(r, 300));
      const r = await api("/api/lecture/complete", { method: "POST", body: { session_id: session.session_id } });
      tracker.stop(); tracker = null;
      toast(`Lecture verified (${fmtTime(r.server_active_s)} active). Quiz unlocked!`);
      location.hash = `#/quiz/${mod.id}`;
    } catch (e) { toast(e.message, 6000); }
  };
  document.getElementById("restart").onclick = async () => {
    tracker.stop();
    try {
      const s = await api("/api/sessions/start", { method: "POST", body: { module_id: mod.id, kind: "lecture" } });
      tracker = new EngagementTracker({});
      mountLecture(mod, s);
    } catch (e) { toast(e.message); }
  };
}

async function viewQuiz(moduleId) {
  if (!currentUser) return (location.hash = "#/login");
  app.innerHTML = `<p class="muted">Loading quiz…</p>`;
  let q;
  try { q = await api(`/api/modules/${moduleId}/quiz`); }
  catch (e) {
    app.innerHTML = `<div class="card"><p>${esc(e.message)}</p><a class="btn" href="#/dashboard">Back</a></div>`;
    return;
  }
  app.innerHTML = `
    <div class="card"><div class="row spread"><h1 style="margin:0">${esc(q.title)} — Assessment</h1>
    <span class="badge">${q.already_passed ? "Previously passed — retake allowed for practice" : `Pass ≥ ${q.pass_score}%`}</span></div>
    <p class="muted small">Time window (server-enforced): ${fmtTime(q.quiz_tmin_s)}–${fmtTime(q.quiz_tmax_s)} wall-clock. Keep the tab active; idle pauses tracking. Answers lock on submit and explanations are revealed after grading.</p>
    <button class="btn" id="startq">Start timed quiz</button></div><div id="quizbody"></div>`;
  document.getElementById("startq").onclick = async () => {
    const s = await api("/api/sessions/start", { method: "POST", body: { module_id: moduleId, kind: "quiz" } });
    mountQuiz(q, s);
  };
}

function mountQuiz(q, session) {
  tracker = new EngagementTracker({});
  document.getElementById("quizbody").innerHTML = `
    <div class="timerbar" id="timerbar"><div class="row spread"><strong>⏱ Quiz timer (server wall-clock)</strong><span id="pausebadge"></span></div>
    <div class="timergrid"><div><div class="muted small">Wall</div><div class="num" id="tWall">0s</div><div class="small muted">min ${fmtTime(q.quiz_tmin_s)} · max ${fmtTime(q.quiz_tmax_s)} · <span id="tRemain"></span></div></div>
    <div><div class="muted small">Engagement</div><div class="num" id="tIdle">0s</div><div class="small muted">idle &gt;30s pauses · <span id="tEvents"></span></div></div></div>
    <div id="tTimeout" hidden><p class="badge bad">⏰ Quiz Tmax exceeded — restart required.</p><button class="btn" id="restart">Restart quiz</button></div>
    <div id="tError" class="small muted"></div></div>
    <form id="qform">
      ${q.questions.map((qq, i) => `
      <fieldset id="q${qq.id}"><legend><b>Q${i + 1}.</b> ${esc(qq.q_text)}</legend>
        ${Object.entries(qq.options).map(([k, v]) => `
        <label class="opt"><input type="radio" name="q${qq.id}" value="${esc(k)}" /> <b>${esc(k.toUpperCase())}.</b> ${esc(v)}</label>`).join("")}
      </fieldset>`).join("")}
      <button type="submit" class="btn">Submit answers</button>
      <span class="small muted" id="submitNote"></span>
    </form><div id="result"></div>`;
  const render = (st) => {
    const bar = document.getElementById("timerbar");
    if (!bar) return;
    document.getElementById("pausebadge").innerHTML = st.timedOut ? `<span class="badge bad">Timed out</span>` : st.paused ? `<span class="badge warn">⏸ Paused — idle ${st.idleS}s</span>` : `<span class="badge ok">● Active</span>`;
    document.getElementById("tWall").textContent = fmtTime(st.serverWallS ?? st.wallS);
    document.getElementById("tRemain").textContent = st.remainingS !== null ? `${fmtTime(st.remainingS)} left` : "";
    document.getElementById("tIdle").textContent = `${st.idleS}s`;
    document.getElementById("tEvents").textContent = `🖱 ${st.mouseMoves} · ⇅ ${st.scrolls} · ⌨ ${st.keys}`;
    if (tracker && tracker.lastError) document.getElementById("tError").textContent = `Heartbeat error: ${tracker.lastError.message}`;
    if (st.timedOut) document.getElementById("tTimeout").hidden = false;
    bar.classList.toggle("paused", !!st.paused);
    bar.classList.toggle("timeout", !!st.timedOut);
  };
  tracker.start({
    heartbeatEveryMs: 5000, idleThresholdMs: session.idle_threshold_s * 1000,
    heartbeat: async (snap) => api("/api/telemetry/heartbeat", { method: "POST", body: { session_id: session.session_id, ...snap } }),
  });
  tracker.onTick = render;
  tracker.onPauseChange = () => render(tracker.state());
  render(tracker.state());
  document.getElementById("qform").onsubmit = async (e) => {
    e.preventDefault();
    const answers = {};
    for (const qq of q.questions) {
      const sel = document.querySelector(`input[name="q${qq.id}"]:checked`);
      if (sel) answers[String(qq.id)] = sel.value;
    }
    if (Object.keys(answers).length !== q.questions.length) {
      document.getElementById("submitNote").textContent = "Answer all questions first.";
      return;
    }
    try {
      const r = await api("/api/quiz/submit", { method: "POST", body: { session_id: session.session_id, answers } });
      tracker.stop(); tracker = null;
      document.getElementById("result").innerHTML = `
        <div class="card">
          <div class="row spread"><h2 style="margin:0">Score: ${r.score}% ${r.passed ? '<span class="badge ok">Passed</span>' : `<span class="badge bad">Not passed (need ${r.pass_score}%)</span>`}</h2><span class="muted small">${r.correct}/${r.total} correct · attempt #${r.attempts} · best ${r.best_score}%</span></div>
          ${r.details.map(d => `
            <fieldset class="${d.correct ? "correct" : "wrong"}"><legend><b>Q${d.ord}.</b> ${esc(d.q_text)}</legend>
              <p>Your answer: <b>${esc((d.your_answer || "—").toUpperCase())}</b> · Correct: <b>${esc(d.correct_answer.toUpperCase())}</b> ${d.correct ? "✅" : "❌"}</p>
              <p class="muted small">${esc(d.explanation)}</p></fieldset>`).join("")}
          ${r.passed
            ? (r.next_unlocked ? `<p><a class="btn" href="#/lecture/${r.next_unlocked}">Next module unlocked →</a></p>` : `<p><span class="badge ok">🎉 Course path complete.</span> <a class="btn" href="#/dashboard">Dashboard</a></p>`)
            : `<p><button class="btn" id="retry">Retry quiz</button></p>`}
        </div>`;
      document.getElementById("qform").style.display = "none";
      document.getElementById("timerbar").style.display = "none";
      const retry = document.getElementById("retry");
      if (retry) retry.onclick = () => viewQuiz(q.module_id);
      window.scrollTo({ top: 0 });
    } catch (e) {
      document.getElementById("submitNote").textContent = e.message;
      toast(e.message);
      if (e.code === "need_more_time") document.getElementById("submitNote").textContent += ` Server wall so far: ${e.payload?.server_wall_s ?? "?"}s (min ${e.payload?.quiz_tmin_s ?? "?"}s) — keep working and resubmit.`;
    }
  };
  document.getElementById("restart").onclick = async () => {
    tracker.stop();
    try {
      const s = await api("/api/sessions/start", { method: "POST", body: { module_id: q.module_id, kind: "quiz" } });
      mountQuiz(q, s);
    } catch (e) { toast(e.message); }
  };
}

async function viewAdmin() {
  if (!currentUser) return (location.hash = "#/login");
  app.innerHTML = `<h1>Admin</h1><div id="root"><p class="muted">Loading…</p></div>`;
  try {
    const [users, tele] = await Promise.all([api("/api/admin/users"), api("/api/admin/telemetry?limit=100")]);
    document.getElementById("root").innerHTML = `
      <div class="card"><h2>Users</h2>
      ${users.users.map(u => `<div class="row spread" style="border-top:1px solid var(--line);padding:8px 0">
        <div><b>${esc(u.username)}</b> <span class="muted small">${esc(u.email)} · ${esc(u.role)} · completed ${u.completed} · active ${fmtTime(u.active_seconds)}</span></div>
        ${u.username === "admin" ? "" : `<button class="btn danger" data-reset="${u.id}">Reset progress</button>`}
      </div>`).join("")}</div>
      <div class="card"><h2>Telemetry (${tele.summary.total} heartbeats · ${tele.summary.clamped} clamped · ${tele.summary.rejected} rejected)</h2>
      <div class="code">${tele.logs.slice(0, 30).map(l => `#${l.id} ${esc(l.ts)} ${esc(l.username)} ${esc(l.session_id.slice(0, 8))} claimed ${l.claimed_active_s}s/${l.claimed_wall_s}s → server ${l.server_active_s}s/${l.server_wall_s}s mouse ${l.mouse_moves} scroll ${l.scrolls} keys ${l.keys} → ${esc(l.verdict)}`).join("\n") || "No heartbeats yet."}</div></div>`;
    document.querySelectorAll("[data-reset]").forEach(b => b.onclick = async () => {
      if (!confirm("Reset this user's progress + sessions?")) return;
      await api(`/api/admin/users/${b.dataset.reset}/reset`, { method: "POST", body: {} });
      toast("Progress reset."); viewAdmin();
    });
  } catch (e) { document.getElementById("root").innerHTML = `<p>${esc(e.message)}</p>`; }
}

async function viewLogin() {
  app.innerHTML = `<div class="card" style="max-width:420px"><h1>Login</h1>
    <form id="f"><label>Username or email</label><input name="username" required /><label>Password</label><input name="password" type="password" required />
    <button class="btn" style="margin-top:14px;width:100%">Login</button><p id="err" class="muted small"></p>
    <p class="small muted">Demo: <b>demo / demo123</b> · Admin: <b>admin / admin123</b> (change after first login!)</p>
    <p class="small">No account? <a href="#/register">Register</a></p></div></div>`;
  document.getElementById("f").onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try { await api("/api/auth/login", { method: "POST", body: { username: fd.get("username"), password: fd.get("password") } }); await refreshMe(); location.hash = "#/dashboard"; route(); }
    catch (err) { document.getElementById("err").textContent = err.message; }
  };
}
async function viewRegister() {
  app.innerHTML = `<div class="card" style="max-width:420px"><h1>Register</h1>
    <form id="f"><label>Username</label><input name="username" required /><label>Email</label><input name="email" type="email" required />
    <label>Password (min 8 chars)</label><input name="password" type="password" required />
    <button class="btn" style="margin-top:14px;width:100%">Create account</button><p id="err" class="muted small"></p>
    <p class="small">Have an account? <a href="#/login">Login</a></p></div></div>`;
  document.getElementById("f").onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try { await api("/api/auth/register", { method: "POST", body: { username: fd.get("username"), email: fd.get("email"), password: fd.get("password") } }); await refreshMe(); location.hash = "#/dashboard"; route(); }
    catch (err) { document.getElementById("err").textContent = err.message; }
  };
}

await refreshMe();
route();
