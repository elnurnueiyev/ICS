"""End-to-end tests via HTTP against an ephemeral server (stdlib only).

Run: python3 -m unittest tests.test_platform -v
Covers: auth + RBAC, progressive locking, server-verified Tmin/Tmax,
clamp/reject anti-cheat, quiz grading + unlock, timeout restart.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ics_platform import db
from ics_platform.server import App, make_handler

BASE = None
_opener_cookies = {}


def _req(method, path, body=None, cookie=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = f"ics_session={cookie}"
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode())
            set_cookie = resp.headers.get("Set-Cookie") or ""
            tok = None
            if "ics_session=" in set_cookie:
                tok = set_cookie.split("ics_session=")[1].split(";")[0]
            return resp.status, payload, tok
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw}
        set_cookie = e.headers.get("Set-Cookie") or ""
        tok = set_cookie.split("ics_session=")[1].split(";")[0] if "ics_session=" in set_cookie else None
        return e.code, payload, tok


class PlatformTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "test.db")
        db.init_db(cls.db_path)
        # Shrink timers for fast tests: lecture tmin 3s / tmax 120s, quiz tmin 2s.
        conn = db.connect(cls.db_path)
        conn.execute("UPDATE modules SET tmin_s=3, tmax_s=120, quiz_tmin_s=2, quiz_tmax_s=120")
        conn.commit()
        conn.close()
        app = App(cls.db_path)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        cls.httpd = httpd
        cls.thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        cls.thread.start()
        global BASE
        BASE = f"http://127.0.0.1:{httpd.server_address[1]}"
        # fresh users per test class
        status, payload, tok = _req("POST", "/api/auth/register", {"username": "alice", "email": "alice@example.com", "password": "password123"})
        assert status == 201, payload
        cls.alice = tok
        status, payload, tok = _req("POST", "/api/auth/login", {"username": "demo", "password": "demo123"})
        assert status == 200, payload
        cls.demo = payload.get("token") or tok
        status, payload, tok = _req("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
        assert status == 200, payload
        cls.admin = payload.get("token") or tok

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def test_01_health_and_auth_required(self):
        status, body, _ = _req("GET", "/api/health")
        self.assertEqual(status, 200)
        status, _, _ = _req("GET", "/api/modules")
        self.assertEqual(status, 401)

    def test_02_progressive_locking(self):
        status, body, _ = _req("GET", "/api/modules", cookie=self.alice)
        self.assertEqual(status, 200)
        mods = body["modules"]
        self.assertEqual(len(mods), 4)
        self.assertTrue(mods[0]["unlocked"])
        self.assertFalse(mods[1]["unlocked"])
        # locked content is not leaked
        status, body, _ = _req("GET", "/api/modules/mod-02", cookie=self.alice)
        self.assertEqual(status, 403)

    def test_03_lecture_tmin_enforced_then_verified(self):
        status, s, _ = _req("POST", "/api/sessions/start", {"module_id": "mod-01", "kind": "lecture"}, cookie=self.alice)
        self.assertEqual(status, 201)
        sid = s["session_id"]
        self.assertEqual(s["tmin_s"], 3)
        # too early
        status, body, _ = _req("POST", "/api/lecture/complete", {"session_id": sid}, cookie=self.alice)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "need_more_time")
        # heartbeat over real time (~3s)
        for _ in range(4):
            time.sleep(1.05)
            st, hb, _ = _req("POST", "/api/telemetry/heartbeat",
                             {"session_id": sid, "active_seconds": 0, "wall_seconds": 0,
                              "mouse_moves": 5, "scrolls": 2, "keys": 1, "focus_events": 1}, cookie=self.alice)
            self.assertEqual(st, 200)
        # final claimed counters inflated? server values must be from wall-clock.
        # Our claimed zeros mean server_active stays 0 -> still need_more_time.
        # That is correct anti-cheat: claiming 0 cannot pass, claiming huge gets clamped.
        status, body, _ = _req("POST", "/api/lecture/complete", {"session_id": sid}, cookie=self.alice)
        self.assertEqual(status, 409)
        # new session with honest counters proportional to elapsed wall
        status, s2, _ = _req("POST", "/api/sessions/start", {"module_id": "mod-01", "kind": "lecture"}, cookie=self.alice)
        sid2 = s2["session_id"]
        time.sleep(3.2)
        st, hb, _ = _req("POST", "/api/telemetry/heartbeat",
                         {"session_id": sid2, "active_seconds": 3, "wall_seconds": 3,
                          "mouse_moves": 20, "scrolls": 5, "keys": 3, "focus_events": 2}, cookie=self.alice)
        self.assertEqual(st, 200)
        self.assertGreaterEqual(hb["server_active_s"], 2)
        status, body, _ = _req("POST", "/api/lecture/complete", {"session_id": sid2}, cookie=self.alice)
        self.assertEqual(status, 200)

    def test_04_quiz_grading_and_unlock(self):
        # fetch quiz (mod-01 lecture verified in test_03)
        status, quiz, _ = _req("GET", "/api/modules/mod-01/quiz", cookie=self.alice)
        self.assertEqual(status, 200)
        # correct answers must never be present pre-submit
        self.assertNotIn("correct_key", json.dumps(quiz))
        self.assertNotIn("correct_answer", json.dumps(quiz))
        # cheat: fabricate answers directly from DB (know key) then submit with honest timer
        conn = db.connect(self.__class__.db_path)
        rows = conn.execute("SELECT id, correct_key FROM questions WHERE module_id='mod-01' ORDER BY ord").fetchall()
        conn.close()
        answers = {str(r["id"]): r["correct_key"] for r in rows}
        status, s, _ = _req("POST", "/api/sessions/start", {"module_id": "mod-01", "kind": "quiz"}, cookie=self.alice)
        sid = s["session_id"]
        time.sleep(2.2)
        _req("POST", "/api/telemetry/heartbeat",
             {"session_id": sid, "active_seconds": 2, "wall_seconds": 2}, cookie=self.alice)
        status, body, _ = _req("POST", "/api/quiz/submit", {"session_id": sid, "answers": answers}, cookie=self.alice)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["score"], 80)
        self.assertTrue(body["passed"])
        self.assertEqual(body["next_unlocked"], "mod-02")
        # next module now unlocked
        status, mods, _ = _req("GET", "/api/modules", cookie=self.alice)
        by_id = {m["id"]: m for m in mods["modules"]}
        self.assertTrue(by_id["mod-02"]["unlocked"])

    def test_05_quiz_too_fast_rejected(self):
        # use demo (has not started mod-01) -> cannot even start quiz
        status, body, _ = _req("POST", "/api/sessions/start", {"module_id": "mod-01", "kind": "quiz"}, cookie=self.demo)
        self.assertEqual(status, 409)

    def test_06_inflated_claim_clamped(self):
        status, s, _ = _req("POST", "/api/sessions/start", {"module_id": "mod-02", "kind": "lecture"}, cookie=self.alice)
        sid = s["session_id"]
        time.sleep(1.2)
        # claim 9999s active in 1s wall -> clamped, server awards ~gap only
        status, hb, _ = _req("POST", "/api/telemetry/heartbeat",
                             {"session_id": sid, "active_seconds": 9999, "wall_seconds": 9999}, cookie=self.alice)
        self.assertEqual(status, 200)
        self.assertEqual(hb["verdict"], "clamped")
        self.assertLess(hb["server_active_s"], 10)
        # non-monotonic
        status, body, _ = _req("POST", "/api/telemetry/heartbeat",
                               {"session_id": sid, "active_seconds": 1, "wall_seconds": 1}, cookie=self.alice)
        self.assertEqual(status, 400)

    def test_07_rbac_and_admin(self):
        status, _, _ = _req("GET", "/api/admin/users", cookie=self.alice)
        self.assertEqual(status, 403)
        status, users, _ = _req("GET", "/api/admin/users", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertTrue(any(u["username"] == "admin" and u["role"] == "admin" for u in users["users"]))
        status, tele, _ = _req("GET", "/api/admin/telemetry", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("summary", tele)

    def test_08_register_login_bad_creds(self):
        status, _, _ = _req("POST", "/api/auth/register", {"username": "alice", "email": "x@example.com", "password": "password123"})
        self.assertEqual(status, 409)
        status, _, _ = _req("POST", "/api/auth/login", {"username": "alice", "password": "wrong"})
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
