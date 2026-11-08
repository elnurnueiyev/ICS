-- Interactive ICS/OT Cyber Security Educational Platform — Schema
-- SQLite. Owner: backend (app/db.py). See docs/ARCHITECTURE.md.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL UNIQUE CHECK (length(username) BETWEEN 3 AND 32),
    email       TEXT NOT NULL UNIQUE CHECK (email LIKE '%@%'),
    pw_hash     TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_login  TEXT
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT NOT NULL,
    ip          TEXT
);

CREATE TABLE IF NOT EXISTS courses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS modules (
    id          TEXT PRIMARY KEY,            -- e.g. 'mod-01'
    course_id   INTEGER NOT NULL REFERENCES courses(id),
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    ord         INTEGER NOT NULL UNIQUE,     -- learning-path order, 0-based
    tmin_s      INTEGER NOT NULL CHECK (tmin_s > 0),
    tmax_s      INTEGER NOT NULL CHECK (tmax_s > tmin_s),
    quiz_tmin_s INTEGER NOT NULL DEFAULT 30,
    quiz_tmax_s INTEGER NOT NULL DEFAULT 600,
    pass_score  INTEGER NOT NULL DEFAULT 80 CHECK (pass_score BETWEEN 0 AND 100),
    lecture_body TEXT NOT NULL DEFAULT ''   -- safe HTML, sanitized server-side on input (seed is trusted)
);

CREATE TABLE IF NOT EXISTS questions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id     TEXT NOT NULL REFERENCES modules(id) ON DELETE CASCADE,
    ord           INTEGER NOT NULL,
    q_text        TEXT NOT NULL,
    options_json  TEXT NOT NULL,            -- {"a":"...","b":"...","c":"...","d":"..."}
    correct_key   TEXT NOT NULL CHECK (correct_key IN ('a','b','c','d')),
    explanation   TEXT NOT NULL DEFAULT '',
    UNIQUE (module_id, ord)
);

-- One row per (user, module) once unlocked. lecture_verified + quiz_passed => completed/unlocks next.
CREATE TABLE IF NOT EXISTS progress (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                 INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    module_id               TEXT NOT NULL REFERENCES modules(id) ON DELETE CASCADE,
    unlocked_at             TEXT NOT NULL DEFAULT (datetime('now')),
    lecture_verified        INTEGER NOT NULL DEFAULT 0 CHECK (lecture_verified IN (0,1)),
    lecture_active_seconds  INTEGER NOT NULL DEFAULT 0,
    lecture_wall_seconds    INTEGER NOT NULL DEFAULT 0,
    quiz_passed             INTEGER NOT NULL DEFAULT 0 CHECK (quiz_passed IN (0,1)),
    best_score              INTEGER NOT NULL DEFAULT 0,
    attempts                INTEGER NOT NULL DEFAULT 0,
    completed_at            TEXT,
    UNIQUE (user_id, module_id)
);

-- Lecture/Quiz timing sessions. Server-trusted active time is recomputed from heartbeats,
-- never taken blindly from the client claim (see app/server.py :: handle_heartbeat).
CREATE TABLE IF NOT EXISTS timing_sessions (
    id                      TEXT PRIMARY KEY,  -- uuid
    user_id                 INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    module_id               TEXT NOT NULL REFERENCES modules(id) ON DELETE CASCADE,
    kind                    TEXT NOT NULL CHECK (kind IN ('lecture','quiz')),
    status                  TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active','verified','completed','timeout','abandoned')),
    started_at              TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at                TEXT,
    last_heartbeat_at       TEXT,
    server_active_seconds   INTEGER NOT NULL DEFAULT 0,
    server_wall_seconds     INTEGER NOT NULL DEFAULT 0,
    last_claimed_active_s   INTEGER NOT NULL DEFAULT 0,
    last_claimed_wall_s     INTEGER NOT NULL DEFAULT 0,
    mouse_moves             INTEGER NOT NULL DEFAULT 0,
    scrolls                 INTEGER NOT NULL DEFAULT 0,
    keys                    INTEGER NOT NULL DEFAULT 0,
    focus_events            INTEGER NOT NULL DEFAULT 0,
    clamped_count           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS telemetry_logs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT NOT NULL REFERENCES timing_sessions(id) ON DELETE CASCADE,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ts                  TEXT NOT NULL DEFAULT (datetime('now')),
    claimed_active_s    INTEGER NOT NULL,
    claimed_wall_s      INTEGER NOT NULL,
    server_active_s     INTEGER NOT NULL,
    server_wall_s       INTEGER NOT NULL,
    mouse_moves         INTEGER NOT NULL DEFAULT 0,
    scrolls             INTEGER NOT NULL DEFAULT 0,
    keys                INTEGER NOT NULL DEFAULT 0,
    focus_events        INTEGER NOT NULL DEFAULT 0,
    verdict             TEXT NOT NULL DEFAULT 'accepted' CHECK (verdict IN ('accepted','clamped','rejected'))
);

CREATE INDEX IF NOT EXISTS idx_progress_user ON progress(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON timing_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_telemetry_session ON telemetry_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_questions_module ON questions(module_id);
