import os
import re
import secrets
import hashlib
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from flask import (
    Flask,
    request,
    jsonify,
    session,
    redirect,
    render_template,
)
from werkzeug.security import (
    generate_password_hash,
    check_password_hash,
)
from groq import Groq

import psycopg2
from psycopg2.extras import RealDictCursor


# =========================================================
# SETTINGS
# =========================================================

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]
SESSION_SECRET = os.environ["SESSION_SECRET"]

ADMIN_USERNAME = os.environ["ADMIN_USERNAME"]

COOKIE_SECURE = (
    os.environ.get("COOKIE_SECURE", "true").lower() == "true"
)

SMTP_HOST = os.environ.get(
    "SMTP_HOST",
    "smtp.gmail.com",
)

SMTP_PORT = int(
    os.environ.get(
        "SMTP_PORT",
        "587",
    )
)

SMTP_USERNAME = os.environ.get(
    "SMTP_USERNAME",
    "",
)

SMTP_PASSWORD = os.environ.get(
    "SMTP_PASSWORD",
    "",
)

SMTP_FROM = os.environ.get(
    "SMTP_FROM",
    SMTP_USERNAME,
)

TEXT_MODEL = "openai/gpt-oss-120b"

VOICE_MODEL = "whisper-large-v3-turbo"


# =========================================================
# APP
# =========================================================

app = Flask(
    __name__,
    template_folder="templates",
    static_folder="static",
)

app.secret_key = SESSION_SECRET

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


groq_client = Groq(
    api_key=GROQ_API_KEY
)


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - "
        "%(levelname)s - "
        "%(message)s"
    ),
)


# =========================================================
# DATABASE
# =========================================================

def get_db():
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
    )


def init_db():

    conn = get_db()

    try:
        with conn.cursor() as cur:

            # -------------------------------------------------
            # USERS
            # -------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS web_users (
                    id BIGSERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    email TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            # -------------------------------------------------
            # CHATS
            # -------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS web_chats (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL
                        REFERENCES web_users(id)
                        ON DELETE CASCADE,
                    title TEXT NOT NULL DEFAULT 'New chat',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            # -------------------------------------------------
            # MESSAGES
            # -------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS web_messages (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id BIGINT
                        REFERENCES web_chats(id)
                        ON DELETE CASCADE,
                    user_id BIGINT
                        REFERENCES web_users(id)
                        ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            # -------------------------------------------------
            # USAGE EVENTS
            # -------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS web_usage_events (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT
                        REFERENCES web_users(id)
                        ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            # -------------------------------------------------
            # PASSWORD RESETS
            # -------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS web_password_resets (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL
                        REFERENCES web_users(id)
                        ON DELETE CASCADE,
                    token_hash TEXT NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    used_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            # -------------------------------------------------
            # SAFE MIGRATIONS
            # -------------------------------------------------

            cur.execute(
                """
                ALTER TABLE web_messages
                ADD COLUMN IF NOT EXISTS chat_id BIGINT
                """
            )

            cur.execute(
                """
                ALTER TABLE web_messages
                ADD COLUMN IF NOT EXISTS user_id BIGINT
                """
            )

            cur.execute(
                """
                ALTER TABLE web_messages
                ADD COLUMN IF NOT EXISTS role TEXT
                """
            )

            cur.execute(
                """
                ALTER TABLE web_messages
                ADD COLUMN IF NOT EXISTS content TEXT
                """
            )

            cur.execute(
                """
                ALTER TABLE web_messages
                ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ
                    DEFAULT NOW()
                """
            )

            # -------------------------------------------------
            # INDEXES
            # -------------------------------------------------

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_web_chats_user_id
                ON web_chats(user_id)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_web_messages_chat_id
                ON web_messages(chat_id)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_web_messages_user_id
                ON web_messages(user_id)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_web_usage_user_id
                ON web_usage_events(user_id)
                """
            )

            # -------------------------------------------------
            # MIGRATE OLD WEB MESSAGES THAT HAVE NO CHAT
            # -------------------------------------------------

            cur.execute(
                """
                SELECT DISTINCT wm.user_id
                FROM web_messages wm
                WHERE wm.chat_id IS NULL
                  AND wm.user_id IS NOT NULL
                """
            )

            old_users = cur.fetchall()

            for row in old_users:

                user_id = row["user_id"]

                cur.execute(
                    """
                    SELECT id
                    FROM web_chats
                    WHERE user_id = %s
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (user_id,),
                )

                existing_chat = cur.fetchone()

                if existing_chat:
                    chat_id = existing_chat["id"]

                else:
                    cur.execute(
                        """
                        INSERT INTO web_chats
                        (user_id, title)
                        VALUES (%s, %s)
                        RETURNING id
                        """,
                        (
                            user_id,
                            "Previous chat",
                        ),
                    )

                    chat_id = cur.fetchone()["id"]

                cur.execute(
                    """
                    UPDATE web_messages
                    SET chat_id = %s
                    WHERE user_id = %s
                      AND chat_id IS NULL
                    """,
                    (
                        chat_id,
                        user_id,
                    ),
                )

        conn.commit()

    except Exception:
        conn.rollback()
        logging.exception(
            "Database initialization failed."
        )
        raise

    finally:
        conn.close()


# =========================================================
# HELPERS
# =========================================================

def utc_now():
    return datetime.now(timezone.utc)


def normalize_username(value):

    if value is None:
        return ""

    value = str(value).strip().lower()

    if value.startswith("@"):
        value = value[1:]

    return value


def is_admin(user):

    if not user:
        return False

    username = normalize_username(
        user["username"]
    )

    admin_username = normalize_username(
        ADMIN_USERNAME
    )

    return (
        username != ""
        and admin_username != ""
        and username == admin_username
    )


def get_current_user():

    user_id = session.get("user_id")

    if not user_id:
        return None

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id,
                    username,
                    email,
                    first_seen,
                    last_seen
                FROM web_users
                WHERE id = %s
                """,
                (user_id,),
            )

            user = cur.fetchone()

            return user

    finally:
        conn.close()


def require_login():

    user = get_current_user()

    if not user:
        return None

    return user


def csrf_token():

    token = session.get("csrf_token")

    if not token:

        token = secrets.token_urlsafe(32)

        session["csrf_token"] = token

    return token


def check_csrf():

    token = request.headers.get(
        "X-CSRF-Token"
    )

    if not token:
        token = request.form.get(
            "csrf_token"
        )

    stored = session.get(
        "csrf_token"
    )

    return (
        bool(token)
        and bool(stored)
        and secrets.compare_digest(
            token,
            stored,
        )
    )


def clean_username(username):

    username = username.strip()

    if not re.fullmatch(
        r"[A-Za-z0-9_]{3,32}",
        username,
    ):
        return None

    return username


def clean_email(email):

    email = email.strip().lower()

    if len(email) > 254:
        return None

    if not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[^@\s]+",
        email,
    ):
        return None

    return email


def valid_password(password):

    return (
        isinstance(password, str)
        and len(password) >= 8
        and len(password) <= 128
    )


def hash_reset_token(token):

    return hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()


def send_password_reset_email(
    email,
    reset_url,
):

    if not (
        SMTP_USERNAME
        and SMTP_PASSWORD
        and SMTP_FROM
    ):
        logging.warning(
            "SMTP is not configured."
        )
        return False

    message = EmailMessage()

    message["Subject"] = (
        "AskOra password reset"
    )

    message["From"] = SMTP_FROM
    message["To"] = email

    message.set_content(
        f"""
Hello,

Someone requested a password reset for your AskOra account.

Use the link below to reset your password:

{reset_url}

This link expires in 1 hour.

If you did not request this, you can ignore this email.

AskOra
Ask. Get answers.
""".strip()
    )

    try:

        with smtplib.SMTP(
            SMTP_HOST,
            SMTP_PORT,
            timeout=20,
        ) as smtp:

            smtp.starttls()

            smtp.login(
                SMTP_USERNAME,
                SMTP_PASSWORD,
            )

            smtp.send_message(
                message
            )

        return True

    except Exception:

        logging.exception(
            "Password reset email failed."
        )

        return False


def record_usage(
    user_id,
    event_type,
):

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO web_usage_events
                (user_id, event_type)
                VALUES (%s, %s)
                """,
                (
                    user_id,
                    event_type,
                ),
            )

        conn.commit()

    finally:
        conn.close()


def update_last_seen(user_id):

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE web_users
                SET last_seen = NOW()
                WHERE id = %s
                """,
                (user_id,),
            )

        conn.commit()

    finally:
        conn.close()


def split_message(
    text,
    max_length=4000,
):

    if not text:
        return [""]

    chunks = []

    while len(text) > max_length:

        split_at = text.rfind(
            "\n",
            0,
            max_length,
        )

        if split_at < 1000:

            split_at = text.rfind(
                " ",
                0,
                max_length,
            )

        if split_at < 1000:
            split_at = max_length

        chunks.append(
            text[:split_at]
        )

        text = text[split_at:].lstrip()

    if text:
        chunks.append(text)

    return chunks


# =========================================================
# CHAT HELPERS
# =========================================================

def get_or_create_chat(
    user_id,
    chat_id=None,
):

    conn = get_db()

    try:

        with conn.cursor() as cur:

            if chat_id:

                cur.execute(
                    """
                    SELECT id
                    FROM web_chats
                    WHERE id = %s
                      AND user_id = %s
                    """,
                    (
                        chat_id,
                        user_id,
                    ),
                )

                chat = cur.fetchone()

                if chat:
                    return chat["id"]

            cur.execute(
                """
                INSERT INTO web_chats
                (user_id, title)
                VALUES (%s, %s)
                RETURNING id
                """,
                (
                    user_id,
                    "New chat",
                ),
            )

            new_chat = cur.fetchone()

        conn.commit()

        return new_chat["id"]

    finally:
        conn.close()


def get_chat_messages(
    user_id,
    chat_id,
):

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    role,
                    content,
                    created_at
                FROM web_messages
                WHERE chat_id = %s
                  AND user_id = %s
                ORDER BY id ASC
                """,
                (
                    chat_id,
                    user_id,
                ),
            )

            return cur.fetchall()

    finally:
        conn.close()


def get_recent_user_context(
    user_id,
    current_chat_id,
    limit=8,
):

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    role,
                    content
                FROM web_messages
                WHERE user_id = %s
                  AND chat_id != %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (
                    user_id,
                    current_chat_id,
                    limit,
                ),
            )

            rows = cur.fetchall()

            rows.reverse()

            return rows

    finally:
        conn.close()


def save_message(
    user_id,
    chat_id,
    role,
    content,
):

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO web_messages
                (
                    chat_id,
                    user_id,
                    role,
                    content
                )
                VALUES
                (%s, %s, %s, %s)
                """,
                (
                    chat_id,
                    user_id,
                    role,
                    content,
                ),
            )

            cur.execute(
                """
                UPDATE web_chats
                SET updated_at = NOW()
                WHERE id = %s
                """,
                (chat_id,),
            )

        conn.commit()

    finally:
        conn.close()


def update_chat_title(
    user_id,
    chat_id,
    title,
):

    title = title.strip()

    if not title:
        return

    title = title[:120]

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE web_chats
                SET title = %s,
                    updated_at = NOW()
                WHERE id = %s
                  AND user_id = %s
                  AND title = 'New chat'
                """,
                (
                    title,
                    chat_id,
                    user_id,
                ),
            )

        conn.commit()

    finally:
        conn.close()


# =========================================================
# AUTH
# =========================================================

@app.route(
    "/login",
    methods=["GET", "POST"],
)
def login():

    if request.method == "GET":
        return render_template(
            "index.html"
        )

    username_or_email = (
        request.form.get(
            "username",
            "",
        )
        .strip()
    )

    password = request.form.get(
        "password",
        "",
    )

    if not username_or_email or not password:

        return jsonify(
            {
                "ok": False,
                "error": (
                    "Username/email and "
                    "password are required."
                ),
            }
        ), 400

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT *
                FROM web_users
                WHERE LOWER(username) = LOWER(%s)
                   OR LOWER(email) = LOWER(%s)
                LIMIT 1
                """,
                (
                    username_or_email,
                    username_or_email,
                ),
            )

            user = cur.fetchone()

            if not user:

                return jsonify(
                    {
                        "ok": False,
                        "error": (
                            "Invalid username/email "
                            "or password."
                        ),
                    }
                ), 401

            if not check_password_hash(
                user["password_hash"],
                password,
            ):

                return jsonify(
                    {
                        "ok": False,
                        "error": (
                            "Invalid username/email "
                            "or password."
                        ),
                    }
                ), 401

            session.permanent = True
            session["user_id"] = user["id"]

            csrf_token()

            cur.execute(
                """
                UPDATE web_users
                SET last_seen = NOW()
                WHERE id = %s
                """,
                (user["id"],),
            )

        conn.commit()

        return jsonify(
            {
                "ok": True,
                "user": {
                    "id": user["id"],
                    "username": user["username"],
                    "email": user["email"],
                    "is_admin": is_admin(user),
                },
            }
        )

    finally:
        conn.close()


@app.route(
    "/register",
    methods=["GET", "POST"],
)
def register():

    if request.method == "GET":
        return render_template(
            "index.html"
        )

    username = clean_username(
        request.form.get(
            "username",
            "",
        )
    )

    email = clean_email(
        request.form.get(
            "email",
            "",
        )
    )

    password = request.form.get(
        "password",
        "",
    )

    if not username:

        return jsonify(
            {
                "ok": False,
                "error": (
                    "Username must be 3–32 "
                    "characters and contain only "
                    "letters, numbers or underscores."
                ),
            }
        ), 400

    if not email:

        return jsonify(
            {
                "ok": False,
                "error": "Enter a valid email address.",
            }
        ), 400

    if not valid_password(password):

        return jsonify(
            {
                "ok": False,
                "error": (
                    "Password must be between "
                    "8 and 128 characters."
                ),
            }
        ), 400

    password_hash = generate_password_hash(
        password
    )

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id
                FROM web_users
                WHERE LOWER(username) = LOWER(%s)
                   OR LOWER(email) = LOWER(%s)
                LIMIT 1
                """,
                (
                    username,
                    email,
                ),
            )

            existing = cur.fetchone()

            if existing:

                return jsonify(
                    {
                        "ok": False,
                        "error": (
                            "Username or email "
                            "is already registered."
                        ),
                    }
                ), 409

            cur.execute(
                """
                INSERT INTO web_users
                (
                    username,
                    email,
                    password_hash
                )
                VALUES
                (%s, %s, %s)
                RETURNING id
                """,
                (
                    username,
                    email,
                    password_hash,
                ),
            )

            user = cur.fetchone()

        conn.commit()

        session.permanent = True
        session["user_id"] = user["id"]

        csrf_token()

        return jsonify(
            {
                "ok": True,
                "user": {
                    "id": user["id"],
                    "username": username,
                    "email": email,
                    "is_admin": (
                        normalize_username(username)
                        == normalize_username(
                            ADMIN_USERNAME
                        )
                    ),
                },
            }
        )

    except psycopg2.errors.UniqueViolation:

        conn.rollback()

        return jsonify(
            {
                "ok": False,
                "error": (
                    "Username or email "
                    "is already registered."
                ),
            }
        ), 409

    finally:
        conn.close()


@app.route(
    "/logout",
    methods=["POST", "GET"],
)
def logout():

    session.clear()

    return jsonify(
        {
            "ok": True
        }
    )


# =========================================================
# PASSWORD RESET
# =========================================================

@app.route(
    "/forgot-password",
    methods=["GET", "POST"],
)
def forgot_password():

    if request.method == "GET":

        return render_template(
            "index.html"
        )

    email = clean_email(
        request.form.get(
            "email",
            "",
        )
    )

    if not email:

        return jsonify(
            {
                "ok": False,
                "error": "Enter a valid email address.",
            }
        ), 400

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id
                FROM web_users
                WHERE LOWER(email) = LOWER(%s)
                LIMIT 1
                """,
                (email,),
            )

            user = cur.fetchone()

            # Always return the same message
            # so account existence is not revealed.

            if user:

                token = secrets.token_urlsafe(
                    48
                )

                token_hash = hash_reset_token(
                    token
                )

                expires_at = (
                    utc_now()
                    + timedelta(hours=1)
                )

                cur.execute(
                    """
                    UPDATE web_password_resets
                    SET used_at = NOW()
                    WHERE user_id = %s
                      AND used_at IS NULL
                    """,
                    (user["id"],),
                )

                cur.execute(
                    """
                    INSERT INTO web_password_resets
                    (
                        user_id,
                        token_hash,
                        expires_at
                    )
                    VALUES
                    (%s, %s, %s)
                    """,
                    (
                        user["id"],
                        token_hash,
                        expires_at,
                    ),
                )

                reset_url = (
                    request.url_root.rstrip("/")
                    + "/reset-password?token="
                    + token
                )

                send_password_reset_email(
                    email,
                    reset_url,
                )

        conn.commit()

    finally:
        conn.close()

    return jsonify(
        {
            "ok": True,
            "message": (
                "If that email is registered, "
                "a password reset link has been sent."
            ),
        }
    )


@app.route(
    "/reset-password",
    methods=["GET", "POST"],
)
def reset_password():

    if request.method == "GET":

        return render_template(
            "index.html"
        )

    token = request.form.get(
        "token",
        "",
    ).strip()

    new_password = request.form.get(
        "password",
        "",
    )

    if not token:

        return jsonify(
            {
                "ok": False,
                "error": "Invalid reset token.",
            }
        ), 400

    if not valid_password(
        new_password
    ):

        return jsonify(
            {
                "ok": False,
                "error": (
                    "Password must be between "
                    "8 and 128 characters."
                ),
            }
        ), 400

    token_hash = hash_reset_token(
        token
    )

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT *
                FROM web_password_resets
                WHERE token_hash = %s
                  AND used_at IS NULL
                  AND expires_at > NOW()
                ORDER BY id DESC
                LIMIT 1
                """,
                (token_hash,),
            )

            reset = cur.fetchone()

            if not reset:

                return jsonify(
                    {
                        "ok": False,
                        "error": (
                            "This reset link is "
                            "invalid or expired."
                        ),
                    }
                ), 400

            new_hash = (
                generate_password_hash(
                    new_password
                )
            )

            cur.execute(
                """
                UPDATE web_users
                SET password_hash = %s
                WHERE id = %s
                """,
                (
                    new_hash,
                    reset["user_id"],
                ),
            )

            cur.execute(
                """
                UPDATE web_password_resets
                SET used_at = NOW()
                WHERE id = %s
                """,
                (reset["id"],),
            )

        conn.commit()

    finally:
        conn.close()

    return jsonify(
        {
            "ok": True,
            "message": (
                "Password changed successfully."
            ),
        }
    )


# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():

    return render_template(
        "index.html"
    )


# =========================================================
# CURRENT USER
# =========================================================

@app.route(
    "/api/me",
    methods=["GET"],
)
def api_me():

    user = get_current_user()

    if not user:

        return jsonify(
            {
                "ok": False,
                "authenticated": False,
            }
        )

    update_last_seen(
        user["id"]
    )

    return jsonify(
        {
            "ok": True,
            "authenticated": True,
            "csrf_token": csrf_token(),
            "user": {
                "id": user["id"],
                "username": user["username"],
                "email": user["email"],
                "is_admin": is_admin(user),
            },
        }
    )


# =========================================================
# CHATS
# =========================================================

@app.route(
    "/api/chats",
    methods=["GET", "POST"],
)
def api_chats():

    user = require_login()

    if not user:

        return jsonify(
            {
                "ok": False,
                "error": "Please log in.",
            }
        ), 401

    user_id = user["id"]

    if request.method == "POST":

        if not check_csrf():

            return jsonify(
                {
                    "ok": False,
                    "error": "Invalid request.",
                }
            ), 403

        chat_id = get_or_create_chat(
            user_id
        )

        return jsonify(
            {
                "ok": True,
                "chat": {
                    "id": chat_id,
                    "title": "New chat",
                },
            }
        )

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id,
                    title,
                    created_at,
                    updated_at
                FROM web_chats
                WHERE user_id = %s
                ORDER BY updated_at DESC
                """,
                (user_id,),
            )

            chats = cur.fetchall()

        return jsonify(
            {
                "ok": True,
                "chats": chats,
            }
        )

    finally:
        conn.close()


@app.route(
    "/api/chats/<int:chat_id>/messages",
    methods=["GET"],
)
def api_chat_messages(
    chat_id,
):

    user = require_login()

    if not user:

        return jsonify(
            {
                "ok": False,
                "error": "Please log in.",
            }
        ), 401

    messages = get_chat_messages(
        user["id"],
        chat_id,
    )

    return jsonify(
        {
            "ok": True,
            "messages": messages,
        }
    )


@app.route(
    "/api/chats/<int:chat_id>",
    methods=["DELETE"],
)
def delete_chat(
    chat_id,
):

    user = require_login()

    if not user:

        return jsonify(
            {
                "ok": False,
                "error": "Please log in.",
            }
        ), 401

    if not check_csrf():

        return jsonify(
            {
                "ok": False,
                "error": "Invalid request.",
            }
        ), 403

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                DELETE FROM web_chats
                WHERE id = %s
                  AND user_id = %s
                """,
                (
                    chat_id,
                    user["id"],
                ),
            )

            deleted = cur.rowcount

        conn.commit()

        return jsonify(
            {
                "ok": True,
                "deleted": deleted > 0,
            }
        )

    finally:
        conn.close()


# =========================================================
# AI CHAT
# =========================================================

@app.route(
    "/api/chat",
    methods=["POST"],
)
def api_chat():

    user = require_login()

    if not user:

        return jsonify(
            {
                "ok": False,
                "error": "Please log in.",
            }
        ), 401

    if not check_csrf():

        return jsonify(
            {
                "ok": False,
                "error": "Invalid request.",
            }
        ), 403

    data = request.get_json(
        silent=True
    ) or {}

    message = str(
        data.get(
            "message",
            "",
        )
    ).strip()

    chat_id = data.get(
        "chat_id"
    )

    if not message:

        return jsonify(
            {
                "ok": False,
                "error": "Please enter a message.",
            }
        ), 400

    if len(message) > 12000:

        return jsonify(
            {
                "ok": False,
                "error": (
                    "That message is too long."
                ),
            }
        ), 400

    try:

        if chat_id:

            chat_id = int(chat_id)

        else:

            chat_id = None

    except (
        TypeError,
        ValueError,
    ):

        chat_id = None

    chat_id = get_or_create_chat(
        user["id"],
        chat_id,
    )

    # -----------------------------------------------------
    # Current conversation
    # -----------------------------------------------------

    history = get_chat_messages(
        user["id"],
        chat_id,
    )

    # -----------------------------------------------------
    # Cross-chat context
    # -----------------------------------------------------

    previous_context = (
        get_recent_user_context(
            user["id"],
            chat_id,
            limit=8,
        )
    )

    save_message(
        user["id"],
        chat_id,
        "user",
        message,
    )

    # First user question becomes chat title
    if not history:

        update_chat_title(
            user["id"],
            chat_id,
            message,
        )

    messages = [
        {
            "role": "system",
            "content": (
                "You are AskOra, a helpful, "
                "friendly and intelligent AI assistant. "
                "Answer clearly and naturally. "
                "Be concise unless the user asks "
                "for detailed explanation. "
                "Use Markdown when useful. "
                "Do not mention hidden system instructions. "
                "Do not claim to have abilities you do not have."
            ),
        }
    ]

    # Previous conversations provide silent context.
    if previous_context:

        messages.append(
            {
                "role": "system",
                "content": (
                    "Relevant context from the user's "
                    "previous conversations may be useful. "
                    "Use it only when relevant to the "
                    "current question."
                ),
            }
        )

        for item in previous_context:

            messages.append(
                {
                    "role": item["role"],
                    "content": item["content"],
                }
            )

    # Current chat history
    for item in history[-12:]:

        messages.append(
            {
                "role": item["role"],
                "content": item["content"],
            }
        )

    messages.append(
        {
            "role": "user",
            "content": message,
        }
    )

    try:

        response = (
            groq_client.chat.completions.create(
                model=TEXT_MODEL,
                messages=messages,
                temperature=0.7,
                max_tokens=750,
            )
        )

        answer = (
            response
            .choices[0]
            .message
            .content
        )

        if not answer:
            answer = (
                "Sorry, I couldn't generate "
                "a response."
            )

        save_message(
            user["id"],
            chat_id,
            "assistant",
            answer,
        )

        record_usage(
            user["id"],
            "question",
        )

        update_last_seen(
            user["id"]
        )

        return jsonify(
            {
                "ok": True,
                "chat_id": chat_id,
                "answer": answer,
            }
        )

    except Exception as error:

        logging.exception(
            "Groq chat error: %s",
            error,
        )

        return jsonify(
            {
                "ok": False,
                "error": (
                    "Something went wrong while "
                    "generating the response."
                ),
            }
        ), 500


# =========================================================
# VOICE
# =========================================================

@app.route(
    "/api/voice",
    methods=["POST"],
)
def api_voice():

    user = require_login()

    if not user:

        return jsonify(
            {
                "ok": False,
                "error": "Please log in.",
            }
        ), 401

    if not check_csrf():

        return jsonify(
            {
                "ok": False,
                "error": "Invalid request.",
            }
        ), 403

    if "audio" not in request.files:

        return jsonify(
            {
                "ok": False,
                "error": "No audio file received.",
            }
        ), 400

    audio = request.files[
        "audio"
    ]

    if not audio.filename:

        return jsonify(
            {
                "ok": False,
                "error": "Invalid audio file.",
            }
        ), 400

    try:

        audio_bytes = audio.read()

        if not audio_bytes:

            return jsonify(
                {
                    "ok": False,
                    "error": "Audio file is empty.",
                }
            ), 400

        if len(audio_bytes) > 15 * 1024 * 1024:

            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "Audio file is too large."
                    ),
                }
            ), 400

        transcript = (
            groq_client.audio.transcriptions.create(
                file=(
                    audio.filename,
                    audio_bytes,
                ),
                model=VOICE_MODEL,
            )
        )

        text = getattr(
            transcript,
            "text",
            "",
        )

        text = (
            text.strip()
            if text
            else ""
        )

        if not text:

            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "I couldn't understand "
                        "the recording."
                    ),
                }
            ), 400

        record_usage(
            user["id"],
            "voice",
        )

        return jsonify(
            {
                "ok": True,
                "text": text,
            }
        )

    except Exception as error:

        logging.exception(
            "Voice transcription error: %s",
            error,
        )

        return jsonify(
            {
                "ok": False,
                "error": (
                    "Something went wrong while "
                    "processing your voice message."
                ),
            }
        ), 500


# =========================================================
# RESET CURRENT CHAT
# =========================================================

@app.route(
    "/api/reset",
    methods=["POST"],
)
def api_reset():

    user = require_login()

    if not user:

        return jsonify(
            {
                "ok": False,
                "error": "Please log in.",
            }
        ), 401

    if not check_csrf():

        return jsonify(
            {
                "ok": False,
                "error": "Invalid request.",
            }
        ), 403

    data = request.get_json(
        silent=True
    ) or {}

    chat_id = data.get(
        "chat_id"
    )

    if not chat_id:

        return jsonify(
            {
                "ok": False,
                "error": "Chat ID is required.",
            }
        ), 400

    try:
        chat_id = int(chat_id)

    except (
        TypeError,
        ValueError,
    ):

        return jsonify(
            {
                "ok": False,
                "error": "Invalid chat ID.",
            }
        ), 400

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                DELETE FROM web_messages
                WHERE chat_id = %s
                  AND user_id = %s
                """,
                (
                    chat_id,
                    user["id"],
                ),
            )

            cur.execute(
                """
                UPDATE web_chats
                SET title = 'New chat',
                    updated_at = NOW()
                WHERE id = %s
                  AND user_id = %s
                """,
                (
                    chat_id,
                    user["id"],
                ),
            )

        conn.commit()

        return jsonify(
            {
                "ok": True,
            }
        )

    finally:
        conn.close()


# =========================================================
# DELETE ACCOUNT
# =========================================================

@app.route(
    "/api/account/delete",
    methods=["POST"],
)
def delete_account():

    user = require_login()

    if not user:

        return jsonify(
            {
                "ok": False,
                "error": "Please log in.",
            }
        ), 401

    if not check_csrf():

        return jsonify(
            {
                "ok": False,
                "error": "Invalid request.",
            }
        ), 403

    data = request.get_json(
        silent=True
    ) or {}

    password = data.get(
        "password",
        "",
    )

    if not password:

        return jsonify(
            {
                "ok": False,
                "error": (
                    "Enter your password "
                    "to delete your account."
                ),
            }
        ), 400

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT password_hash
                FROM web_users
                WHERE id = %s
                """,
                (user["id"],),
            )

            account = cur.fetchone()

            if not account:

                session.clear()

                return jsonify(
                    {
                        "ok": False,
                        "error": (
                            "Account not found."
                        ),
                    }
                ), 404

            if not check_password_hash(
                account["password_hash"],
                password,
            ):

                return jsonify(
                    {
                        "ok": False,
                        "error": (
                            "Incorrect password."
                        ),
                    }
                ), 401

            cur.execute(
                """
                DELETE FROM web_users
                WHERE id = %s
                """,
                (user["id"],),
            )

        conn.commit()

        session.clear()

        return jsonify(
            {
                "ok": True,
            }
        )

    finally:
        conn.close()


# =========================================================
# ADMIN
# =========================================================

@app.route("/admin")
def admin_page():

    user = get_current_user()

    if not user:

        return redirect("/")

    if not is_admin(user):

        return redirect("/")

    return render_template(
        "admin.html"
    )


@app.route(
    "/api/admin/stats",
    methods=["GET"],
)
def admin_stats():

    user = get_current_user()

    if not user:

        return jsonify(
            {
                "ok": False,
                "error": "Unauthorized.",
            }
        ), 401

    if not is_admin(user):

        return jsonify(
            {
                "ok": False,
                "error": "Unauthorized.",
            }
        ), 403

    conn = get_db()

    try:

        with conn.cursor() as cur:

            # Total users
            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_users
                """
            )

            total_users = cur.fetchone()[
                "count"
            ]

            # Total chats
            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_chats
                """
            )

            total_chats = cur.fetchone()[
                "count"
            ]

            # Total questions
            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_usage_events
                WHERE event_type = 'question'
                """
            )

            total_questions = cur.fetchone()[
                "count"
            ]

            # Voice requests
            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_usage_events
                WHERE event_type = 'voice'
                """
            )

            voice_requests = cur.fetchone()[
                "count"
            ]

            # Users today
            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_users
                WHERE first_seen >= CURRENT_DATE
                """
            )

            users_today = cur.fetchone()[
                "count"
            ]

        return jsonify(
            {
                "ok": True,
                "stats": {
                    "total_users": total_users,
                    "total_chats": total_chats,
                    "total_questions": total_questions,
                    "voice_requests": voice_requests,
                    "users_today": users_today,
                },
            }
        )

    finally:
        conn.close()


# =========================================================
# HEALTH
# =========================================================

@app.route("/health")
def health():

    try:

        conn = get_db()

        try:

            with conn.cursor() as cur:

                cur.execute(
                    "SELECT 1"
                )

                cur.fetchone()

        finally:
            conn.close()

        return jsonify(
            {
                "ok": True,
                "status": "healthy",
            }
        )

    except Exception:

        logging.exception(
            "Health check failed."
        )

        return jsonify(
            {
                "ok": False,
                "status": "unhealthy",
            }
        ), 500


# =========================================================
# ERROR HANDLERS
# =========================================================

@app.errorhandler(404)
def not_found(error):

    if request.path.startswith(
        "/api/"
    ):

        return jsonify(
            {
                "ok": False,
                "error": "Not found.",
            }
        ), 404

    return render_template(
        "index.html"
    )


@app.errorhandler(500)
def internal_error(error):

    logging.exception(
        "Internal server error: %s",
        error,
    )

    if request.path.startswith(
        "/api/"
    ):

        return jsonify(
            {
                "ok": False,
                "error": (
                    "Internal server error."
                ),
            }
        ), 500

    return (
        "Something went wrong.",
        500,
    )


# =========================================================
# STARTUP
# =========================================================

try:

    init_db()

    logging.info(
        "AskOra database initialized."
    )

    logging.info(
        "Configured admin username: %s",
        normalize_username(
            ADMIN_USERNAME
        ),
    )

except Exception:

    logging.exception(
        "AskOra startup database initialization failed."
    )


# =========================================================
# LOCAL RUN
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "10000",
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
