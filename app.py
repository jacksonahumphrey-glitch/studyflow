# app.py — StudyFlow (FULL, UPDATED FREE/PRO LIMITS)

import psycopg2
import psycopg2.extras
import os
import re
import json
import sqlite3
import smtplib
import random
import ssl
from email.message import EmailMessage
from datetime import datetime, date, timezone
from functools import wraps
from typing import Any, Dict, Optional, Tuple, List

import certifi
import stripe
from dotenv import load_dotenv
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    jsonify,
    send_from_directory,
)
from werkzeug.security import generate_password_hash, check_password_hash

APP_NAME = "StudyFlow"

# ---------------------------
# Limits
# ---------------------------
FREE_LIMITS = {
    "classes": 3,
    "assignments": 10,
    "notes": 5,
    "flashcards": 5,
    "quizzes": 2,
}

PRO_LIMITS = {
    "classes": 100,
    "assignments": 500,
    "notes": 200,
    "flashcards": 300,
    "quizzes": 50,
}

# ---------------------------
# Load .env reliably
# ---------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_PATH, override=True)


def _env(k: str, default: str = "") -> str:
    return (os.getenv(k, default) or "").strip()


DB_PATH = os.path.join(BASE_DIR, "studyflow.db")
DATABASE_URL = _env("DATABASE_URL", "")
USE_POSTGRES = bool(DATABASE_URL)

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ---------------------------
# Stripe config
# ---------------------------
STRIPE_SECRET_KEY = _env("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = _env("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_PRICE_ID = _env("STRIPE_PRICE_ID", "")
BASE_URL = _env("BASE_URL", "http://127.0.0.1:5000").rstrip("/")
BYPASS_PRO = _env("BYPASS_PRO", "false").lower() == "true"

stripe.api_key = STRIPE_SECRET_KEY

# ---------------------------
# Flask app
# ---------------------------
app = Flask(__name__)
app.secret_key = _env("SECRET_KEY", _env("STUDYFLOW_SECRET", "dev_secret_change_me"))

print("ENV LOADED FROM:", ENV_PATH)
print("BASE_URL =", BASE_URL)
print("STRIPE CONFIGURED =", bool(STRIPE_SECRET_KEY and STRIPE_PRICE_ID))

# ---------------------------
# Email (SMTP) config
# ---------------------------
MAIL_HOST = _env("STUDYFLOW_MAIL_HOST", "smtp.gmail.com")
MAIL_PORT = int(_env("STUDYFLOW_MAIL_PORT", "587") or "587")
MAIL_USER = _env("STUDYFLOW_MAIL_USER", "")
MAIL_PASS = _env("STUDYFLOW_MAIL_PASS", "").replace(" ", "")
MAIL_FROM = _env("STUDYFLOW_MAIL_FROM", f"StudyFlow <{MAIL_USER}>") if MAIL_USER else ""
MAIL_ENABLED = bool(MAIL_USER and MAIL_PASS and MAIL_FROM)

print("=== StudyFlow MAIL CONFIG ===")
print("HOST =", MAIL_HOST)
print("PORT =", MAIL_PORT)
print("USER =", MAIL_USER)
print("PASS? =", bool(MAIL_PASS))
print("FROM =", MAIL_FROM)
print("ENABLED =", MAIL_ENABLED)
print("=============================")

# ---------------------------
# Time / utils
# ---------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_iso() -> str:
    return date.today().isoformat()


def clamp_int(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except Exception:
        return default
    return max(lo, min(hi, n))


def safe_text(s: Any, max_len: int = 200) -> str:
    s = "" if s is None else str(s)
    s = s.strip()
    return s[:max_len]


def safe_email(s: Any) -> str:
    return safe_text(s, 200).lower()


def is_valid_email(s: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s))


def allowed_upload_filename(name: str) -> bool:
    name = (name or "").lower()
    return name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))


def iso_from_unix(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return ""


def stripe_ready() -> bool:
    return bool(STRIPE_SECRET_KEY and STRIPE_PRICE_ID)


def parse_iso_date(value: str) -> Optional[date]:
    try:
        if not value:
            return None
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def days_until_due(due_date_str: str) -> Optional[int]:
    d = parse_iso_date(due_date_str)
    if not d:
        return None
    return (d - date.today()).days


def contains_banned_word(text):
    if not text:
        return False

    t = text.lower()

    banned = [
        "nigger",
        "nigga"
    ]

    return any(word in t for word in banned)

# ---------------------------
# DB helpers
# ---------------------------
POSTGRES_INSERT_ID_TABLES = {
    "users",
    "classes",
    "assignments",
    "events",
    "notes",
    "flashcards",
    "quizzes",
}


class PGResult:
    def __init__(self, cursor, lastrowid=None):
        self.cursor = cursor
        self.lastrowid = lastrowid

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()


class PGConnection:
    def __init__(self, dsn: str):
        self.conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
        self.conn.autocommit = False

    def execute(self, query: str, params=()):
        q = query.strip()
        q_upper = q.upper()

        # SQLite -> Postgres compatibility fixes
        q = q.replace(" COLLATE NOCASE", "")
        q = q.replace("?", "%s")

        # SQLite upsert used by daily_plan
        if q_upper.startswith("INSERT OR REPLACE INTO DAILY_PLAN"):
            q = """
            INSERT INTO daily_plan(user_id, day, payload_json, created_at)
            VALUES(%s, %s, %s, %s)
            ON CONFLICT (user_id, day)
            DO UPDATE SET
                payload_json = EXCLUDED.payload_json,
                created_at = EXCLUDED.created_at
            """

        wants_returning_id = False
        for table in POSTGRES_INSERT_ID_TABLES:
            pattern = rf"^\s*INSERT\s+INTO\s+{table}\b"
            if re.match(pattern, q, flags=re.IGNORECASE) and "RETURNING" not in q.upper():
                q = q.rstrip() + " RETURNING id"
                wants_returning_id = True
                break

        cur = self.conn.cursor()
        cur.execute(q, params or ())

        lastrowid = None
        if wants_returning_id:
            row = cur.fetchone()
            if row and "id" in row:
                lastrowid = int(row["id"])

        return PGResult(cur, lastrowid=lastrowid)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def db():
    if USE_POSTGRES:
        return PGConnection(DATABASE_URL)

    conn = sqlite3.connect(DB_PATH, timeout=8, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def _table_exists(conn, table: str) -> bool:
    if USE_POSTGRES:
        row = conn.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema='public' AND table_name=%s
            ) AS exists
            """,
            (table,),
        ).fetchone()
        return bool(row["exists"]) if row else False

    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _column_exists(conn, table: str, column: str) -> bool:
    if USE_POSTGRES:
        row = conn.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema='public'
                  AND table_name=%s
                  AND column_name=%s
            ) AS exists
            """,
            (table, column),
        ).fetchone()
        return bool(row["exists"]) if row else False

    if not _table_exists(conn, table):
        return False
    rows = conn.execute(f"PRAGMA table_info({table});").fetchall()
    cols = {r["name"] for r in rows}
    return column in cols


def _notes_text_cols(conn) -> Tuple[bool, bool]:
    if USE_POSTGRES:
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='notes'
            """
        ).fetchall()
        cols = {r["column_name"] for r in rows}
        return ("body" in cols), ("content" in cols)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(notes);").fetchall()}
    return ("body" in cols), ("content" in cols)

def ensure_schema() -> None:
    conn = db()
    try:
        if USE_POSTGRES:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT '',
                    stripe_customer_id TEXT NOT NULL DEFAULT '',
                    stripe_subscription_id TEXT NOT NULL DEFAULT '',
                    subscription_status TEXT NOT NULL DEFAULT 'free',
                    current_period_end TEXT NOT NULL DEFAULT ''
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    mode TEXT NOT NULL DEFAULT 'school',
                    available_minutes INTEGER NOT NULL DEFAULT 120,
                    streak INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT '',
                    last_streak_at TEXT NOT NULL DEFAULT '',
                    xp INTEGER NOT NULL DEFAULT 0,
                    level INTEGER NOT NULL DEFAULT 1,
                    combo INTEGER NOT NULL DEFAULT 0,
                    last_xp_award_at TEXT NOT NULL DEFAULT ''
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS classes (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(user_id, name)
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assignments (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    class_id INTEGER REFERENCES classes(id) ON DELETE SET NULL,
                    title TEXT NOT NULL,
                    due_date TEXT NOT NULL DEFAULT '',
                    minutes INTEGER NOT NULL DEFAULT 30,
                    status TEXT NOT NULL DEFAULT 'open',
                    priority TEXT NOT NULL DEFAULT 'normal',
                    task_type TEXT NOT NULL DEFAULT '',
                    ignore_count INTEGER NOT NULL DEFAULT 0,
                    last_planned_at TEXT NOT NULL DEFAULT '',
                    last_completed_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    date TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT ''
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL DEFAULT 'Untitled',
                    body TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    tag TEXT NOT NULL DEFAULT 'general',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS flashcards (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    class_id INTEGER REFERENCES classes(id) ON DELETE SET NULL,
                    front TEXT NOT NULL,
                    back TEXT NOT NULL,
                    last_reviewed TEXT NOT NULL DEFAULT '',
                    ease DOUBLE PRECISION NOT NULL DEFAULT 2.5,
                    interval_days INTEGER NOT NULL DEFAULT 0,
                    due_date TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quizzes (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    questions_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_plan (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    day TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(user_id, day)
                )
                """
            )

            conn.execute("UPDATE assignments SET priority='normal' WHERE priority IS NULL OR priority='';")
            conn.execute("UPDATE assignments SET task_type='' WHERE task_type IS NULL;")
            conn.execute("UPDATE assignments SET ignore_count=0 WHERE ignore_count IS NULL;")
            conn.execute("UPDATE assignments SET last_planned_at='' WHERE last_planned_at IS NULL;")
            conn.execute("UPDATE assignments SET last_completed_at='' WHERE last_completed_at IS NULL;")

            conn.execute("UPDATE notes SET title='Untitled' WHERE title IS NULL;")
            conn.execute("UPDATE notes SET body='' WHERE body IS NULL;")
            conn.execute("UPDATE notes SET content='' WHERE content IS NULL;")
            conn.execute("UPDATE notes SET tag='general' WHERE tag IS NULL;")
            conn.execute("UPDATE notes SET created_at='' WHERE created_at IS NULL;")
            conn.execute("UPDATE notes SET updated_at='' WHERE updated_at IS NULL;")
            conn.execute("UPDATE notes SET body=content WHERE (body='' OR body IS NULL) AND content<>'';")
            conn.execute("UPDATE notes SET content=body WHERE (content='' OR content IS NULL) AND body<>'';")

            conn.commit()
            return

        # ---------------- SQLite path ----------------
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT '',
                stripe_customer_id TEXT NOT NULL DEFAULT '',
                stripe_subscription_id TEXT NOT NULL DEFAULT '',
                subscription_status TEXT NOT NULL DEFAULT 'free',
                current_period_end TEXT NOT NULL DEFAULT ''
            )
            """
        )

        if not _column_exists(conn, "users", "stripe_customer_id"):
            conn.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT NOT NULL DEFAULT '';")
        if not _column_exists(conn, "users", "stripe_subscription_id"):
            conn.execute("ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT NOT NULL DEFAULT '';")
        if not _column_exists(conn, "users", "subscription_status"):
            conn.execute("ALTER TABLE users ADD COLUMN subscription_status TEXT NOT NULL DEFAULT 'free';")
        if not _column_exists(conn, "users", "current_period_end"):
            conn.execute("ALTER TABLE users ADD COLUMN current_period_end TEXT NOT NULL DEFAULT '';")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'school',
                available_minutes INTEGER NOT NULL DEFAULT 120,
                streak INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT '',
                last_streak_at TEXT NOT NULL DEFAULT '',
                xp INTEGER NOT NULL DEFAULT 0,
                level INTEGER NOT NULL DEFAULT 1,
                combo INTEGER NOT NULL DEFAULT 0,
                last_xp_award_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        if not _column_exists(conn, "settings", "streak"):
            conn.execute("ALTER TABLE settings ADD COLUMN streak INTEGER NOT NULL DEFAULT 0;")
        if not _column_exists(conn, "settings", "updated_at"):
            conn.execute("ALTER TABLE settings ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';")
        if not _column_exists(conn, "settings", "last_streak_at"):
            conn.execute("ALTER TABLE settings ADD COLUMN last_streak_at TEXT NOT NULL DEFAULT '';")
        if not _column_exists(conn, "settings", "xp"):
            conn.execute("ALTER TABLE settings ADD COLUMN xp INTEGER NOT NULL DEFAULT 0;")
        if not _column_exists(conn, "settings", "level"):
            conn.execute("ALTER TABLE settings ADD COLUMN level INTEGER NOT NULL DEFAULT 1;")
        if not _column_exists(conn, "settings", "combo"):
            conn.execute("ALTER TABLE settings ADD COLUMN combo INTEGER NOT NULL DEFAULT 0;")
        if not _column_exists(conn, "settings", "last_xp_award_at"):
            conn.execute("ALTER TABLE settings ADD COLUMN last_xp_award_at TEXT NOT NULL DEFAULT '';")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS classes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT '',
                UNIQUE(user_id, name),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                class_id INTEGER,
                title TEXT NOT NULL,
                due_date TEXT NOT NULL DEFAULT '',
                minutes INTEGER NOT NULL DEFAULT 30,
                status TEXT NOT NULL DEFAULT 'open',
                priority TEXT NOT NULL DEFAULT 'normal',
                task_type TEXT NOT NULL DEFAULT '',
                ignore_count INTEGER NOT NULL DEFAULT 0,
                last_planned_at TEXT NOT NULL DEFAULT '',
                last_completed_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(class_id) REFERENCES classes(id) ON DELETE SET NULL
            )
            """
        )

        if not _column_exists(conn, "assignments", "updated_at"):
            conn.execute("ALTER TABLE assignments ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';")
        if not _column_exists(conn, "assignments", "priority"):
            conn.execute("ALTER TABLE assignments ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal';")
        if not _column_exists(conn, "assignments", "task_type"):
            conn.execute("ALTER TABLE assignments ADD COLUMN task_type TEXT NOT NULL DEFAULT '';")
        if not _column_exists(conn, "assignments", "ignore_count"):
            conn.execute("ALTER TABLE assignments ADD COLUMN ignore_count INTEGER NOT NULL DEFAULT 0;")
        if not _column_exists(conn, "assignments", "last_planned_at"):
            conn.execute("ALTER TABLE assignments ADD COLUMN last_planned_at TEXT NOT NULL DEFAULT '';")
        if not _column_exists(conn, "assignments", "last_completed_at"):
            conn.execute("ALTER TABLE assignments ADD COLUMN last_completed_at TEXT NOT NULL DEFAULT '';")

        conn.execute("UPDATE assignments SET priority='normal' WHERE priority IS NULL OR priority='';")
        conn.execute("UPDATE assignments SET task_type='' WHERE task_type IS NULL;")
        conn.execute("UPDATE assignments SET ignore_count=0 WHERE ignore_count IS NULL;")
        conn.execute("UPDATE assignments SET last_planned_at='' WHERE last_planned_at IS NULL;")
        conn.execute("UPDATE assignments SET last_completed_at='' WHERE last_completed_at IS NULL;")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                date TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT 'Untitled',
                body TEXT NOT NULL DEFAULT '',
                tag TEXT NOT NULL DEFAULT 'general',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        if not _column_exists(conn, "notes", "title"):
            conn.execute("ALTER TABLE notes ADD COLUMN title TEXT NOT NULL DEFAULT 'Untitled';")
        if not _column_exists(conn, "notes", "body"):
            conn.execute("ALTER TABLE notes ADD COLUMN body TEXT NOT NULL DEFAULT '';")
        if not _column_exists(conn, "notes", "content"):
            conn.execute("ALTER TABLE notes ADD COLUMN content TEXT NOT NULL DEFAULT '';")
        if not _column_exists(conn, "notes", "tag"):
            conn.execute("ALTER TABLE notes ADD COLUMN tag TEXT NOT NULL DEFAULT 'general';")
        if not _column_exists(conn, "notes", "created_at"):
            conn.execute("ALTER TABLE notes ADD COLUMN created_at TEXT NOT NULL DEFAULT '';")
        if not _column_exists(conn, "notes", "updated_at"):
            conn.execute("ALTER TABLE notes ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';")

        conn.execute("UPDATE notes SET title='Untitled' WHERE title IS NULL;")
        conn.execute("UPDATE notes SET body='' WHERE body IS NULL;")
        conn.execute("UPDATE notes SET content='' WHERE content IS NULL;")
        conn.execute("UPDATE notes SET tag='general' WHERE tag IS NULL;")
        conn.execute("UPDATE notes SET created_at='' WHERE created_at IS NULL;")
        conn.execute("UPDATE notes SET updated_at='' WHERE updated_at IS NULL;")
        conn.execute("UPDATE notes SET body=content WHERE (body='' OR body IS NULL) AND content<>'';")
        conn.execute("UPDATE notes SET content=body WHERE (content='' OR content IS NULL) AND body<>'';")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS flashcards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                class_id INTEGER,
                front TEXT NOT NULL,
                back TEXT NOT NULL,
                last_reviewed TEXT NOT NULL DEFAULT '',
                ease REAL NOT NULL DEFAULT 2.5,
                interval_days INTEGER NOT NULL DEFAULT 0,
                due_date TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(class_id) REFERENCES classes(id) ON DELETE SET NULL
            )
            """
        )

        if not _column_exists(conn, "flashcards", "class_id"):
            conn.execute("ALTER TABLE flashcards ADD COLUMN class_id INTEGER;")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quizzes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                questions_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        if not _column_exists(conn, "quizzes", "questions_json"):
            conn.execute("ALTER TABLE quizzes ADD COLUMN questions_json TEXT NOT NULL DEFAULT '[]';")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_plan (
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(user_id, day),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        conn.commit()
    finally:
        conn.close()


# ---------------------------
# Boot schema
# ---------------------------
_SCHEMA_READY = False


@app.before_request
def _boot_schema_once():
    global _SCHEMA_READY
    if not _SCHEMA_READY:
        ensure_schema()
        _SCHEMA_READY = True


# ---------------------------
# Auth helpers
# ---------------------------
def uid() -> Optional[int]:
    u = session.get("user_id")
    try:
        return int(u) if u is not None else None
    except Exception:
        return None


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not uid():
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def api_login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not uid():
            return jsonify({"ok": False, "error": "Not logged in."}), 401
        return f(*args, **kwargs)
    return wrapper


def ensure_settings_row(user_id: int) -> None:
    conn = db()
    try:
        row = conn.execute("SELECT user_id FROM settings WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            conn.execute(
                """
                INSERT INTO settings(
                    user_id, mode, available_minutes, streak, updated_at, last_streak_at,
                    xp, level, combo, last_xp_award_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (user_id, "school", 120, 0, now_iso(), "", 0, 1, 0, ""),
            )
            conn.commit()
    finally:
        conn.close()


def update_login_streak(user_id: int) -> None:
    conn = db()
    try:
        row = conn.execute(
            "SELECT streak, last_streak_at FROM settings WHERE user_id=?",
            (user_id,),
        ).fetchone()

        if not row:
            ensure_settings_row(user_id)
            row = conn.execute(
                "SELECT streak, last_streak_at FROM settings WHERE user_id=?",
                (user_id,),
            ).fetchone()

        current_streak = int(row["streak"] or 0)
        last_streak_at = row["last_streak_at"] or ""

        today = date.today()

        if not last_streak_at:
            conn.execute(
                "UPDATE settings SET streak=?, last_streak_at=?, updated_at=? WHERE user_id=?",
                (1, today.isoformat(), now_iso(), user_id),
            )
            conn.commit()
            return

        try:
            last_day = datetime.strptime(last_streak_at, "%Y-%m-%d").date()
        except Exception:
            conn.execute(
                "UPDATE settings SET streak=?, last_streak_at=?, updated_at=? WHERE user_id=?",
                (1, today.isoformat(), now_iso(), user_id),
            )
            conn.commit()
            return

        diff_days = (today - last_day).days

        if diff_days <= 0:
            return
        elif diff_days == 1:
            new_streak = current_streak + 1
        else:
            new_streak = 1

        conn.execute(
            "UPDATE settings SET streak=?, last_streak_at=?, updated_at=? WHERE user_id=?",
            (new_streak, today.isoformat(), now_iso(), user_id),
        )
        conn.commit()
    finally:
        conn.close()

# ---------------------------
# Stripe helpers
# ---------------------------
def get_or_create_stripe_customer(user_id: int) -> str:
    conn = db()
    try:
        user = conn.execute(
            "SELECT id, email, name, stripe_customer_id FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if not user:
            raise ValueError("User not found.")

        existing_customer_id = (user["stripe_customer_id"] or "").strip()
        if existing_customer_id:
            return existing_customer_id

        customer = stripe.Customer.create(
            email=user["email"],
            name=user["name"] or "StudyFlow User",
            metadata={"user_id": str(user_id)},
        )
        customer_id = customer["id"]

        conn.execute(
            "UPDATE users SET stripe_customer_id=? WHERE id=?",
            (customer_id, user_id),
        )
        conn.commit()
        return customer_id
    finally:
        conn.close()


def _pick_best_subscription(subscriptions: List[Any]) -> Optional[Any]:
    if not subscriptions:
        return None

    priority = {
        "active": 1,
        "trialing": 2,
        "past_due": 3,
        "unpaid": 4,
        "incomplete": 5,
        "canceled": 6,
        "incomplete_expired": 7,
        "paused": 8,
    }

    def key_fn(sub: Any):
        status = getattr(sub, "status", None) or sub.get("status", "free")
        return priority.get(status, 99)

    ordered = sorted(subscriptions, key=key_fn)
    return ordered[0] if ordered else None


def sync_billing_status_for_user(user_id: int) -> Dict[str, Any]:
    conn = db()
    try:
        user = conn.execute(
            "SELECT id, stripe_customer_id, stripe_subscription_id, subscription_status FROM users WHERE id=?",
            (user_id,),
        ).fetchone()

        if not user:
            return {
                "configured": stripe_ready(),
                "customer_id": "",
                "subscription_id": "",
                "status": "free",
                "is_pro": False,
                "current_period_end": "",
            }

        customer_id = (user["stripe_customer_id"] or "").strip()
        if not stripe_ready():
            return {
                "configured": False,
                "customer_id": customer_id,
                "subscription_id": user["stripe_subscription_id"] or "",
                "status": user["subscription_status"] or "free",
                "is_pro": BYPASS_PRO,
                "current_period_end": "",
            }

        if not customer_id:
            conn.execute(
                "UPDATE users SET subscription_status=?, stripe_subscription_id=?, current_period_end=? WHERE id=?",
                ("free", "", "", user_id),
            )
            conn.commit()
            return {
                "configured": True,
                "customer_id": "",
                "subscription_id": "",
                "status": "free",
                "is_pro": BYPASS_PRO,
                "current_period_end": "",
            }

        subs = stripe.Subscription.list(customer=customer_id, status="all", limit=10)
        best = _pick_best_subscription(list(subs.data))

        if not best:
            conn.execute(
                "UPDATE users SET subscription_status=?, stripe_subscription_id=?, current_period_end=? WHERE id=?",
                ("free", "", "", user_id),
            )
            conn.commit()
            return {
                "configured": True,
                "customer_id": customer_id,
                "subscription_id": "",
                "status": "free",
                "is_pro": BYPASS_PRO,
                "current_period_end": "",
            }

        status = best["status"]
        sub_id = best["id"]
        current_period_end = iso_from_unix(best.get("current_period_end"))
        is_pro = BYPASS_PRO or status in ("active", "trialing")

        conn.execute(
            """
            UPDATE users
            SET stripe_subscription_id=?, subscription_status=?, current_period_end=?
            WHERE id=?
            """,
            (sub_id, status, current_period_end, user_id),
        )
        conn.commit()

        return {
            "configured": True,
            "customer_id": customer_id,
            "subscription_id": sub_id,
            "status": status,
            "is_pro": is_pro,
            "current_period_end": current_period_end,
        }

    except Exception as e:
        return {
            "configured": stripe_ready(),
            "customer_id": "",
            "subscription_id": "",
            "status": "error",
            "is_pro": BYPASS_PRO,
            "current_period_end": "",
            "error": str(e),
        }
    finally:
        conn.close()


def user_has_pro(user_id: int) -> bool:
    info = sync_billing_status_for_user(user_id)
    return bool(info.get("is_pro"))


def get_limits_for_user(user_id: int) -> Dict[str, int]:
    is_pro = user_has_pro(user_id)
    print("IS PRO:", is_pro)
    return PRO_LIMITS if is_pro else FREE_LIMITS


def count_user_rows(user_id: int, table: str) -> int:
    allowed_tables = {"classes", "assignments", "notes", "flashcards", "quizzes"}
    if table not in allowed_tables:
        raise ValueError("Invalid table for count_user_rows")

    conn = db()
    try:
        row = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return int(row["c"] or 0)
    finally:
        conn.close()


def count_user_open_assignments(user_id: int) -> int:
    conn = db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM assignments WHERE user_id=? AND status='open'",
            (user_id,),
        ).fetchone()
        return int(row["c"] or 0)
    finally:
        conn.close()

def count_user_done_today(user_id: int) -> int:
    today = today_iso()
    conn = db()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM assignments
            WHERE user_id=?
              AND status='done'
              AND substr(last_completed_at, 1, 10)=?
            """,
            (user_id, today),
        ).fetchone()
        return int(row["c"] or 0)
    finally:
        conn.close()


def sum_user_completed_minutes_today(user_id: int) -> int:
    today = today_iso()
    conn = db()
    try:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(minutes), 0) AS total
            FROM assignments
            WHERE user_id=?
              AND status='done'
              AND substr(last_completed_at, 1, 10)=?
            """,
            (user_id, today),
        ).fetchone()
        return int(row["total"] or 0)
    finally:
        conn.close()


def build_today_stats(user_id: int) -> Dict[str, Any]:
    done_today = count_user_done_today(user_id)
    completed_minutes = sum_user_completed_minutes_today(user_id)
    open_tasks = count_user_open_assignments(user_id)

    conn = db()
    try:
        s = conn.execute(
            """
            SELECT streak, xp, level, combo
            FROM settings
            WHERE user_id=?
            """,
            (user_id,),
        ).fetchone()
    finally:
        conn.close()

    streak = int(s["streak"] or 0) if s else 0
    xp = int(s["xp"] or 0) if s else 0
    level = int(s["level"] or 1) if s else 1
    combo = int(s["combo"] or 0) if s else 0

    if done_today >= 8:
        rank = "Unstoppable"
    elif done_today >= 5:
        rank = "Locked In"
    elif done_today >= 3:
        rank = "Rolling"
    elif done_today >= 1:
        rank = "Started"
    else:
        rank = "Idle"

    return {
        "day": today_iso(),
        "done_today": int(done_today),
        "completed_minutes_today": int(completed_minutes),
        "open_tasks": int(open_tasks),
        "points_today": xp,
        "xp": xp,
        "level": level,
        "combo": combo,
        "streak": streak,
        "rank": rank,
    }


def xp_needed_for_level(level: int) -> int:
    return max(50, int(level) * 50)

def award_done_xp(user_id: int) -> Dict[str, int]:
    ensure_settings_row(user_id)

    conn = db()
    try:
        s = conn.execute(
            """
            SELECT xp, level, combo
            FROM settings
            WHERE user_id=?
            """,
            (user_id,),
        ).fetchone()

        xp = int(s["xp"] or 0)
        level = int(s["level"] or 1)
        combo = int(s["combo"] or 0)

        combo += 1
        gain = 10 + (5 if combo >= 3 else 0)
        xp += gain

        leveled_up = 0
        while xp >= xp_needed_for_level(level):
            xp -= xp_needed_for_level(level)
            level += 1
            leveled_up += 1

        conn.execute(
            """
            UPDATE settings
            SET xp=?, level=?, combo=?, updated_at=?, last_xp_award_at=?
            WHERE user_id=?
            """,
            (xp, level, combo, now_iso(), now_iso(), user_id),
        )
        conn.commit()

        return {
            "xp": xp,
            "level": level,
            "combo": combo,
            "gain": gain,
            "leveled_up": leveled_up,
        }
    finally:
        conn.close()


def award_recall_xp(user_id: int, amount: int = 5) -> Dict[str, int]:
    ensure_settings_row(user_id)
    amount = max(0, int(amount or 0))

    conn = db()
    try:
        s = conn.execute(
            """
            SELECT xp, level, combo
            FROM settings
            WHERE user_id=?
            """,
            (user_id,),
        ).fetchone()

        xp = int(s["xp"] or 0)
        level = int(s["level"] or 1)
        combo = int(s["combo"] or 0)

        xp += amount

        leveled_up = 0
        while xp >= xp_needed_for_level(level):
            xp -= xp_needed_for_level(level)
            level += 1
            leveled_up += 1

        conn.execute(
            """
            UPDATE settings
            SET xp=?, level=?, updated_at=?, last_xp_award_at=?
            WHERE user_id=?
            """,
            (xp, level, now_iso(), now_iso(), user_id),
        )
        conn.commit()

        return {
            "xp": xp,
            "level": level,
            "combo": combo,
            "gain": amount,
            "leveled_up": leveled_up,
        }
    finally:
        conn.close()

# ---------------------------
# Email helpers
# ---------------------------
def smtp_configured() -> bool:
    return bool(MAIL_ENABLED)


def generate_4digit_code() -> str:
    return f"{random.randint(0, 9999):04d}"


def send_reset_code_email(to_email: str, code: str) -> Tuple[bool, str]:
    if not smtp_configured():
        return False, "Email sending is not configured on this computer."

    msg = EmailMessage()
    msg["Subject"] = f"{APP_NAME} password reset code"
    msg["From"] = MAIL_FROM
    msg["To"] = to_email
    msg.set_content(
        f"Your {APP_NAME} reset code is: {code}\n\n"
        f"If you didn't request this, ignore this email."
    )

    ctx = ssl.create_default_context(cafile=certifi.where())

    try:
        with smtplib.SMTP(MAIL_HOST, 587, timeout=20) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            s.login(MAIL_USER, MAIL_PASS)
            s.send_message(msg)
        return True, "Code sent! Check your inbox (and spam)."
    except smtplib.SMTPAuthenticationError as e:
        return False, f"Email auth failed (STARTTLS 587): {e}"
    except Exception as e:
        err_587 = str(e)

    try:
        with smtplib.SMTP_SSL(MAIL_HOST, 465, timeout=20, context=ctx) as s:
            s.login(MAIL_USER, MAIL_PASS)
            s.send_message(msg)
        return True, "Code sent! Check your inbox (and spam)."
    except smtplib.SMTPAuthenticationError as e:
        return False, f"Email auth failed (SSL 465): {e}"
    except Exception as e:
        return False, f"Email failed. 587 err: {err_587} | 465 err: {e}"

# ---------------------------
# Planner helpers
# ---------------------------
def assignment_keyword_boost(title: str, task_type: str = "") -> int:
    t = (title or "").lower()
    tt = (task_type or "").lower()

    score = 0

    if any(k in t for k in ("exam", "final", "midterm")) or tt == "exam":
        score = max(score, 35)

    if "test" in t or tt == "test":
        score = max(score, 32)

    if "quiz" in t or tt == "quiz":
        score = max(score, 20)

    if any(k in t for k in ("essay", "paper")) or tt == "essay":
        score = max(score, 24)

    if "presentation" in t or tt == "presentation":
        score = max(score, 22)

    if "project" in t or tt == "project":
        score = max(score, 20)

    if "homework" in t or tt == "homework":
        score = max(score, 10)

    if tt == "study":
        score = max(score, 8)

    return score


def assignment_priority_boost(priority: str) -> int:
    p = (priority or "normal").lower()
    if p == "high":
        return 28
    if p == "normal":
        return 10
    return 0


def assignment_size_boost(minutes: int) -> int:
    minutes = int(minutes or 0)

    if minutes >= 180:
        return 28
    if minutes >= 120:
        return 22
    if minutes >= 90:
        return 17
    if minutes >= 60:
        return 12
    if minutes >= 30:
        return 6
    return 0


def assignment_urgency_boost(due_date_str: str) -> int:
    d = days_until_due(due_date_str)

    if d is None:
        return 6
    if d < 0:
        return 120
    if d == 0:
        return 90
    if d == 1:
        return 70
    if d == 2:
        return 52
    if d == 3:
        return 40
    if 4 <= d <= 5:
        return 28
    if 6 <= d <= 7:
        return 18
    return 8


def recency_penalty(last_planned_at: str) -> int:
    if not last_planned_at:
        return 0

    try:
        planned_dt = datetime.fromisoformat(last_planned_at.replace("Z", "+00:00"))
        now_dt = datetime.now(timezone.utc)
        hours_ago = (now_dt - planned_dt).total_seconds() / 3600.0

        if hours_ago < 6:
            return -12
        if hours_ago < 12:
            return -8
        if hours_ago < 24:
            return -4
        return 0
    except Exception:
        return 0


def class_repeat_penalty(class_counts: Dict[str, int], class_name: str) -> int:
    name = (class_name or "").strip().lower()
    if not name:
        return 0

    repeats = class_counts.get(name, 0)
    if repeats <= 0:
        return 0
    if repeats == 1:
        return -6
    if repeats == 2:
        return -12
    return -18


def chunk_task_minutes(task_minutes: int, available_minutes: int, urgency_days: Optional[int] = None) -> List[int]:
    task_minutes = max(15, int(task_minutes or 15))
    available_minutes = max(0, int(available_minutes or 0))

    if available_minutes < 45:
        target = 20
    elif available_minutes < 90:
        target = 30
    elif available_minutes < 150:
        target = 40
    else:
        target = 50

    if urgency_days is not None and urgency_days <= 1:
        target += 10

    target = max(20, min(target, 60))

    chunks: List[int] = []
    remaining = task_minutes

    while remaining > 0:
        if remaining <= target:
            chunks.append(remaining)
            break

        chunk = target
        leftover = remaining - chunk

        if 0 < leftover < 15:
            chunk += leftover
            leftover = 0

        chunks.append(chunk)
        remaining -= chunk

    return chunks


def fetch_open_assignments_for_planning(user_id: int) -> List[Dict[str, Any]]:
    conn = db()
    try:
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.due_date, a.minutes, a.class_id, a.created_at,
                   a.priority, a.task_type, a.ignore_count, a.last_planned_at, a.last_completed_at,
                   c.name AS class_name
            FROM assignments a
            LEFT JOIN classes c ON c.id = a.class_id
            WHERE a.user_id=? AND a.status='open'
            ORDER BY a.created_at DESC
            """,
            (user_id,),
        ).fetchall()

        items = []
        for r in rows:
            items.append(
                {
                    "id": int(r["id"]),
                    "title": r["title"],
                    "due_date": r["due_date"],
                    "minutes": int(r["minutes"]),
                    "class_id": int(r["class_id"]) if r["class_id"] is not None else None,
                    "class_name": r["class_name"] or "",
                    "created_at": r["created_at"] or "",
                    "priority": r["priority"] or "normal",
                    "task_type": r["task_type"] or "",
                    "ignore_count": int(r["ignore_count"] or 0),
                    "last_planned_at": r["last_planned_at"] or "",
                    "last_completed_at": r["last_completed_at"] or "",
                }
            )
        return items
    finally:
        conn.close()


def compute_pro_score(task: Dict[str, Any]) -> int:
    minutes = int(task.get("minutes", 0) or 0)
    priority = task.get("priority", "normal")
    title = task.get("title", "")
    task_type = task.get("task_type", "")
    due_date = task.get("due_date", "")
    ignore_count = int(task.get("ignore_count", 0) or 0)
    last_planned_at = task.get("last_planned_at", "")

    score = 0
    score += assignment_urgency_boost(due_date)
    score += assignment_size_boost(minutes)
    score += assignment_keyword_boost(title, task_type)
    score += assignment_priority_boost(priority)

    if ignore_count == 1:
        score += 12
    elif ignore_count == 2:
        score += 20
    elif ignore_count >= 3:
        score += 30

    score += recency_penalty(last_planned_at)

    return int(score)


def mark_planned_assignments(user_id: int, planned_ids: List[int]) -> None:
    conn = db()
    try:
        now = now_iso()

        unique_ids = []
        seen = set()
        for pid in planned_ids:
            if pid not in seen:
                seen.add(pid)
                unique_ids.append(pid)

        if unique_ids:
            placeholders = ",".join(["?"] * len(unique_ids))

            conn.execute(
                f"""
                UPDATE assignments
                SET last_planned_at=?,
                    ignore_count=0
                WHERE user_id=? AND id IN ({placeholders})
                """,
                [now, user_id, *unique_ids],
            )

            conn.execute(
                f"""
                UPDATE assignments
                SET ignore_count=ignore_count+1
                WHERE user_id=?
                  AND status='open'
                  AND id NOT IN ({placeholders})
                """,
                [user_id, *unique_ids],
            )
        else:
            conn.execute(
                """
                UPDATE assignments
                SET ignore_count=ignore_count+1
                WHERE user_id=? AND status='open'
                """,
                (user_id,),
            )

        conn.commit()
    finally:
        conn.close()


def build_free_plan(user_id: int, available_minutes: int) -> Dict[str, Any]:
    tasks = fetch_open_assignments_for_planning(user_id)

    for t in tasks:
        t["score"] = (
            assignment_urgency_boost(t.get("due_date", "")) +
            assignment_priority_boost(t.get("priority", "normal")) +
            assignment_keyword_boost(t.get("title", ""), t.get("task_type", "")) +
            assignment_size_boost(int(t.get("minutes", 0) or 0))
        )

    tasks.sort(
        key=lambda t: (
            -int(t["score"]),
            1 if not t["due_date"] else 0,
            t["due_date"] or "9999-12-31",
            -int(t["minutes"]),
            t["created_at"],
        )
    )

    top3 = tasks[:3]
    next_task = tasks[0] if tasks else None

    blocks = []
    remaining = int(available_minutes)
    planned_ids: List[int] = []

    for t in tasks:
        if remaining < 15:
            break

        chunk = min(45, max(25, min(remaining, int(t["minutes"]) if int(t["minutes"]) > 0 else 30)))
        blocks.append({
            "title": t["title"],
            "class": t["class_name"],
            "minutes": int(chunk),
            "assignment_id": t["id"],
            "score": int(t["score"]),
            "priority": t.get("priority", "normal"),
            "task_type": t.get("task_type", ""),
        })
        remaining -= int(chunk)
        planned_ids.append(t["id"])

    mark_planned_assignments(user_id, planned_ids)

    return {
        "day": today_iso(),
        "plan_type": "free",
        "available_minutes": int(available_minutes),
        "top3": top3,
        "next": next_task,
        "time_blocks": blocks,
        "buffer_minutes": 0,
        "ignore_list": [],
    }


def build_pro_plan(user_id: int, available_minutes: int) -> Dict[str, Any]:
    tasks = fetch_open_assignments_for_planning(user_id)

    scored: List[Dict[str, Any]] = []
    for t in tasks:
        t2 = dict(t)
        t2["score"] = compute_pro_score(t2)
        t2["days_until_due"] = days_until_due(t2.get("due_date", ""))
        scored.append(t2)

    scored.sort(
        key=lambda t: (
            -int(t["score"]),
            1 if not t["due_date"] else 0,
            t["due_date"] or "9999-12-31",
            -int(t["minutes"]),
            t["created_at"],
        )
    )

    top3 = scored[:3]
    next_task = scored[0] if scored else None

    if available_minutes >= 180:
        buffer_minutes = 20
    elif available_minutes >= 120:
        buffer_minutes = 15
    elif available_minutes >= 60:
        buffer_minutes = 10
    else:
        buffer_minutes = 5

    usable_minutes = max(0, int(available_minutes) - buffer_minutes)

    blocks = []
    remaining = usable_minutes
    planned_ids: List[int] = []
    class_counts: Dict[str, int] = {}

    working = [dict(x) for x in scored]

    while remaining >= 15 and working:
        best_index = None
        best_score = None

        for i, task in enumerate(working):
            adjusted = int(task["score"]) + class_repeat_penalty(class_counts, task.get("class_name", ""))
            if best_score is None or adjusted > best_score:
                best_score = adjusted
                best_index = i

        if best_index is None:
            break

        t = working.pop(best_index)
        urgency_days = t.get("days_until_due")
        chunks = chunk_task_minutes(
            int(t["minutes"]),
            int(available_minutes),
            urgency_days=urgency_days
        )

        for idx, chunk in enumerate(chunks):
            if remaining < 15:
                break

            if chunk > remaining:
                if remaining >= 15:
                    chunk = remaining
                else:
                    break

            label = t["title"]
            if len(chunks) > 1:
                label = f"{t['title']} (Part {idx + 1})"

            blocks.append({
                "title": label,
                "class": t["class_name"],
                "minutes": int(chunk),
                "assignment_id": t["id"],
                "score": int(t["score"]),
                "priority": t.get("priority", "normal"),
                "task_type": t.get("task_type", ""),
            })
            remaining -= int(chunk)

            class_key = (t.get("class_name") or "").strip().lower()
            if class_key:
                class_counts[class_key] = class_counts.get(class_key, 0) + 1

            if t["id"] not in planned_ids:
                planned_ids.append(t["id"])

    mark_planned_assignments(user_id, planned_ids)

    return {
        "day": today_iso(),
        "plan_type": "pro",
        "available_minutes": int(available_minutes),
        "top3": top3,
        "next": next_task,
        "time_blocks": blocks,
        "buffer_minutes": int(buffer_minutes),
        "ignore_list": [],
    }


def build_plan(user_id: int, available_minutes: int) -> Dict[str, Any]:
    if user_has_pro(user_id):
        return build_pro_plan(user_id, available_minutes)
    return build_free_plan(user_id, available_minutes)

# ---------------------------
# Pages
# ---------------------------
@app.route("/")
def index():
    if uid():
        return redirect(url_for("today"))
    return redirect(url_for("login"))


@app.route("/debug-mail")
def debug_mail():
    return jsonify({
        "env_path": ENV_PATH,
        "MAIL_USER": MAIL_USER,
        "MAIL_FROM": MAIL_FROM,
        "PASS_LEN": len(MAIL_PASS),
        "PASS_HAS_SPACES": (" " in MAIL_PASS),
        "MAIL_ENABLED": MAIL_ENABLED,
        "HOST": MAIL_HOST,
        "PORT": MAIL_PORT,
        "STRIPE_READY": stripe_ready(),
        "STRIPE_PRICE_ID": STRIPE_PRICE_ID,
        "BASE_URL": BASE_URL,
    })


@app.route("/login", methods=["GET", "POST"])
def login():
    if uid():
        return redirect(url_for("today"))

    if request.method == "POST":
        email = safe_email(request.form.get("email"))
        password = request.form.get("password") or ""

        if not email or not password:
            return render_template("login.html", error="Enter email + password.")

        conn = db()
        try:
            user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        finally:
            conn.close()

        if (not user) or (user["id"] is None) or (not user["password_hash"]):
            return render_template("login.html", error="Invalid email or password.")

        if not check_password_hash(user["password_hash"], password):
            return render_template("login.html", error="Invalid email or password.")

        session["user_id"] = int(user["id"])
        ensure_settings_row(int(user["id"]))
        update_login_streak(int(user["id"]))
        return redirect(url_for("today"))

    return render_template("login.html", error=None)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if uid():
        return redirect(url_for("today"))

    if request.method == "POST":
        name = safe_text(request.form.get("name"), 80) or "User"
        email = safe_email(request.form.get("email"))
        password = request.form.get("password") or ""

        if not is_valid_email(email):
            return render_template("signup.html", error="Enter a valid email.")

        if len(password) < 6:
            return render_template("signup.html", error="Password must be 6+ characters.")

        conn = db()
        try:
            existing = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if existing:
                return render_template("signup.html", error="That email already exists. Log in instead.")

            pw_hash = generate_password_hash(password)
            created = now_iso()

            cur = conn.execute(
                """
                INSERT INTO users(
                    email, name, password_hash, created_at,
                    stripe_customer_id, stripe_subscription_id, subscription_status, current_period_end
                )
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (email, name, pw_hash, created, "", "", "free", ""),
            )
            user_id = int(cur.lastrowid)

            conn.execute(
                """
                INSERT INTO settings(
                    user_id, mode, available_minutes, streak, updated_at, last_streak_at,
                    xp, level, combo, last_xp_award_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (user_id, "school", 120, 0, now_iso(), "", 0, 1, 0, ""),
            )

            conn.commit()
        finally:
            conn.close()

        session["user_id"] = user_id
        update_login_streak(user_id)
        return redirect(url_for("today"))

    return render_template("signup.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------
# Password reset
# ---------------------------
@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = safe_email(request.form.get("email"))

        if not is_valid_email(email):
            return render_template("forgot_password.html", info=None, error="Enter a valid email.")

        conn = db()
        try:
            u = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        finally:
            conn.close()

        session["reset_email"] = email

        if not u:
            session["reset_info"] = "If an account exists for that email, a code was sent."
            return redirect(url_for("reset_password"))

        code = generate_4digit_code()
        session["reset_code"] = code
        session["reset_code_created_at"] = now_iso()

        ok, msg = send_reset_code_email(email, code)
        session["reset_info"] = msg
        return redirect(url_for("reset_password"))

    return render_template("forgot_password.html", info=None, error=None)


@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    if request.method == "POST":
        email = safe_email(request.form.get("email"))
        code = safe_text(request.form.get("code"), 4)
        new_password = request.form.get("password") or ""

        if not is_valid_email(email):
            return render_template("reset_password.html", info=None, error="Enter a valid email.", email=email)

        if len(code) != 4 or (not code.isdigit()):
            return render_template("reset_password.html", info=None, error="Enter the 4-digit code.", email=email)

        if len(new_password) < 6:
            return render_template("reset_password.html", info=None, error="Password must be 6+ characters.", email=email)

        if email != session.get("reset_email") or code != session.get("reset_code"):
            return render_template("reset_password.html", info=None, error="Invalid email or code.", email=email)

        conn = db()
        try:
            conn.execute(
                "UPDATE users SET password_hash=? WHERE email=?",
                (generate_password_hash(new_password), email),
            )
            conn.commit()
        finally:
            conn.close()

        session.pop("reset_email", None)
        session.pop("reset_code", None)
        session.pop("reset_code_created_at", None)
        session.pop("reset_info", None)

        return redirect(url_for("login"))

    info = session.pop("reset_info", None)
    email = session.get("reset_email", "")
    return render_template("reset_password.html", info=info, error=None, email=email)


@app.route("/today")
@login_required
def today():
    return render_template("today.html", app_name=APP_NAME)


@app.route("/inputs")
@login_required
def inputs():
    return render_template("inputs.html", app_name=APP_NAME)


@app.route("/resources")
@login_required
def resources():
    return render_template("resources.html", app_name=APP_NAME)


@app.route("/active-recall")
@login_required
def active_recall():
    return render_template("active_recall.html", app_name=APP_NAME)


@app.route("/settings")
@login_required
def settings_page():
    return render_template("settings.html", app_name=APP_NAME)


@app.route("/billing/success")
@login_required
def billing_success():
    user_id = uid()
    session_id = safe_text(request.args.get("session_id"), 200)

    if stripe_ready() and session_id:
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id)
            customer_id = checkout_session.get("customer", "") or ""
            subscription_id = checkout_session.get("subscription", "") or ""

            if customer_id:
                conn = db()
                try:
                    conn.execute(
                        """
                        UPDATE users
                        SET stripe_customer_id=?,
                            stripe_subscription_id=?,
                            subscription_status=?,
                            current_period_end=?
                        WHERE id=?
                        """,
                        (customer_id, subscription_id, "active", "", user_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception:
            pass

    sync_billing_status_for_user(int(user_id))
    return redirect(url_for("settings_page"))


# ---------------------------
# API: settings + billing
# ---------------------------

@app.route("/api/settings", methods=["GET"])
@api_login_required
def api_settings_get():
    user_id = int(uid())
    conn = db()
    try:
        s = conn.execute(
            """
            SELECT mode, available_minutes, streak, updated_at, last_streak_at,
                   xp, level, combo, last_xp_award_at
            FROM settings
            WHERE user_id=?
            """,
            (user_id,),
        ).fetchone()

        if not s:
            ensure_settings_row(user_id)
            s = conn.execute(
                """
                SELECT mode, available_minutes, streak, updated_at, last_streak_at,
                       xp, level, combo, last_xp_award_at
                FROM settings
                WHERE user_id=?
                """,
                (user_id,),
            ).fetchone()

        u = conn.execute(
            """
            SELECT name, email, stripe_customer_id, stripe_subscription_id, subscription_status
            FROM users
            WHERE id=?
            """,
            (user_id,),
        ).fetchone()

        billing = sync_billing_status_for_user(user_id)
        limits = get_limits_for_user(user_id)

        classes_count = count_user_rows(user_id, "classes")
        assignments_count = count_user_open_assignments(user_id)
        notes_count = count_user_rows(user_id, "notes")
        flashcards_count = count_user_rows(user_id, "flashcards")
        quizzes_count = count_user_rows(user_id, "quizzes")
        today_stats = build_today_stats(user_id)

        return jsonify(
            {
                "ok": True,
                "settings": {
                    "mode": s["mode"],
                    "available_minutes": int(s["available_minutes"]),
                    "streak": int(s["streak"]),
                    "updated_at": s["updated_at"],
                    "xp": int(s["xp"] or 0),
                    "level": int(s["level"] or 1),
                    "combo": int(s["combo"] or 0),
                    "last_xp_award_at": s["last_xp_award_at"] or "",
                },
                "profile": {
                    "name": u["name"] if u else "",
                    "email": u["email"] if u else "",
                    "streak": int(s["streak"]) if s else 0,
                },
                "billing": billing,
                "stripe": {
                    "publishable_key": STRIPE_PUBLISHABLE_KEY,
                    "configured": stripe_ready(),
                    "bypass_pro": BYPASS_PRO,
                },
                "limits": limits,
                "usage": {
                    "classes": classes_count,
                    "assignments": assignments_count,
                    "notes": notes_count,
                    "flashcards": flashcards_count,
                    "quizzes": quizzes_count,
                },
                "today_stats": today_stats,
            }
        )
    finally:
        conn.close()

@app.route("/api/settings", methods=["POST"])
@api_login_required
def api_settings_post():
    user_id = uid()
    data = request.get_json(silent=True) or {}

    mode = safe_text(data.get("mode"), 20).lower()
    available = data.get("available_minutes")
    name = data.get("name")

    if mode and mode not in ("school", "business", "personal"):
        return jsonify({"ok": False, "error": "mode must be school, business, or personal."}), 400

    available_minutes = None
    if available is not None:
        available_minutes = clamp_int(available, 0, 24 * 60, 120)

    name = safe_text(name, 80) if name is not None else None
    if name is not None and contains_banned_word(name):
        return jsonify({
            "ok": False,
            "error": "That name is not allowed."
        }), 400

    conn = db()
    try:
        ensure_settings_row(int(user_id))

        if name is not None:
            conn.execute("UPDATE users SET name=? WHERE id=?", (name, user_id))

        if mode:
            conn.execute("UPDATE settings SET mode=?, updated_at=? WHERE user_id=?", (mode, now_iso(), user_id))

        if available_minutes is not None:
            conn.execute(
                "UPDATE settings SET available_minutes=?, updated_at=? WHERE user_id=?",
                (available_minutes, now_iso(), user_id),
            )

        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route("/api/billing/status", methods=["GET"])
@api_login_required
def api_billing_status():
    billing = sync_billing_status_for_user(int(uid()))
    return jsonify({"ok": True, "billing": billing})

@app.route("/api/stats/today", methods=["GET"])
@api_login_required
def api_stats_today():
    user_id = int(uid())
    stats = build_today_stats(user_id)
    return jsonify({"ok": True, "stats": stats})

@app.route("/api/billing/create-checkout-session", methods=["POST"])
@api_login_required
def api_billing_create_checkout_session():
    user_id = int(uid())

    if not stripe_ready():
        return jsonify({"ok": False, "error": "Stripe is not configured yet."}), 500

    try:
        customer_id = get_or_create_stripe_customer(user_id)

        checkout_session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            allow_promotion_codes=True,
            success_url=f"{BASE_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{BASE_URL}/settings",
            metadata={"user_id": str(user_id)},
        )

        return jsonify({"ok": True, "url": checkout_session.url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/billing/create-portal-session", methods=["POST"])
@api_login_required
def api_billing_create_portal_session():
    user_id = int(uid())

    if not stripe_ready():
        return jsonify({"ok": False, "error": "Stripe is not configured yet."}), 500

    try:
        customer_id = get_or_create_stripe_customer(user_id)

        portal_session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{BASE_URL}/settings",
        )

        return jsonify({"ok": True, "url": portal_session.url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/recall/xp", methods=["POST"])
@api_login_required
def api_recall_xp():
    user_id = int(uid())
    data = request.get_json(silent=True) or {}

    amount = clamp_int(data.get("amount"), 0, 50, 5)

    xp_info = award_recall_xp(user_id, amount)

    return jsonify({
        "ok": True,
        "xp": xp_info
    })

# ---------------------------
# API: classes
# ---------------------------
@app.route("/api/classes", methods=["GET"])
@api_login_required
def api_classes_list():
    user_id = uid()
    conn = db()
    try:
        rows = conn.execute(
            "SELECT id, name, created_at FROM classes WHERE user_id=? ORDER BY name COLLATE NOCASE ASC",
            (user_id,),
        ).fetchall()
        return jsonify({
            "ok": True,
            "classes": [{"id": int(r["id"]), "name": r["name"], "created_at": r["created_at"]} for r in rows]
        })
    finally:
        conn.close()


@app.route("/api/classes", methods=["POST"])
@api_login_required
def api_classes_create():
    user_id = int(uid())
    data = request.get_json(silent=True) or {}
    name = safe_text(data.get("name"), 80)

    if not name:
        return jsonify({"ok": False, "error": "Class name is required."}), 400

    limits = get_limits_for_user(user_id)
    classes_count = count_user_rows(user_id, "classes")
    print("DEBUG CLASSES:", classes_count, "/", limits["classes"])
    if classes_count >= limits["classes"]:
        return jsonify({
            "ok": False,
            "error": f"Class limit reached. Your plan allows {limits['classes']} classes."
        }), 403

    conn = db()
    try:
        try:
            cur = conn.execute(
                "INSERT INTO classes(user_id, name, created_at) VALUES(?,?,?)",
                (user_id, name, now_iso()),
            )
            conn.commit()
        except Exception:
            return jsonify({"ok": False, "error": "That class already exists."}), 400

        return jsonify({"ok": True, "class": {"id": int(cur.lastrowid), "name": name}})
    finally:
        conn.close()


@app.route("/api/classes/<int:class_id>", methods=["DELETE"])
@api_login_required
def api_classes_delete(class_id: int):
    user_id = uid()
    conn = db()
    try:
        conn.execute("DELETE FROM classes WHERE id=? AND user_id=?", (class_id, user_id))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


# ---------------------------
# API: assignments
# ---------------------------
@app.route("/api/assignments", methods=["GET"])
@api_login_required
def api_assignments_list():
    user_id = uid()
    conn = db()
    try:
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.due_date, a.minutes, a.status, a.class_id,
                   a.priority, a.task_type, a.ignore_count, a.last_planned_at, a.last_completed_at,
                   c.name AS class_name
            FROM assignments a
            LEFT JOIN classes c ON c.id = a.class_id
            WHERE a.user_id=?
            ORDER BY CASE WHEN a.status='done' THEN 1 ELSE 0 END,
                     CASE WHEN a.due_date='' THEN 1 ELSE 0 END,
                     a.due_date ASC,
                     a.created_at DESC
            """,
            (user_id,),
        ).fetchall()

        items = []
        for r in rows:
            items.append({
                "id": int(r["id"]),
                "title": r["title"],
                "due_date": r["due_date"],
                "minutes": int(r["minutes"]),
                "status": r["status"],
                "class_id": int(r["class_id"]) if r["class_id"] is not None else None,
                "class_name": r["class_name"] or "",
                "priority": r["priority"] or "normal",
                "task_type": r["task_type"] or "",
                "ignore_count": int(r["ignore_count"] or 0),
                "last_planned_at": r["last_planned_at"] or "",
                "last_completed_at": r["last_completed_at"] or "",
            })

        return jsonify({"ok": True, "assignments": items})
    finally:
        conn.close()


@app.route("/api/assignments", methods=["POST"])
@api_login_required
def api_assignments_create():
    user_id = int(uid())
    data = request.get_json(silent=True) or {}

    title = safe_text(data.get("title"), 160)
    due_date_raw = safe_text(data.get("due_date"), 20)
    due_date = due_date_raw if due_date_raw else ""
    minutes = data.get("minutes")
    class_id = data.get("class_id")

    priority = safe_text(data.get("priority"), 20).lower() or "normal"
    task_type = safe_text(data.get("task_type"), 40)

    if priority not in ("low", "normal", "high"):
        priority = "normal"

    if not title:
        return jsonify({"ok": False, "error": "Assignment title is required."}), 400

    limits = get_limits_for_user(user_id)
    assignments_count = count_user_open_assignments(user_id)
    if assignments_count >= limits["assignments"]:
        return jsonify({
            "ok": False,
            "error": f"Assignment limit reached. Your plan allows {limits['assignments']} open assignments."
        }), 403

    mins = clamp_int(minutes, 5, 480, 30)

    if due_date and (not re.match(r"^\d{4}-\d{2}-\d{2}$", due_date)):
        return jsonify({"ok": False, "error": "due_date must be YYYY-MM-DD or blank."}), 400

    class_id_int = None
    if class_id is not None and str(class_id).strip() != "":
        try:
            class_id_int = int(class_id)
        except Exception:
            return jsonify({"ok": False, "error": "class_id must be a number or blank."}), 400

    conn = db()
    try:
        if class_id_int is not None:
            owned = conn.execute(
                "SELECT id FROM classes WHERE id=? AND user_id=?",
                (class_id_int, user_id),
            ).fetchone()
            if not owned:
                return jsonify({"ok": False, "error": "That class doesn't exist."}), 400

        cur = conn.execute(
            """
            INSERT INTO assignments(
                user_id, class_id, title, due_date, minutes, status,
                priority, task_type, ignore_count, last_planned_at, last_completed_at,
                created_at, updated_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                user_id,
                class_id_int,
                title,
                due_date or "",
                int(mins),
                "open",
                priority,
                task_type,
                0,
                "",
                "",
                now_iso(),
                now_iso(),
            ),
        )
        conn.commit()
        return jsonify({"ok": True, "assignment": {"id": int(cur.lastrowid)}})
    finally:
        conn.close()


@app.route("/api/assignments/<int:assignment_id>", methods=["DELETE"])
@api_login_required
def api_assignments_delete(assignment_id: int):
    user_id = uid()
    conn = db()
    try:
        conn.execute("DELETE FROM assignments WHERE id=? AND user_id=?", (assignment_id, user_id))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route("/api/assignments/<int:assignment_id>/status", methods=["POST"])
@api_login_required
def api_assignments_set_status(assignment_id: int):
    user_id = uid()
    data = request.get_json(silent=True) or {}
    status = safe_text(data.get("status"), 10).lower()

    if status not in ("open", "done"):
        return jsonify({"ok": False, "error": "status must be open or done."}), 400

    conn = db()
    try:
        assignment = conn.execute(
            """
            SELECT id, status
            FROM assignments
            WHERE id=? AND user_id=?
            """,
            (assignment_id, user_id),
        ).fetchone()

        if not assignment:
            return jsonify({"ok": False, "error": "Assignment not found."}), 404

        current_status = assignment["status"]

        if status == "done":
            if current_status == "done":
                s = conn.execute(
                    """
                    SELECT xp, level, combo
                    FROM settings
                    WHERE user_id=?
                    """,
                    (user_id,),
                ).fetchone()

                return jsonify({
                    "ok": True,
                    "already_done": True,
                    "xp": {
                        "xp": int(s["xp"] or 0) if s else 0,
                        "level": int(s["level"] or 1) if s else 1,
                        "combo": int(s["combo"] or 0) if s else 0,
                        "gain": 0,
                        "leveled_up": 0,
                    }
                })

            conn.execute(
                """
                UPDATE assignments
                SET status=?, updated_at=?, last_completed_at=?
                WHERE id=? AND user_id=?
                """,
                (status, now_iso(), now_iso(), assignment_id, user_id),
            )
            conn.commit()

            xp_info = award_done_xp(int(user_id))

            remaining_open = conn.execute(
                "SELECT COUNT(*) AS c FROM assignments WHERE user_id=? AND status='open'",
                (user_id,),
            ).fetchone()

            if int(remaining_open["c"] or 0) == 0:
                conn.execute(
                    "UPDATE settings SET combo=0, updated_at=? WHERE user_id=?",
                    (now_iso(), user_id),
                )
                conn.commit()
                xp_info["combo"] = 0

            return jsonify({"ok": True, "xp": xp_info})

        else:
            conn.execute(
                """
                UPDATE assignments
                SET status=?, updated_at=?
                WHERE id=? AND user_id=?
                """,
                (status, now_iso(), assignment_id, user_id),
            )
            conn.commit()
            return jsonify({"ok": True})

    finally:
        conn.close()

# ---------------------------
# API: events
# ---------------------------
@app.route("/api/events", methods=["GET"])
@api_login_required
def api_events_list():
    user_id = uid()
    day = safe_text(request.args.get("date"), 20) or today_iso()

    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        day = today_iso()

    conn = db()
    try:
        rows = conn.execute(
            "SELECT id, name, start_time, end_time, date FROM events WHERE user_id=? AND date=? ORDER BY start_time ASC",
            (user_id, day),
        ).fetchall()
        events = [
            {"id": int(r["id"]), "name": r["name"], "start_time": r["start_time"], "end_time": r["end_time"], "date": r["date"]}
            for r in rows
        ]
        return jsonify({"ok": True, "events": events})
    finally:
        conn.close()


@app.route("/api/events", methods=["POST"])
@api_login_required
def api_events_create():
    user_id = uid()
    data = request.get_json(silent=True) or {}

    name = safe_text(data.get("name"), 120)
    start_time = safe_text(data.get("start_time"), 5)
    end_time = safe_text(data.get("end_time"), 5)
    day = safe_text(data.get("date"), 20) or today_iso()
    if not name:
        return jsonify({"ok": False, "error": "Event name is required."}), 400
    if not re.match(r"^\d{2}:\d{2}$", start_time) or not re.match(r"^\d{2}:\d{2}$", end_time):
        return jsonify({"ok": False, "error": "Start/end must be HH:MM."}), 400
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        return jsonify({"ok": False, "error": "date must be YYYY-MM-DD."}), 400

    conn = db()
    try:
        cur = conn.execute(
            "INSERT INTO events(user_id, name, start_time, end_time, date, created_at) VALUES(?,?,?,?,?,?)",
            (user_id, name, start_time, end_time, day, now_iso()),
        )
        conn.commit()
        return jsonify({"ok": True, "event": {"id": int(cur.lastrowid)}})
    finally:
        conn.close()


@app.route("/api/events/<int:event_id>", methods=["DELETE"])
@api_login_required
def api_events_delete(event_id: int):
    user_id = uid()
    conn = db()
    try:
        conn.execute("DELETE FROM events WHERE id=? AND user_id=?", (event_id, user_id))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()

# ---------------------------
# API: notes
# ---------------------------
@app.route("/api/notes", methods=["GET", "POST"])
@api_login_required
def api_notes():
    user_id = int(uid())

    if request.method == "GET":
        conn = db()
        try:
            has_body, has_content = _notes_text_cols(conn)
            text_expr = "body" if has_body else ("content" if has_content else "''")

            rows = conn.execute(
                f"""
                SELECT id, title, tag, created_at, updated_at, {text_expr} AS body
                FROM notes
                WHERE user_id=?
                ORDER BY updated_at DESC, created_at DESC, id DESC
                """,
                (user_id,),
            ).fetchall()

            notes = []
            for r in rows:
                notes.append({
                    "id": int(r["id"]),
                    "title": r["title"] or "Untitled",
                    "tag": r["tag"] or "general",
                    "created_at": r["created_at"] or "",
                    "updated_at": r["updated_at"] or "",
                    "body": r["body"] or "",
                })

            return jsonify({"ok": True, "notes": notes})
        except Exception as e:
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
        finally:
            conn.close()

    limits = get_limits_for_user(user_id)
    notes_count = count_user_rows(user_id, "notes")
    if notes_count >= limits["notes"]:
        return jsonify({
            "ok": False,
            "error": f"Note limit reached. Your plan allows {limits['notes']} notes."
        }), 403

    data = request.get_json(silent=True) or {}
    title = safe_text(data.get("title"), 120) or "Untitled"
    body = safe_text(data.get("body"), 20000)
    tag = safe_text(data.get("tag"), 30) or "general"

    if not body:
        return jsonify({"ok": False, "error": "Note body is required."}), 400

    conn = db()
    try:
        has_body, has_content = _notes_text_cols(conn)

        cols = ["user_id", "title", "tag", "created_at", "updated_at"]
        vals = [user_id, title, tag, now_iso(), now_iso()]

        if has_body:
            cols.append("body")
            vals.append(body)
        if has_content:
            cols.append("content")
            vals.append(body)

        placeholders = ",".join(["?"] * len(cols))
        col_sql = ",".join(cols)

        cur = conn.execute(
            f"INSERT INTO notes({col_sql}) VALUES({placeholders})",
            tuple(vals),
        )
        conn.commit()

        return jsonify({"ok": True, "note": {"id": int(cur.lastrowid)}})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
    finally:
        conn.close()


@app.route("/api/notes/<int:note_id>", methods=["GET", "POST", "DELETE"])
@api_login_required
def api_note_by_id(note_id: int):
    user_id = int(uid())
    conn = db()
    try:
        has_body, has_content = _notes_text_cols(conn)
        text_expr = "body" if has_body else ("content" if has_content else "''")

        existing = conn.execute(
            f"""
            SELECT id, title, tag, created_at, updated_at, {text_expr} AS body
            FROM notes
            WHERE id=? AND user_id=?
            """,
            (note_id, user_id),
        ).fetchone()

        if not existing:
            return jsonify({"ok": False, "error": "Note not found."}), 404

        if request.method == "GET":
            return jsonify({
                "ok": True,
                "note": {
                    "id": int(existing["id"]),
                    "title": existing["title"] or "Untitled",
                    "body": existing["body"] or "",
                    "tag": existing["tag"] or "general",
                    "created_at": existing["created_at"] or "",
                    "updated_at": existing["updated_at"] or "",
                },
            })

        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            title = safe_text(data.get("title"), 120) or "Untitled"
            body = safe_text(data.get("body"), 20000)
            tag = safe_text(data.get("tag"), 30) or "general"

            if not body:
                return jsonify({"ok": False, "error": "Note body is required."}), 400

            sets = ["title=?", "tag=?", "updated_at=?"]
            vals = [title, tag, now_iso()]

            if has_body:
                sets.append("body=?")
                vals.append(body)
            if has_content:
                sets.append("content=?")
                vals.append(body)

            vals.extend([note_id, user_id])

            conn.execute(
                f"UPDATE notes SET {', '.join(sets)} WHERE id=? AND user_id=?",
                tuple(vals),
            )
            conn.commit()

            return jsonify({"ok": True, "note": {"id": note_id}})

        conn.execute("DELETE FROM notes WHERE id=? AND user_id=?", (note_id, user_id))
        conn.commit()
        return jsonify({"ok": True})

    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
    finally:
        conn.close()


@app.route("/api/generate_flashcards", methods=["POST"])
@api_login_required
def generate_flashcards():
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")

    if not text or not text.strip():
        return jsonify({"ok": False, "error": "No text provided"}), 400

    sentences = re.split(r"[.!?]", text)
    flashcards = []

    for s in sentences:
        if len(flashcards) >= 5:
            break

        s = s.strip()
        if len(s) > 10:
            flashcards.append({
                "front": s[:40] + "...",
                "back": s
            })

    return jsonify({
        "ok": True,
        "flashcards": flashcards
    })

# ---------------------------
# API: flashcards
# ---------------------------
@app.route("/api/flashcards", methods=["GET"])
@api_login_required
def api_flashcards_list():
    user_id = uid()
    conn = db()
    try:
        rows = conn.execute(
            """
            SELECT id, class_id, front, back,
                   last_reviewed, ease, interval_days, due_date,
                   created_at, updated_at
            FROM flashcards
            WHERE user_id=?
            ORDER BY
                CASE
                    WHEN due_date IS NULL OR due_date='' THEN 0
                    WHEN due_date <= ? THEN 0
                    ELSE 1
                END,
                CASE
                    WHEN due_date IS NULL OR due_date='' THEN created_at
                    ELSE due_date
                END ASC,
                created_at DESC
            """,
            (user_id, today_iso()),
        ).fetchall()

        cards = []
        today = today_iso()

        for r in rows:
            due_date = r["due_date"] or ""
            is_due = (due_date == "") or (due_date <= today)

            cards.append({
                "id": int(r["id"]),
                "class_id": int(r["class_id"]) if r["class_id"] is not None else None,
                "front": r["front"],
                "back": r["back"],
                "last_reviewed": r["last_reviewed"] or "",
                "ease": float(r["ease"] or 2.5),
                "interval_days": int(r["interval_days"] or 0),
                "due_date": due_date,
                "is_due": is_due,
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            })

        return jsonify({"ok": True, "flashcards": cards})
    finally:
        conn.close()


@app.route("/api/flashcards", methods=["POST"])
@api_login_required
def api_flashcards_create():
    user_id = int(uid())

    limits = get_limits_for_user(user_id)
    count = count_user_rows(user_id, "flashcards")
    if count >= limits["flashcards"]:
        return jsonify({
            "ok": False,
            "error": f"Flashcard limit reached. Your plan allows {limits['flashcards']} flashcards."
        }), 403

    data = request.get_json(silent=True) or {}

    front = safe_text(data.get("front"), 300)
    back = safe_text(data.get("back"), 600)
    class_id = data.get("class_id")

    if not front or not back:
        return jsonify({"ok": False, "error": "Front and back are required."}), 400

    class_id_int = None
    if class_id is not None and str(class_id).strip() != "":
        try:
            class_id_int = int(class_id)
        except Exception:
            return jsonify({"ok": False, "error": "class_id must be a number."}), 400

    conn = db()
    try:
        if class_id_int is not None:
            owned = conn.execute("SELECT id FROM classes WHERE id=? AND user_id=?", (class_id_int, user_id)).fetchone()
            if not owned:
                return jsonify({"ok": False, "error": "That class doesn't exist."}), 400

        cur = conn.execute(
            """
            INSERT INTO flashcards(user_id, class_id, front, back, last_reviewed, ease, interval_days, due_date, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (user_id, class_id_int, front, back, "", 2.5, 0, "", now_iso(), now_iso()),
        )
        conn.commit()
        return jsonify({"ok": True, "flashcard": {"id": int(cur.lastrowid)}})
    finally:
        conn.close()

@app.route("/api/flashcards/<int:card_id>", methods=["POST"])
@api_login_required
def api_flashcards_update(card_id: int):
    user_id = int(uid())
    data = request.get_json(silent=True) or {}

    front = safe_text(data.get("front"), 300)
    back = safe_text(data.get("back"), 600)
    class_id = data.get("class_id")

    if not front or not back:
        return jsonify({"ok": False, "error": "Front and back are required."}), 400

    class_id_int = None
    if class_id is not None and str(class_id).strip() != "":
        try:
            class_id_int = int(class_id)
        except Exception:
            return jsonify({"ok": False, "error": "class_id must be a number."}), 400

    conn = db()
    try:
        if class_id_int is not None:
            owned = conn.execute(
                "SELECT id FROM classes WHERE id=? AND user_id=?",
                (class_id_int, user_id)
            ).fetchone()
            if not owned:
                return jsonify({"ok": False, "error": "That class doesn't exist."}), 400

        existing = conn.execute(
            "SELECT id FROM flashcards WHERE id=? AND user_id=?",
            (card_id, user_id)
        ).fetchone()

        if not existing:
            return jsonify({"ok": False, "error": "Flashcard not found."}), 404

        conn.execute(
            """
            UPDATE flashcards
            SET class_id=?, front=?, back=?, updated_at=?
            WHERE id=? AND user_id=?
            """,
            (class_id_int, front, back, now_iso(), card_id, user_id),
        )
        conn.commit()

        return jsonify({"ok": True})
    finally:
        conn.close()

@app.route("/api/flashcards/<int:card_id>", methods=["DELETE"])
@api_login_required
def api_flashcards_delete(card_id: int):
    user_id = uid()
    conn = db()
    try:
        conn.execute("DELETE FROM flashcards WHERE id=? AND user_id=?", (card_id, user_id))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()

@app.route("/api/flashcards/<int:card_id>/review", methods=["POST"])
@api_login_required
def api_flashcards_review(card_id: int):
    user_id = int(uid())
    data = request.get_json(silent=True) or {}
    result = safe_text(data.get("result"), 20).lower()

    if result not in ("correct", "wrong"):
        return jsonify({"ok": False, "error": "Invalid result."}), 400

    conn = db()
    try:
        card = conn.execute(
            """
            SELECT id, interval_days, ease
            FROM flashcards
            WHERE id=? AND user_id=?
            """,
            (card_id, user_id),
        ).fetchone()

        if not card:
            return jsonify({"ok": False, "error": "Flashcard not found."}), 404

        interval_days = int(card["interval_days"] or 0)
        ease = float(card["ease"] or 2.5)

        if result == "correct":
            interval_days = max(1, int(round((interval_days or 1) * ease)))
            ease = min(3.0, ease + 0.05)
        else:
            interval_days = 1
            ease = max(1.3, ease - 0.2)

        due = date.today().fromordinal(date.today().toordinal() + interval_days).isoformat()

        conn.execute(
            """
            UPDATE flashcards
            SET interval_days=?, ease=?, last_reviewed=?, due_date=?, updated_at=?
            WHERE id=? AND user_id=?
            """,
            (
                interval_days,
                ease,
                now_iso(),
                due,
                now_iso(),
                card_id,
                user_id,
            ),
        )
        conn.commit()

        return jsonify({
            "ok": True,
            "flashcard": {
                "id": card_id,
                "interval_days": interval_days,
                "ease": ease,
                "due_date": due,
            }
        })
    finally:
        conn.close()

# ---------------------------
# API: quizzes
# ---------------------------
@app.route("/api/quizzes", methods=["GET"])
@api_login_required
def api_quizzes_list():
    user_id = uid()
    conn = db()
    try:
        rows = conn.execute(
            """
            SELECT id, title, questions_json, created_at, updated_at
            FROM quizzes
            WHERE user_id=?
            ORDER BY created_at DESC
            """,
            (user_id,),
        ).fetchall()

        quizzes = []
        for r in rows:
            quizzes.append({
                "id": int(r["id"]),
                "title": r["title"],
                "questions_json": r["questions_json"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            })
        return jsonify({"ok": True, "quizzes": quizzes})
    finally:
        conn.close()


@app.route("/api/quizzes", methods=["POST"])
@api_login_required
def api_quizzes_create():
    user_id = int(uid())

    limits = get_limits_for_user(user_id)
    count = count_user_rows(user_id, "quizzes")
    if count >= limits["quizzes"]:
        return jsonify({
            "ok": False,
            "error": f"Quiz limit reached. Your plan allows {limits['quizzes']} quizzes."
        }), 403

    data = request.get_json(silent=True) or {}
    title = safe_text(data.get("title"), 120) or "Untitled Quiz"

    conn = db()
    try:
        cur = conn.execute(
            """
            INSERT INTO quizzes(user_id, title, questions_json, created_at, updated_at)
            VALUES(?,?,?,?,?)
            """,
            (user_id, title, "[]", now_iso(), now_iso()),
        )
        conn.commit()
        return jsonify({"ok": True, "quiz": {"id": int(cur.lastrowid), "title": title}})
    finally:
        conn.close()


@app.route("/api/quizzes/<int:quiz_id>", methods=["DELETE"])
@api_login_required
def api_quizzes_delete(quiz_id: int):
    user_id = uid()
    conn = db()
    try:
        conn.execute("DELETE FROM quizzes WHERE id=? AND user_id=?", (quiz_id, user_id))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


# ---------------------------
# API: plans
# ---------------------------
@app.route("/api/plan/generate", methods=["POST"])
@api_login_required
def api_plan_generate():
    user_id = int(uid())
    data = request.get_json(silent=True) or {}
    available = data.get("available_minutes")

    conn = db()
    try:
        s = conn.execute(
            "SELECT available_minutes FROM settings WHERE user_id=?",
            (user_id,),
        ).fetchone()
        default_avail = int(s["available_minutes"]) if s else 120
    finally:
        conn.close()

    available_minutes = default_avail if available is None else clamp_int(
        available, 0, 24 * 60, default_avail
    )

    try:
        payload = build_plan(user_id, int(available_minutes))
    except Exception as e:
        return jsonify({"ok": False, "error": f"Plan generation failed: {e}"}), 500

    conn = db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO daily_plan(user_id, day, payload_json, created_at) VALUES(?,?,?,?)",
            (user_id, today_iso(), json.dumps(payload), now_iso()),
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"ok": True, "plan": payload})


@app.route("/api/plan/today", methods=["GET"])
@api_login_required
def api_plan_today():
    user_id = int(uid())
    conn = db()
    try:
        row = conn.execute(
            "SELECT payload_json FROM daily_plan WHERE user_id=? AND day=?",
            (user_id, today_iso()),
        ).fetchone()

        if not row:
            return jsonify({"ok": True, "plan": None})

        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            payload = None

        return jsonify({"ok": True, "plan": payload})
    finally:
        conn.close()


# ---------------------------
# Uploads
# ---------------------------
@app.route("/uploads/<path:filename>")
@login_required
def uploads(filename: str):
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=False)


# ---------------------------
# Run
# ---------------------------
if __name__ == "__main__":
    ensure_schema()
    app.run(debug=True, port=5000)
