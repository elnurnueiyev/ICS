"""Backend: REST API + static SPA server (stdlib only).

Security model
--------------
- Passwords: PBKDF2-HMAC-SHA256. Sessions: opaque 32-byte tokens in HttpOnly cookies.
- RBAC: role in {user, admin}. Admin routes check role.
- Timer integrity: client claims are never trusted blindly. The server recomputes
  active time from heartbeat deltas: active_delta = min(claimed_delta, wall_delta),
  and any heartbeat gap > IDLE_THRESHOLD_S is treated as idle (not counted active).
  A heartbeat claiming more active than wall is clamped + logged as 'clamped'.
- Tmin/Tmax enforced server-side on /lecture/complete and /quiz/submit.
  wall > Tmax => status 'timeout' (section must be restarted with a new session).
"""
from __future__ import annotations

import json
import mimetypes
import re
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import db

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
IDLE_THRESHOLD_S = 30          # 30-60s window per spec; 30 is the strict default
HEARTBEAT_GRACE_S = 2          # clock-skew tolerance on claimed deltas
SESSION_TTL_SECONDS = 12 * 3600
MAX_ACTIVE_PER_HEARTBEAT_S = 60  # sanity cap for absurd claims


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}


def public_user(row) -> dict:
    return {"id": row["id"], "username": row["username"], "email": row["email"], "role": row["role"]}


class App:
    """Framework-free request router + business logic (unit-testable without HTTP)."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or db.db_path()

    def conn(self):
        return db.connect(self.db_path)

    # ---- auth helpers
    def current_user(self, token: str | None) -> dict | None:
        if not token:
            return None
        conn = self.conn()
        try:
            row = conn.execute(
                """SELECT u.* FROM auth_sessions s JOIN users u ON u.id=s.user_id
                   WHERE s.token=? AND s.expires_at > ?""",
                (token, _iso(_now())),
            ).fetchone()
            return public_user(row) if row else None
        finally:
            conn.close()

    def require_user(self, token) -> dict:
        u = self.current_user(token)
        if not u:
            raise ApiError(401, "unauthorized", "Login required")
        return u

    def require_admin(self, token) -> dict:
        u = self.require_user(token)
        if u["role"] != "admin":
            raise ApiError(403, "forbidden", "Admin role required")
        return u

    # ---- auth endpoints
    def register(self, body: dict) -> dict:
        username = str(body.get("username", "")).strip()
        email = str(body.get("email", "")).strip().lower()
        pw = str(body.get("password", ""))
        if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", username):
            raise ApiError(400, "invalid_input", "Username must be 3-32 chars [a-z0-9_]")
        if "@" not in email or len(email) > 120:
            raise ApiError(400, "invalid_input", "Valid email required")
        if len(pw) < 8:
            raise ApiError(400, "invalid_input", "Password must be at least 8 characters")
        conn = self.conn()
        try:
            if conn.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone():
                raise ApiError(409, "conflict", "Username already taken")
            if conn.execute("SELECT 1 FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone():
                raise ApiError(409, "conflict", "Email already registered")
            cur = conn.execute(
                "INSERT INTO users (username, email, pw_hash, role) VALUES (?,?,?,'user')",
                (username, email, db.hash_password(pw)),
            )
            uid = cur.lastrowid
            conn.commit()
            token = self._new_session(conn, uid)
            row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            return {"user": public_user(row), "token": token}
        finally:
            conn.close()

    def login(self, body: dict) -> dict:
        ident = str(body.get("username") or body.get("email") or "").strip()
        pw = str(body.get("password", ""))
        if not ident or not pw:
            raise ApiError(400, "invalid_input", "Username/email and password required")
        conn = self.conn()
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE username=? COLLATE NOCASE OR email=? COLLATE NOCASE",
                (ident, ident),
            ).fetchone()
            if not row or not db.verify_password(pw, row["pw_hash"]):
                raise ApiError(401, "bad_credentials", "Invalid credentials")
            conn.execute("UPDATE users SET last_login=? WHERE id=?", (_iso(_now()), row["id"]))
            token = self._new_session(conn, row["id"])
            conn.commit()
            return {"user": public_user(row), "token": token}
        finally:
            conn.close()

    def _new_session(self, conn, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        exp = _iso(_now() + timedelta(seconds=SESSION_TTL_SECONDS))
        conn.execute(
            "INSERT INTO auth_sessions (token, user_id, expires_at) VALUES (?,?,?)",
            (token, user_id, exp),
        )
        conn.commit()
        return token

    def logout(self, token: str | None):
        if token:
            conn = self.conn()
            try:
                conn.execute("DELETE FROM auth_sessions WHERE token=?", (token,))
                conn.commit()
            finally:
                conn.close()
        return {"ok": True}

    # ---- curriculum / progress
    def list_modules(self, token) -> dict:
        user = self.require_user(token)
        conn = self.conn()
        try:
            mods = db.list_modules(conn)
            total = len(mods)
            done = db.completed_count(conn, user["id"])
            out = []
            for m in mods:
                unlocked = db.is_unlocked(conn, user["id"], m)
                prow = conn.execute(
                    "SELECT * FROM progress WHERE user_id=? AND module_id=?", (user["id"], m["id"])
                ).fetchone()
                out.append({
                    "id": m["id"], "title": m["title"], "description": m["description"], "ord": m["ord"],
                    "tmin_s": m["tmin_s"], "tmax_s": m["tmax_s"], "quiz_tmin_s": m["quiz_tmin_s"],
                    "quiz_tmax_s": m["quiz_tmax_s"], "pass_score": m["pass_score"],
                    "unlocked": unlocked,
                    "lecture_verified": bool(prow and prow["lecture_verified"]),
                    "quiz_passed": bool(prow and prow["quiz_passed"]),
                    "best_score": prow["best_score"] if prow else 0,
                    "attempts": prow["attempts"] if prow else 0,
                    "completed": bool(prow and prow["lecture_verified"] and prow["quiz_passed"]),
                })
            return {"modules": out, "completed": done, "total": total,
                    "progress_pct": round(100 * done / total) if total else 0}
        finally:
            conn.close()

    def get_module(self, token, module_id: str) -> dict:
        user = self.require_user(token)
        conn = self.conn()
        try:
            m = db.get_module(conn, module_id)
            if not m:
                raise ApiError(404, "not_found", "Module not found")
            if not db.is_unlocked(conn, user["id"], m):
                raise ApiError(403, "locked", "Complete the previous module to unlock this one")
            # ensure progress row exists once unlocked
            db.ensure_progress_row(conn, user["id"], m["id"])
            conn.commit()
            prow = conn.execute(
                "SELECT * FROM progress WHERE user_id=? AND module_id=?", (user["id"], m["id"])
            ).fetchone()
            return {
                "id": m["id"], "title": m["title"], "description": m["description"], "ord": m["ord"],
                "tmin_s": m["tmin_s"], "tmax_s": m["tmax_s"], "quiz_tmin_s": m["quiz_tmin_s"],
                "quiz_tmax_s": m["quiz_tmax_s"], "pass_score": m["pass_score"],
                "lecture_body": m["lecture_body"],
                "lecture_verified": bool(prow and prow["lecture_verified"]),
                "quiz_passed": bool(prow and prow["quiz_passed"]),
            }
        finally:
            conn.close()

    def get_quiz(self, token, module_id: str) -> dict:
        user = self.require_user(token)
        conn = self.conn()
        try:
            m = db.get_module(conn, module_id)
            if not m:
                raise ApiError(404, "not_found", "Module not found")
            if not db.is_unlocked(conn, user["id"], m):
                raise ApiError(403, "locked", "Module is locked")
            prow = conn.execute(
                "SELECT * FROM progress WHERE user_id=? AND module_id=?", (user["id"], m["id"])
            ).fetchone()
            if not prow or not prow["lecture_verified"]:
                raise ApiError(409, "lecture_required", "Verify the lecture before taking the quiz")
            rows = conn.execute(
                "SELECT * FROM questions WHERE module_id=? ORDER BY ord", (module_id,)
            ).fetchall()
            # Never send correct answers before submission.
            questions = [{
                "id": r["id"], "ord": r["ord"], "q_text": r["q_text"],
                "options": json.loads(r["options_json"]),
            } for r in rows]
            return {"module_id": module_id, "title": m["title"], "pass_score": m["pass_score"],
                    "quiz_tmin_s": m["quiz_tmin_s"], "quiz_tmax_s": m["quiz_tmax_s"],
                    "questions": questions, "already_passed": bool(prow["quiz_passed"])}
        finally:
            conn.close()

    # ---- sessions, telemetry, timers
    def start_session(self, token, body: dict) -> dict:
        user = self.require_user(token)
        module_id = str(body.get("module_id", ""))
        kind = str(body.get("kind", ""))
        if kind not in ("lecture", "quiz"):
            raise ApiError(400, "invalid_input", "kind must be 'lecture' or 'quiz'")
        conn = self.conn()
        try:
            m = db.get_module(conn, module_id)
            if not m:
                raise ApiError(404, "not_found", "Module not found")
            if not db.is_unlocked(conn, user["id"], m):
                raise ApiError(403, "locked", "Module is locked")
            db.ensure_progress_row(conn, user["id"], m["id"])
            if kind == "quiz":
                prow = conn.execute(
                    "SELECT * FROM progress WHERE user_id=? AND module_id=?", (user["id"], m["id"])
                ).fetchone()
                if not prow or not prow["lecture_verified"]:
                    raise ApiError(409, "lecture_required", "Verify the lecture before starting the quiz")
            conn.commit()
            sid = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO timing_sessions (id, user_id, module_id, kind, status, started_at, last_heartbeat_at)
                   VALUES (?,?,?,?,'active',?,?)""",
                (sid, user["id"], m["id"], kind, _iso(_now()), _iso(_now())),
            )
            conn.commit()
            tmin = m["tmin_s"] if kind == "lecture" else m["quiz_tmin_s"]
            tmax = m["tmax_s"] if kind == "lecture" else m["quiz_tmax_s"]
            return {"session_id": sid, "kind": kind, "module_id": m["id"],
                    "server_time": _iso(_now()), "tmin_s": tmin, "tmax_s": tmax,
                    "idle_threshold_s": IDLE_THRESHOLD_S}
        finally:
            conn.close()

    def heartbeat(self, token, body: dict) -> dict:
        """Client reports counters; server returns the *trusted* counters.

        Trusted wall = last_heartbeat - started (monotonic by construction).
        Trusted active delta = min(claimed_delta, wall_delta, MAX_ACTIVE_PER_HEARTBEAT)
        unless the heartbeat gap exceeds IDLE_THRESHOLD_S, in which case active
        delta is forced to 0 for that interval (idle window). Inflated claims
        are clamped and logged; absurd claims are rejected.
        """
        user = self.require_user(token)
        sid = str(body.get("session_id", ""))
        claimed_active = _int(body.get("active_seconds"))
        claimed_wall = _int(body.get("wall_seconds"))
        if not sid or claimed_active is None or claimed_wall is None:
            raise ApiError(400, "invalid_input", "session_id, active_seconds, wall_seconds required")
        if claimed_active < 0 or claimed_wall < 0 or claimed_wall < claimed_active:
            raise ApiError(400, "invalid_input", "wall_seconds must be >= active_seconds >= 0")
        conn = self.conn()
        try:
            s = conn.execute("SELECT * FROM timing_sessions WHERE id=?", (sid,)).fetchone()
            if not s or s["user_id"] != user["id"]:
                raise ApiError(404, "not_found", "Session not found")
            if s["status"] != "active":
                raise ApiError(409, "session_closed", f"Session is {s['status']}; start a new one")
            now = _now()
            started = _parse_iso(s["started_at"]) or now
            last_hb = _parse_iso(s["last_heartbeat_at"]) or started
            server_wall = max(0, int((now - started).total_seconds()))
            gap_s = max(0, int((now - last_hb).total_seconds()))
            claim_d_active = claimed_active - int(s["last_claimed_active_s"])
            claim_d_wall = claimed_wall - int(s["last_claimed_wall_s"])
            if claim_d_active < 0 or claim_d_wall < 0:
                verdict = "rejected"
                raise ApiError(400, "rejected", "Counters must be monotonic",
                               {"verdict": verdict})
            # Inflated claims are clamped (not rejected) below: the client loses
            # the excess but keeps its session; only protocol violations
            # (non-monotonic, wall<active) are rejected outright.
            if gap_s > IDLE_THRESHOLD_S:
                # Long gap: client was away; do not count this interval as active.
                delta_active = 0
                verdict = "clamped" if claim_d_active > 0 else "accepted"
            else:
                delta_active = min(claim_d_active, gap_s + HEARTBEAT_GRACE_S, MAX_ACTIVE_PER_HEARTBEAT_S)
                delta_active = max(0, delta_active)
                verdict = "clamped" if claim_d_active > delta_active else "accepted"
                if claim_d_active > delta_active and claim_d_active > gap_s + HEARTBEAT_GRACE_S + 1:
                    verdict = "clamped"
            server_active = int(s["server_active_seconds"]) + int(delta_active)
            conn.execute(
                """UPDATE timing_sessions
                   SET server_active_seconds=?, server_wall_seconds=?, last_heartbeat_at=?,
                       last_claimed_active_s=?, last_claimed_wall_s=?,
                       mouse_moves=?, scrolls=?, keys=?, focus_events=?,
                       clamped_count = clamped_count + ?
                   WHERE id=?""",
                (server_active, server_wall, _iso(now), claimed_active, claimed_wall,
                 _int(body.get("mouse_moves"), 0) or 0, _int(body.get("scrolls"), 0) or 0,
                 _int(body.get("keys"), 0) or 0, _int(body.get("focus_events"), 0) or 0,
                 1 if verdict == "clamped" else 0, sid),
            )
            self._log_telemetry(conn, s, claimed_active, claimed_wall, server_active, server_wall, body, verdict)
            conn.commit()
            m = db.get_module(conn, s["module_id"])
            tmax = (m["tmax_s"] if s["kind"] == "lecture" else m["quiz_tmax_s"]) if m else 0
            remaining = max(0, tmax - server_wall) if tmax else 0
            timed_out = bool(tmax and server_wall > tmax)
            return {"session_id": sid, "verdict": verdict,
                    "server_active_s": server_active, "server_wall_s": server_wall,
                    "remaining_s": remaining, "timed_out": timed_out,
                    "idle_threshold_s": IDLE_THRESHOLD_S}
        finally:
            conn.close()

    def _log_telemetry(self, conn, sess, ca, cw, sa, sw, body, verdict):
        conn.execute(
            """INSERT INTO telemetry_logs
               (session_id, user_id, claimed_active_s, claimed_wall_s, server_active_s, server_wall_s,
                mouse_moves, scrolls, keys, focus_events, verdict)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (sess["id"], sess["user_id"], ca, cw, sa, sw,
             _int(body.get("mouse_moves"), 0) or 0, _int(body.get("scrolls"), 0) or 0,
             _int(body.get("keys"), 0) or 0, _int(body.get("focus_events"), 0) or 0, verdict),
        )

    def complete_lecture(self, token, body: dict) -> dict:
        user = self.require_user(token)
        sid = str(body.get("session_id", ""))
        conn = self.conn()
        try:
            s = conn.execute("SELECT * FROM timing_sessions WHERE id=?", (sid,)).fetchone()
            if not s or s["user_id"] != user["id"] or s["kind"] != "lecture":
                raise ApiError(404, "not_found", "Lecture session not found")
            if s["status"] != "active":
                raise ApiError(409, "session_closed", f"Session is {s['status']}; start a new one")
            m = db.get_module(conn, s["module_id"])
            if not m:
                raise ApiError(404, "not_found", "Module not found")
            active, wall = int(s["server_active_seconds"]), int(s["server_wall_seconds"])
            if wall > int(m["tmax_s"]):
                conn.execute("UPDATE timing_sessions SET status='timeout', ended_at=? WHERE id=?",
                             (_iso(_now()), sid))
                conn.commit()
                raise ApiError(409, "timeout", "Session exceeded Tmax; this section must be restarted",
                               {"server_active_s": active, "server_wall_s": wall,
                                "tmin_s": m["tmin_s"], "tmax_s": m["tmax_s"]})
            if active < int(m["tmin_s"]):
                raise ApiError(409, "need_more_time",
                               f"Active reading time not yet verified: {active}/{m['tmin_s']}s",
                               {"server_active_s": active, "server_wall_s": wall,
                                "tmin_s": m["tmin_s"], "tmax_s": m["tmax_s"],
                                "remaining_s": int(m["tmin_s"]) - active})
            if wall - active > int(m["tmax_s"]) * 2:  # pathological idle abuse guard
                raise ApiError(409, "need_active_time", "Too much idle time in this session; restart",
                               {"server_active_s": active, "server_wall_s": wall})
            conn.execute("UPDATE timing_sessions SET status='verified', ended_at=? WHERE id=?",
                         (_iso(_now()), sid))
            db.ensure_progress_row(conn, user["id"], m["id"])
            conn.execute(
                """UPDATE progress SET lecture_verified=1, lecture_active_seconds=?,
                   lecture_wall_seconds=? WHERE user_id=? AND module_id=?""",
                (active, wall, user["id"], m["id"]),
            )
            conn.commit()
            return {"ok": True, "module_id": m["id"], "server_active_s": active,
                    "server_wall_s": wall, "next": "quiz"}
        finally:
            conn.close()

    def submit_quiz(self, token, body: dict) -> dict:
        user = self.require_user(token)
        sid = str(body.get("session_id", ""))
        answers_raw = body.get("answers", {})
        if not isinstance(answers_raw, dict):
            raise ApiError(400, "invalid_input", "answers must be an object {question_id: key}")
        conn = self.conn()
        try:
            s = conn.execute("SELECT * FROM timing_sessions WHERE id=?", (sid,)).fetchone()
            if not s or s["user_id"] != user["id"] or s["kind"] != "quiz":
                raise ApiError(404, "not_found", "Quiz session not found")
            if s["status"] != "active":
                raise ApiError(409, "session_closed", f"Session is {s['status']}; start a new one")
            m = db.get_module(conn, s["module_id"])
            if not m:
                raise ApiError(404, "not_found", "Module not found")
            prow = conn.execute(
                "SELECT * FROM progress WHERE user_id=? AND module_id=?", (user["id"], m["id"])
            ).fetchone()
            if not prow or not prow["lecture_verified"]:
                raise ApiError(409, "lecture_required", "Verify the lecture before submitting the quiz")
            active, wall = int(s["server_active_seconds"]), int(s["server_wall_seconds"])
            # Quiz timing: min dwell prevents instant-bot submit; max bounds wall.
            if wall > int(m["quiz_tmax_s"]):
                conn.execute("UPDATE timing_sessions SET status='timeout', ended_at=? WHERE id=?",
                             (_iso(_now()), sid))
                conn.commit()
                raise ApiError(409, "timeout", "Quiz exceeded Tmax; restart the quiz",
                               {"server_wall_s": wall, "quiz_tmin_s": m["quiz_tmin_s"],
                                "quiz_tmax_s": m["quiz_tmax_s"]})
            if wall < int(m["quiz_tmin_s"]):
                raise ApiError(409, "need_more_time", f"Quiz submitted too quickly: {wall}/{m['quiz_tmin_s']}s",
                               {"server_wall_s": wall, "quiz_tmin_s": m["quiz_tmin_s"]})
            rows = conn.execute("SELECT * FROM questions WHERE module_id=? ORDER BY ord",
                                (m["id"],)).fetchall()
            if not rows:
                raise ApiError(500, "server_error", "No questions configured")
            details = []
            correct = 0
            for r in rows:
                submitted = str(answers_raw.get(str(r["id"]), answers_raw.get(r["id"], ""))).lower()
                ok = submitted == r["correct_key"]
                correct += 1 if ok else 0
                details.append({"question_id": r["id"], "ord": r["ord"], "q_text": r["q_text"],
                                "your_answer": submitted or None, "correct_answer": r["correct_key"],
                                "correct": ok, "explanation": r["explanation"]})
            total = len(rows)
            score = round(100 * correct / total)
            passed = score >= int(m["pass_score"])
            attempts = int(prow["attempts"]) + 1
            best = max(int(prow["best_score"]), score)
            conn.execute("UPDATE timing_sessions SET status='completed', ended_at=? WHERE id=?",
                         (_iso(_now()), sid))
            conn.execute(
                """UPDATE progress SET attempts=?, best_score=?,
                   quiz_passed=?, completed_at=COALESCE(completed_at, ?)
                   WHERE user_id=? AND module_id=?""",
                (attempts, best, 1 if passed else 0,
                 _iso(_now()) if passed else None, user["id"], m["id"]),
            )
            next_unlocked = None
            if passed:
                nxt = db.module_by_ord(conn, int(m["ord"]) + 1)
                if nxt:
                    db.ensure_progress_row(conn, user["id"], nxt["id"])
                    next_unlocked = nxt["id"]
            conn.commit()
            return {"score": score, "passed": passed, "pass_score": m["pass_score"],
                    "correct": correct, "total": total, "attempts": attempts,
                    "best_score": best, "next_unlocked": next_unlocked,
                    "details": details}
        finally:
            conn.close()

    def my_progress(self, token) -> dict:
        self.require_user(token)  # noqa: F841 (auth check)
        return self.list_modules(token)

    # ---- admin
    def admin_users(self, token) -> dict:
        self.require_admin(token)
        conn = self.conn()
        try:
            rows = conn.execute(
                """SELECT u.id, u.username, u.email, u.role, u.created_at, u.last_login,
                          COUNT(CASE WHEN p.lecture_verified=1 AND p.quiz_passed=1 THEN 1 END) AS completed,
                          COALESCE(SUM(p.lecture_active_seconds),0) AS active_seconds
                   FROM users u LEFT JOIN progress p ON p.user_id=u.id
                   GROUP BY u.id ORDER BY u.id"""
            ).fetchall()
            return {"users": [dict(r) for r in rows]}
        finally:
            conn.close()

    def admin_reset(self, token, user_id: int) -> dict:
        self.require_admin(token)
        conn = self.conn()
        try:
            if user_id == 1:
                raise ApiError(400, "invalid_input", "Cannot reset the seeded admin")
            found = conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone()
            if not found:
                raise ApiError(404, "not_found", "User not found")
            conn.execute("DELETE FROM progress WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM telemetry_logs WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM timing_sessions WHERE user_id=?", (user_id,))
            conn.commit()
            return {"ok": True, "user_id": user_id}
        finally:
            conn.close()

    def admin_telemetry(self, token, limit: int = 100) -> dict:
        self.require_admin(token)
        conn = self.conn()
        try:
            rows = conn.execute(
                """SELECT t.id, t.ts, t.session_id, t.user_id, u.username,
                          t.claimed_active_s, t.claimed_wall_s, t.server_active_s, t.server_wall_s,
                          t.mouse_moves, t.scrolls, t.keys, t.focus_events, t.verdict
                   FROM telemetry_logs t JOIN users u ON u.id=t.user_id
                   ORDER BY t.id DESC LIMIT ?""",
                (min(max(limit, 1), 500),),
            ).fetchall()
            stats = conn.execute(
                """SELECT COUNT(*) total, SUM(CASE WHEN verdict='clamped' THEN 1 ELSE 0 END) clamped,
                          SUM(CASE WHEN verdict='rejected' THEN 1 ELSE 0 END) rejected
                   FROM telemetry_logs"""
            ).fetchone()
            return {"logs": [dict(r) for r in rows],
                    "summary": {"total": stats["total"], "clamped": stats["clamped"] or 0,
                                "rejected": stats["rejected"] or 0}}
        finally:
            conn.close()


def _int(v, default=None):
    try:
        if v is None or isinstance(v, bool):
            return default
        return int(v)
    except Exception:
        return default


# --------------------------------------------------------------- HTTP layer
def make_handler(app: App, frontend_dir: Path = FRONTEND_DIR):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ICSPlatform/1.0"

        def log_message(self, fmt, *args):  # quiet; uncomment for debugging
            pass

        def _token(self) -> str | None:
            raw = self.headers.get("Cookie", "")
            jar = cookies.SimpleCookie(raw)
            morsel = jar.get("ics_session")
            return morsel.value if morsel else None

        def _send_json(self, status: int, payload: dict, set_cookie: str | None = None, clear_cookie: bool = False):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*") or "*")
            self.send_header("Access-Control-Allow-Credentials", "true")
            if set_cookie:
                self.send_header("Set-Cookie",
                                 f"ics_session={set_cookie}; HttpOnly; Path=/; SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}")
            if clear_cookie:
                self.send_header("Set-Cookie", "ics_session=; HttpOnly; Path=/; Max-Age=0")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
                return data if isinstance(data, dict) else {}
            except Exception:
                raise ApiError(400, "invalid_json", "Request body must be valid JSON")

        def _set_cookie(self, token: str):
            self.send_header("Set-Cookie",
                             f"ics_session={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}")

        def _clear_cookie(self):
            self.send_header("Set-Cookie", "ics_session=; HttpOnly; Path=/; Max-Age=0")

        def _api(self, method: str, path: str):
            token = self._token()
            try:
                if path == "/api/health" and method == "GET":
                    return 200, {"ok": True, "service": "ics-platform"}, False
                if path == "/api/auth/register" and method == "POST":
                    out = app.register(self._read_json())
                    return 201, out, True
                if path == "/api/auth/login" and method == "POST":
                    return 200, app.login(self._read_json()), True
                if path == "/api/auth/logout" and method == "POST":
                    return 200, app.logout(token), False
                if path == "/api/auth/me" and method == "GET":
                    return 200, {"user": app.require_user(token)}, False
                if path == "/api/modules" and method == "GET":
                    return 200, app.list_modules(token), False
                m = re.match(r"^/api/modules/([\w\-]+)$", path)
                if m and method == "GET":
                    return 200, app.get_module(token, m.group(1)), False
                m = re.match(r"^/api/modules/([\w\-]+)/quiz$", path)
                if m and method == "GET":
                    return 200, app.get_quiz(token, m.group(1)), False
                if path == "/api/sessions/start" and method == "POST":
                    return 201, app.start_session(token, self._read_json()), False
                if path == "/api/telemetry/heartbeat" and method == "POST":
                    return 200, app.heartbeat(token, self._read_json()), False
                if path == "/api/lecture/complete" and method == "POST":
                    return 200, app.complete_lecture(token, self._read_json()), False
                if path == "/api/quiz/submit" and method == "POST":
                    return 200, app.submit_quiz(token, self._read_json()), False
                if path == "/api/progress/me" and method == "GET":
                    return 200, app.my_progress(token), False
                if path == "/api/admin/users" and method == "GET":
                    return 200, app.admin_users(token), False
                m = re.match(r"^/api/admin/users/(\d+)/reset$", path)
                if m and method == "POST":
                    return 200, app.admin_reset(token, int(m.group(1))), False
                if path == "/api/admin/telemetry" and method == "GET":
                    qs = urlparse(self.path).query
                    limit = 100
                    mm = re.search(r"limit=(\d+)", qs)
                    if mm:
                        limit = int(mm.group(1))
                    return 200, app.admin_telemetry(token, limit), False
                raise ApiError(404, "not_found", f"No route for {method} {path}")
            except ApiError as e:
                return e.status, {"error": e.code, "message": e.message, **e.extra}, (e.status == 201)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                status, payload, is_auth = self._api("GET", parsed.path)
                self._send_json(status, payload)
                return
            self._static(parsed.path)

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                status, payload, is_auth = self._api("POST", parsed.path)
                tok = payload.get("token") if isinstance(payload, dict) else None
                safe = {k: v for k, v in payload.items() if k != "token"}  # never echo token in body
                if tok and parsed.path in ("/api/auth/register", "/api/auth/login"):
                    self._send_json(status, safe, set_cookie=tok)
                elif parsed.path == "/api/auth/logout":
                    self._send_json(status, safe, clear_cookie=True)
                else:
                    self._send_json(status, safe)
                return
            self._send_json(404, {"error": "not_found", "message": "Unknown path"})

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*") or "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def _static(self, path: str):
            rel = path.lstrip("/") or "index.html"
            target = (frontend_dir / rel).resolve()
            if not str(target).startswith(str(frontend_dir.resolve())) or not target.is_file():
                target = frontend_dir / "index.html"
            mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") or "javascript" in mime or mime == "application/json" else ""))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8000, db_path: str | None = None):
    db.init_db(db_path)
    app = App(db_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"ICS Platform on http://{host}:{port}  (db: {db_path or db.db_path()})")
    print("demo accounts -> user: demo / demo123   admin: admin / admin123")
    httpd.serve_forever()
