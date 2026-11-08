# ICS/OT Security Academy — Interactive Learning Platform

A web platform that takes **absolute beginners → junior ICS/OT security specialists**:
progressive module locking, lecture + assessment quizzes (≥80% to pass), and
**server-verified active-time timers** with cursor/scroll/key telemetry and
AFK pause — background-tab progression is not possible.

## Quick start (no installs, no build step)

```bash
cd ics-platform
python3 run.py            # http://127.0.0.1:8000
```

Open the URL, log in, and work through 4 modules. **Python 3.10+ stdlib only**
(SQLite + `http.server`). No `pip install`, no npm.

Seeded accounts (change after first login):

| Role | Username | Password |
|------|----------|----------|
| Admin | `admin` | `admin123` |
| Learner | `demo` | `demo123` |

Admin (`#/admin` or `/api/admin/*`) sees all users, per-user verified active
seconds, the full telemetry log (accepted/clamped/rejected heartbeats) and can
reset a learner's progress.

## How it meets each requirement

| Requirement | Implementation |
|---|---|
| **A. Progressive unlocking** | Modules unlock in order. `is_unlocked()` = first module, or a `progress` row exists, or previous module `lecture_verified && quiz_passed`. Locked content returns `403` and the lecture body is never sent. |
| **B. Lectures + quizzes, 80%** | `modules` holds Tmin/Tmax/pass_score + safe HTML lecture; `questions` holds options/correct/explanation. Passing score configurable per module (seeded 80%). Explanations revealed only **after** submit. |
| **C. Tmin/Tmax + AFK** | Client hook (see below) pauses after 30 s without mouse/scroll/key or on blur/hidden. Server enforces Tmin/Tmax independently: `need_more_time` / `timeout` (restart required). |
| **Engagement telemetry** | `mousemove/scroll/keydown/blur/focus` counted client-side, heartbeated every 5 s, stored in `telemetry_logs` with claimed vs server-trusted values. |
| **Anti-cheat** | Server recomputes active time: `active_delta = min(claimed_delta, heartbeat_gap + 2s)`; heartbeat gaps > 30 s award 0 active. Inflated claims are **clamped** + logged; non-monotonic/absurd claims are **rejected**. Completion is decided by `server_active_s`/`server_wall_s` only. |
| **D1. Schema** | `schema.sql` + `ics_platform/db.py` — Users, AuthSessions, Courses, Modules, Questions, Progress, TimingSessions, TelemetryLogs. |
| **D2. Engagement hook** | `frontend/js/engagement.js` — `EngagementTracker` (React/Vue-hook equivalent). Heartbeat every 5 s, pause on 30 s idle / hidden. |
| **D3. APIs** | `ics_platform/server.py` — auth, content, progress, `sessions/start`, `telemetry/heartbeat`, `lecture/complete`, `quiz/submit`, admin reset/telemetry. |
| **D4. UI** | `frontend/` SPA — dashboard + progress bar, lecture view with embedded verified timer, quiz runner with post-submit score + per-question explanations. |

## API map

```
POST /api/auth/register {username,email,password} -> 201 + user
POST /api/auth/login {username,password} -> 200 + user (HttpOnly cookie)
POST /api/auth/logout
GET  /api/auth/me
GET  /api/modules                         -> [...] + unlocked flags + progress_pct
GET  /api/modules/:id                     -> lecture (403 if locked, body withheld)
GET  /api/modules/:id/quiz                -> questions WITHOUT answers (409 until lecture verified)
POST /api/sessions/start {module_id,kind:lecture|quiz} -> session_id + tmin/tmax + idle threshold
POST /api/telemetry/heartbeat {session_id,active_seconds,wall_seconds,mouse_moves,scrolls,keys,focus_events}
                                        -> {verdict, server_active_s, server_wall_s, remaining_s, timed_out}
POST /api/lecture/complete {session_id} -> verified | 409 need_more_time | 409 timeout
POST /api/quiz/submit {session_id,answers:{qid:"a"}} -> {score,passed,details:[explanations],next_unlocked}
GET  /api/progress/me
GET  /api/admin/users                     (admin)
POST /api/admin/users/:id/reset           (admin)
GET  /api/admin/telemetry?limit=100       (admin)
GET  /api/health
```

Errors are JSON: `{error, message, ...}` — `need_more_time` includes
`server_active_s`, `tmin_s`, `remaining_s`; `timeout` includes both counters.

## Timer rules (exact)

- **Idle window:** 30 s without a valid mouse/scroll/key event, or tab blur/hidden → timer *paused* client-side; server awards 0 active for heartbeat gaps > 30 s. Adjustable server-side via `IDLE_THRESHOLD_S` in `server.py` and per-session via `idle_threshold_s`.
- **Tmin:** lecture completion requires `server_active_s ≥ tmin_s` (90–120 s seeded; production raise to 300–600 s). Quiz submit requires `server_wall_s ≥ quiz_tmin_s` (30 s) to block instant-bot submits.
- **Tmax:** `server_wall_s > tmax_s` → session marked `timeout`; the page shows a restart control and a fresh session is required. Background tabs cannot extend walls — wall-clock is server-measured.
- **Heartbeat cadence:** 5 s. Every heartbeat is persisted (telemetry logs), so the admin view shows exactly which intervals were clamped/rejected and why.

## Curriculum

1. **Foundations of Networking for OT** — IP/TCP/UDP, VLANs, NAT, Purdue model (90 s min)
2. **Industrial Protocols: Modbus & DNP3** — function codes, Secure DNP3, abuse patterns (90 s min)
3. **PLC, SCADA & Controller Architectures** — scan cycle, HMI/historian/EWS, SIS independence (120 s min)
4. **ICS Threat Vectors & Defensive Fundamentals** — TRITON/Ukraine/Oldsmar, ATT&CK for ICS, monitoring & response (120 s min)

24 scenario-based questions (6 per module) with per-question explanations.

## Configuration

```bash
# production-ish thresholds (example: 5 min min / 20 min max)
python3 - <<'PY'
import sqlite3; c=sqlite3.connect("ics_platform.db")
c.execute("UPDATE modules SET tmin_s=300, tmax_s=1200 WHERE id IN ('mod-01','mod-02')")
c.execute("UPDATE modules SET tmin_s=600, tmax_s=1800 WHERE id IN ('mod-03','mod-04')")
c.commit()
PY
ICS_DB=/path/to.db python3 run.py --port 8000
```

## Tests

```bash
python3 -m unittest tests.test_platform -v   # ~12s; spins up an ephemeral server
```

Covers locking, Tmin enforcement, clamped-inflation anti-cheat, monotonicity
rejection, quiz grading + unlock, instant-quiz rejection, RBAC, duplicate
registration and bad credentials.

## Project layout

```
ics-platform/
  run.py                    # entry point
  schema.sql                # DDL (deliverable D1)
  ics_platform/
    db.py                   # schema init + seed curriculum + progress helpers (D1)
    server.py               # REST API + timer verification + RBAC (D3)
  frontend/
    index.html
    css/style.css
    js/api.js
    js/engagement.js        # engagement/timer hook (D2)
    js/app.js               # dashboard / lecture / quiz / admin views (D4)
  tests/test_platform.py
```
