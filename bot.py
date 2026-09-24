import os
import secrets
import hashlib
import logging
import smtplib
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    session,
    redirect,
)
from werkzeug.security import (
    generate_password_hash,
    check_password_hash,
)
from groq import Groq


# ============================================================
# CONFIG
# ============================================================

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]
SESSION_SECRET = os.environ["SESSION_SECRET"]
ADMIN_USERNAME = os.environ["ADMIN_USERNAME"]

COOKIE_SECURE = (
    os.environ.get("COOKIE_SECURE", "true").lower()
    == "true"
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

CHAT_MODEL = "openai/gpt-oss-120b"
VOICE_MODEL = "whisper-large-v3-turbo"

MAX_MESSAGE_LENGTH = 12000

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - "
        "%(levelname)s - "
        "%(message)s"
    ),
)

logger = logging.getLogger("askora")


# ============================================================
# FLASK
# ============================================================

app = Flask(
    __name__,
    template_folder="templates",
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


# ============================================================
# DATABASE
# ============================================================

def get_db():
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
    )


def init_db():

    conn = get_db()

    try:

        with conn.cursor() as cur:

            # ------------------------------------------------
            # USERS
            # ------------------------------------------------

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

            # Safe migrations.
            cur.execute(
                """
                ALTER TABLE web_users
                ADD COLUMN IF NOT EXISTS email TEXT
                """
            )

            cur.execute(
                """
                ALTER TABLE web_users
                ADD COLUMN IF NOT EXISTS password_hash TEXT
                """
            )

            cur.execute(
                """
                ALTER TABLE web_users
                ADD COLUMN IF NOT EXISTS first_seen
                TIMESTAMPTZ DEFAULT NOW()
                """
            )

            cur.execute(
                """
                ALTER TABLE web_users
                ADD COLUMN IF NOT EXISTS last_seen
                TIMESTAMPTZ DEFAULT NOW()
                """
            )

            # ------------------------------------------------
            # CHATS
            # ------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS web_chats (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    title TEXT NOT NULL DEFAULT 'New chat',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            cur.execute(
                """
                ALTER TABLE web_chats
                ADD COLUMN IF NOT EXISTS user_id BIGINT
                """
            )

            cur.execute(
                """
                ALTER TABLE web_chats
                ADD COLUMN IF NOT EXISTS title TEXT
                """
            )

            cur.execute(
                """
                ALTER TABLE web_chats
                ADD COLUMN IF NOT EXISTS created_at
                TIMESTAMPTZ DEFAULT NOW()
                """
            )

            cur.execute(
                """
                ALTER TABLE web_chats
                ADD COLUMN IF NOT EXISTS updated_at
                TIMESTAMPTZ DEFAULT NOW()
                """
            )

            # ------------------------------------------------
            # MESSAGES
            # ------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS web_messages (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    user_id BIGINT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

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
                ADD COLUMN IF NOT EXISTS created_at
                TIMESTAMPTZ DEFAULT NOW()
                """
            )

            # ------------------------------------------------
            # USAGE
            # ------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS web_usage_events (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            # ------------------------------------------------
            # PASSWORD RESET
            # ------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS web_password_resets (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    token_hash TEXT NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    used_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            # ------------------------------------------------
            # INDEXES
            # ------------------------------------------------

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_web_chats_user
                ON web_chats(user_id)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_web_messages_chat
                ON web_messages(chat_id)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_web_messages_user
                ON web_messages(user_id)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_web_usage_user
                ON web_usage_events(user_id)
                """
            )

            # ------------------------------------------------
            # MIGRATE OLD NULL-CHAT MESSAGES
            #
            # Existing Telegram tables are NOT touched.
            # ------------------------------------------------

            cur.execute(
                """
                SELECT DISTINCT user_id
                FROM web_messages
                WHERE chat_id IS NULL
                AND user_id IS NOT NULL
                """
            )

            old_users = cur.fetchall()

            for old_user in old_users:

                user_id = old_user["user_id"]

                cur.execute(
                    """
                    SELECT id
                    FROM web_chats
                    WHERE user_id = %s
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
                        VALUES
                            (%s, %s)
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

        logger.info(
            "AskOra database initialized successfully."
        )

    except Exception:

        conn.rollback()

        logger.exception(
            "Database initialization failed."
        )

        raise

    finally:
        conn.close()


# ============================================================
# CSRF
# ============================================================

def get_csrf_token():

    token = session.get("csrf_token")

    if not token:

        token = secrets.token_urlsafe(32)

        session["csrf_token"] = token

    return token


def check_csrf():

    expected = session.get(
        "csrf_token"
    )

    received = (
        request.headers.get(
            "X-CSRF-Token"
        )
        or request.form.get(
            "csrf_token"
        )
    )

    return bool(
        expected
        and received
        and secrets.compare_digest(
            expected,
            received,
        )
    )


# ============================================================
# AUTH HELPERS
# ============================================================

def current_user():

    user_id = session.get(
        "user_id"
    )

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

            return cur.fetchone()

    finally:
        conn.close()


def require_login():

    user = current_user()

    if not user:
        return None

    return user


def is_admin(user):

    if not user:
        return False

    return (
        user["username"]
        == ADMIN_USERNAME
    )


def touch_user(user_id):

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


def log_usage(
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
                VALUES
                    (%s, %s)
                """,
                (
                    user_id,
                    event_type,
                ),
            )

        conn.commit()

    finally:
        conn.close()


# ============================================================
# AUTH ROUTES
# ============================================================

@app.route(
    "/login",
    methods=["GET", "POST"],
)
def login():

    if request.method == "GET":

        if current_user():
            return redirect("/")

        return render_template(
            "index.html",
            csrf_token=get_csrf_token(),
        )

    if not check_csrf():
        return jsonify(
            {
                "ok": False,
                "error": "Invalid request.",
            }
        ), 403

    identity = (
        request.form.get(
            "identity"
        )
        or ""
    ).strip()

    password = (
        request.form.get(
            "password"
        )
        or ""
    )

    if not identity or not password:

        return jsonify(
            {
                "ok": False,
                "error":
                    "Enter your login details.",
            }
        )

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT *
                FROM web_users
                WHERE LOWER(username)
                    = LOWER(%s)
                OR LOWER(email)
                    = LOWER(%s)
                LIMIT 1
                """,
                (
                    identity,
                    identity,
                ),
            )

            user = cur.fetchone()

    finally:
        conn.close()

    if not user:

        return jsonify(
            {
                "ok": False,
                "error":
                    "Invalid username/email or password.",
            }
        )

    try:

        valid = check_password_hash(
            user["password_hash"],
            password,
        )

    except Exception:

        valid = False

    if not valid:

        return jsonify(
            {
                "ok": False,
                "error":
                    "Invalid username/email or password.",
            }
        )

    session.clear()

    session.permanent = True

    session["user_id"] = user["id"]

    session["csrf_token"] = (
        secrets.token_urlsafe(32)
    )

    touch_user(
        user["id"]
    )

    return jsonify(
        {
            "ok": True,
            "redirect": "/",
        }
    )


@app.route(
    "/register",
    methods=["GET", "POST"],
)
def register():

    if request.method == "GET":

        if current_user():
            return redirect("/")

        return render_template(
            "index.html",
            csrf_token=get_csrf_token(),
        )

    if not check_csrf():
        return jsonify(
            {
                "ok": False,
                "error": "Invalid request.",
            }
        ), 403

    username = (
        request.form.get(
            "username"
        )
        or ""
    ).strip()

    email = (
        request.form.get(
            "email"
        )
        or ""
    ).strip().lower()

    password = (
        request.form.get(
            "password"
        )
        or ""
    )

    if len(username) < 3:

        return jsonify(
            {
                "ok": False,
                "error":
                    "Username must be at least 3 characters.",
            }
        )

    if len(password) < 8:

        return jsonify(
            {
                "ok": False,
                "error":
                    "Password must be at least 8 characters.",
            }
        )

    if "@" not in email:

        return jsonify(
            {
                "ok": False,
                "error":
                    "Enter a valid email.",
            }
        )

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id
                FROM web_users
                WHERE LOWER(username)
                    = LOWER(%s)
                LIMIT 1
                """,
                (username,),
            )

            if cur.fetchone():

                return jsonify(
                    {
                        "ok": False,
                        "error":
                            "That username is already taken.",
                    }
                )

            cur.execute(
                """
                SELECT id
                FROM web_users
                WHERE LOWER(email)
                    = LOWER(%s)
                LIMIT 1
                """,
                (email,),
            )

            if cur.fetchone():

                return jsonify(
                    {
                        "ok": False,
                        "error":
                            "That email is already registered.",
                    }
                )

            password_hash = (
                generate_password_hash(
                    password
                )
            )

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

            user_id = cur.fetchone()["id"]

        conn.commit()

    except Exception as error:

        conn.rollback()

        logger.exception(
            "Registration error: %s",
            error,
        )

        return jsonify(
            {
                "ok": False,
                "error":
                    "Could not create account.",
            }
        ), 500

    finally:
        conn.close()

    session.clear()

    session.permanent = True

    session["user_id"] = user_id

    session["csrf_token"] = (
        secrets.token_urlsafe(32)
    )

    return jsonify(
        {
            "ok": True,
            "redirect": "/",
        }
    )


@app.route(
    "/logout",
    methods=["POST"],
)
def logout():

    if not check_csrf():
        return jsonify(
            {
                "ok": False,
                "error": "Invalid request.",
            }
        ), 403

    session.clear()

    return jsonify(
        {
            "ok": True,
            "redirect": "/login",
        }
    )


# ============================================================
# PASSWORD RESET
# ============================================================

def hash_token(token):

    return hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()


def send_reset_email(
    email,
    reset_url,
):

    if not (
        SMTP_USERNAME
        and SMTP_PASSWORD
        and SMTP_FROM
    ):

        logger.warning(
            "SMTP is not configured. "
            "Reset URL: %s",
            reset_url,
        )

        return False

    message = EmailMessage()

    message["Subject"] = (
        "AskOra password reset"
    )

    message["From"] = SMTP_FROM
    message["To"] = email

    message.set_content(
        "You requested a password reset "
        "for your AskOra account.\n\n"
        "Open this link to reset your password:\n\n"
        f"{reset_url}\n\n"
        "This link expires in 30 minutes.\n\n"
        "If you did not request this, "
        "you can ignore this email."
    )

    with smtplib.SMTP(
        SMTP_HOST,
        SMTP_PORT,
        timeout=30,
    ) as server:

        server.starttls()

        server.login(
            SMTP_USERNAME,
            SMTP_PASSWORD,
        )

        server.send_message(
            message
        )

    return True


@app.route(
    "/forgot-password",
    methods=["GET", "POST"],
)
def forgot_password():

    if request.method == "GET":

        return render_template(
            "index.html",
            csrf_token=get_csrf_token(),
        )

    if not check_csrf():
        return jsonify(
            {
                "ok": False,
                "error": "Invalid request.",
            }
        ), 403

    email = (
        request.form.get(
            "email"
        )
        or ""
    ).strip().lower()

    generic_message = (
        "If that email exists, "
        "a password reset link has been sent."
    )

    if not email:

        return jsonify(
            {
                "ok": True,
                "message":
                    generic_message,
            }
        )

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id, email
                FROM web_users
                WHERE LOWER(email)
                    = LOWER(%s)
                LIMIT 1
                """,
                (email,),
            )

            user = cur.fetchone()

            if not user:

                return jsonify(
                    {
                        "ok": True,
                        "message":
                            generic_message,
                    }
                )

            token = secrets.token_urlsafe(
                48
            )

            token_hash = hash_token(
                token
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
                    (
                        %s,
                        %s,
                        NOW() + INTERVAL '30 minutes'
                    )
                """,
                (
                    user["id"],
                    token_hash,
                ),
            )

        conn.commit()

    finally:
        conn.close()

    base_url = request.host_url.rstrip("/")

    reset_url = (
        f"{base_url}/reset-password"
        f"?token={token}"
    )

    try:

        send_reset_email(
            user["email"],
            reset_url,
        )

    except Exception as error:

        logger.exception(
            "Password reset email error: %s",
            error,
        )

    return jsonify(
        {
            "ok": True,
            "message":
                generic_message,
        }
    )


@app.route(
    "/reset-password",
    methods=["GET", "POST"],
)
def reset_password():

    token = (
        request.args.get(
            "token"
        )
        or ""
    ).strip()

    if request.method == "GET":

        return render_template(
            "index.html",
            csrf_token=get_csrf_token(),
        )

    if not check_csrf():
        return jsonify(
            {
                "ok": False,
                "error": "Invalid request.",
            }
        ), 403

    password = (
        request.form.get(
            "password"
        )
        or ""
    )

    if len(password) < 8:

        return jsonify(
            {
                "ok": False,
                "error":
                    "Password must be at least 8 characters.",
            }
        )

    if not token:

        return jsonify(
            {
                "ok": False,
                "error":
                    "Invalid reset link.",
            }
        )

    token_hash = hash_token(
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
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (token_hash,),
            )

            reset = cur.fetchone()

            if not reset:

                return jsonify(
                    {
                        "ok": False,
                        "error":
                            "This reset link is invalid or expired.",
                    }
                )

            password_hash = (
                generate_password_hash(
                    password
                )
            )

            cur.execute(
                """
                UPDATE web_users
                SET password_hash = %s,
                    last_seen = NOW()
                WHERE id = %s
                """,
                (
                    password_hash,
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
            "redirect": "/login",
        }
    )


# ============================================================
# MAIN PAGE
# ============================================================

@app.route("/")
def home():

    user = current_user()

    if not user:
        return redirect("/login")

    return render_template(
        "index.html",
        csrf_token=get_csrf_token(),
    )


@app.route("/api/me")
def api_me():

    user = current_user()

    if not user:

        return jsonify(
            {
                "ok": False,
                "error":
                    "Not authenticated.",
            }
        ), 401

    return jsonify(
        {
            "ok": True,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "email": user["email"],
                "is_admin":
                    is_admin(user),
            },
        }
    )


# ============================================================
# CHATS
# ============================================================

@app.route(
    "/api/chats",
    methods=["GET", "POST"],
)
def chats():

    user = require_login()

    if not user:

        return jsonify(
            {
                "ok": False,
                "error":
                    "Not authenticated.",
            }
        ), 401

    if request.method == "POST":

        if not check_csrf():
            return jsonify(
                {
                    "ok": False,
                    "error":
                        "Invalid request.",
                }
            ), 403

        conn = get_db()

        try:

            with conn.cursor() as cur:

                cur.execute(
                    """
                    INSERT INTO web_chats
                        (
                            user_id,
                            title
                        )
                    VALUES
                        (
                            %s,
                            %s
                        )
                    RETURNING
                        id,
                        title,
                        created_at,
                        updated_at
                    """,
                    (
                        user["id"],
                        "New chat",
                    ),
                )

                chat = cur.fetchone()

            conn.commit()

        finally:
            conn.close()

        return jsonify(
            {
                "ok": True,
                "chat": {
                    "id": chat["id"],
                    "title": chat["title"],
                    "created_at":
                        chat["created_at"].isoformat(),
                    "updated_at":
                        chat["updated_at"].isoformat(),
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
                (user["id"],),
            )

            rows = cur.fetchall()

    finally:
        conn.close()

    return jsonify(
        {
            "ok": True,
            "chats": [
                {
                    "id": row["id"],
                    "title":
                        row["title"]
                        or "New chat",
                    "created_at":
                        row["created_at"].isoformat(),
                    "updated_at":
                        row["updated_at"].isoformat(),
                }
                for row in rows
            ],
        }
    )


@app.route(
    "/api/chats/<int:chat_id>/messages"
)
def chat_messages(chat_id):

    user = require_login()

    if not user:

        return jsonify(
            {
                "ok": False,
                "error":
                    "Not authenticated.",
            }
        ), 401

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id
                FROM web_chats
                WHERE id = %s
                AND user_id = %s
                """,
                (
                    chat_id,
                    user["id"],
                ),
            )

            chat = cur.fetchone()

            if not chat:

                return jsonify(
                    {
                        "ok": False,
                        "error":
                            "Chat not found.",
                    }
                ), 404

            cur.execute(
                """
                SELECT
                    id,
                    role,
                    content,
                    created_at
                FROM web_messages
                WHERE chat_id = %s
                AND user_id = %s
                ORDER BY created_at ASC, id ASC
                """,
                (
                    chat_id,
                    user["id"],
                ),
            )

            messages = cur.fetchall()

    finally:
        conn.close()

    return jsonify(
        {
            "ok": True,
            "messages": [
                {
                    "id": row["id"],
                    "role": row["role"],
                    "content": row["content"],
                    "created_at":
                        row["created_at"].isoformat(),
                }
                for row in messages
            ],
        }
    )


@app.route(
    "/api/chats/<int:chat_id>",
    methods=["DELETE"],
)
def delete_chat(chat_id):

    user = require_login()

    if not user:

        return jsonify(
            {
                "ok": False,
                "error":
                    "Not authenticated.",
            }
        ), 401

    if not check_csrf():
        return jsonify(
            {
                "ok": False,
                "error":
                    "Invalid request.",
            }
        ), 403

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id
                FROM web_chats
                WHERE id = %s
                AND user_id = %s
                """,
                (
                    chat_id,
                    user["id"],
                ),
            )

            chat = cur.fetchone()

            if not chat:

                return jsonify(
                    {
                        "ok": False,
                        "error":
                            "Chat not found.",
                    }
                ), 404

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
                DELETE FROM web_chats
                WHERE id = %s
                AND user_id = %s
                """,
                (
                    chat_id,
                    user["id"],
                ),
            )

        conn.commit()

    finally:
        conn.close()

    return jsonify(
        {
            "ok": True
        }
    )


# ============================================================
# MEMORY / CONTEXT
# ============================================================

def get_cross_chat_context(
    user_id,
    current_chat_id,
):

    conn = get_db()

    try:

        with conn.cursor() as cur:

            # Get recent messages from OTHER chats.
            #
            # This is silent memory.
            # There is no memory page or memory button.

            cur.execute(
                """
                SELECT
                    m.role,
                    m.content,
                    c.title,
                    m.created_at
                FROM web_messages m
                JOIN web_chats c
                    ON c.id = m.chat_id
                WHERE m.user_id = %s
                AND c.user_id = %s
                AND c.id <> %s
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT 40
                """,
                (
                    user_id,
                    user_id,
                    current_chat_id,
                ),
            )

            rows = cur.fetchall()

    finally:
        conn.close()

    rows.reverse()

    return rows


def get_current_chat_context(
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
                    content
                FROM web_messages
                WHERE user_id = %s
                AND chat_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 30
                """,
                (
                    user_id,
                    chat_id,
                ),
            )

            rows = cur.fetchall()

    finally:
        conn.close()

    rows.reverse()

    return rows


# ============================================================
# AI
# ============================================================

SYSTEM_PROMPT = """
You are AskOra, a smart, friendly and reliable AI assistant.

Your style:
- Be concise and direct.
- Give the answer first.
- Usually answer in 2-5 short paragraphs or bullets.
- Do not unnecessarily repeat the question.
- Do not produce very long explanations unless the user asks for detail.
- Use clear Markdown when useful.
- Be natural and conversational.
- If the user asks for a detailed explanation, then provide more detail.
- If the user tells you personal information such as their name,
  preferences, project details or goals, remember it when it is
  relevant to future conversations.
- Previous conversations may be supplied as context.
- Use previous conversation context naturally.
- Do not mention an internal memory system.
- Do not claim to remember something that is not present in the
  supplied conversation context.
"""


def generate_ai_response(
    user_id,
    chat_id,
    message,
):

    current_context = (
        get_current_chat_context(
            user_id,
            chat_id,
        )
    )

    previous_context = (
        get_cross_chat_context(
            user_id,
            chat_id,
        )
    )

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        }
    ]

    # --------------------------------------------------------
    # PREVIOUS CONVERSATIONS
    # --------------------------------------------------------

    if previous_context:

        memory_lines = [
            "Relevant previous conversations:"
        ]

        for row in previous_context:

            title = (
                row["title"]
                or "Previous chat"
            )

            memory_lines.append(
                f"[{title}] "
                f"{row['role']}: "
                f"{row['content']}"
            )

        messages.append(
            {
                "role": "system",
                "content":
                    "\n".join(memory_lines),
            }
        )

    # --------------------------------------------------------
    # CURRENT CHAT
    # --------------------------------------------------------

    for row in current_context:

        role = row["role"]

        if role not in (
            "user",
            "assistant",
        ):
            continue

        messages.append(
            {
                "role": role,
                "content": row["content"],
            }
        )

    messages.append(
        {
            "role": "user",
            "content": message,
        }
    )

    response = (
        groq_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=messages,
            temperature=0.65,
            max_tokens=750,
        )
    )

    answer = (
        response.choices[0]
        .message
        .content
        .strip()
    )

    return answer


# ============================================================
# CHAT
# ============================================================

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
                "error":
                    "Not authenticated.",
            }
        ), 401

    if not check_csrf():

        return jsonify(
            {
                "ok": False,
                "error":
                    "Invalid request.",
            }
        ), 403

    data = request.get_json(
        silent=True
    ) or {}

    chat_id = data.get(
        "chat_id"
    )

    message = (
        data.get(
            "message"
        )
        or ""
    ).strip()

    try:
        chat_id = int(chat_id)
    except (
        TypeError,
        ValueError,
    ):

        return jsonify(
            {
                "ok": False,
                "error":
                    "Invalid chat.",
            }
        ), 400

    if not message:

        return jsonify(
            {
                "ok": False,
                "error":
                    "Message cannot be empty.",
            }
        ), 400

    if len(message) > MAX_MESSAGE_LENGTH:

        return jsonify(
            {
                "ok": False,
                "error":
                    "Message is too long.",
            }
        ), 400

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id,
                    title
                FROM web_chats
                WHERE id = %s
                AND user_id = %s
                """,
                (
                    chat_id,
                    user["id"],
                ),
            )

            chat = cur.fetchone()

            if not chat:

                return jsonify(
                    {
                        "ok": False,
                        "error":
                            "Chat not found.",
                    }
                ), 404

            # Save user message.
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
                    (
                        %s,
                        %s,
                        %s,
                        %s
                    )
                """,
                (
                    chat_id,
                    user["id"],
                    "user",
                    message,
                ),
            )

            # ------------------------------------------------
            # IMPORTANT:
            # Change "New chat" into the actual question.
            # ------------------------------------------------

            current_title = (
                chat["title"]
                or "New chat"
            )

            if current_title == "New chat":

                clean_title = (
                    " ".join(
                        message
                        .split()
                    )
                )

                if len(clean_title) > 55:
                    clean_title = (
                        clean_title[:55]
                        .rstrip()
                        + "..."
                    )

                cur.execute(
                    """
                    UPDATE web_chats
                    SET title = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    AND user_id = %s
                    """,
                    (
                        clean_title,
                        chat_id,
                        user["id"],
                    ),
                )

                current_title = clean_title

            else:

                cur.execute(
                    """
                    UPDATE web_chats
                    SET updated_at = NOW()
                    WHERE id = %s
                    AND user_id = %s
                    """,
                    (
                        chat_id,
                        user["id"],
                    ),
                )

        conn.commit()

    finally:
        conn.close()

    try:

        answer = generate_ai_response(
            user["id"],
            chat_id,
            message,
        )

    except Exception as error:

        logger.exception(
            "Groq chat error: %s",
            error,
        )

        return jsonify(
            {
                "ok": False,
                "error":
                    "Sorry, something went wrong while generating the response.",
            }
        ), 500

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
                    (
                        %s,
                        %s,
                        %s,
                        %s
                    )
                """,
                (
                    chat_id,
                    user["id"],
                    "assistant",
                    answer,
                ),
            )

            cur.execute(
                """
                UPDATE web_chats
                SET updated_at = NOW()
                WHERE id = %s
                AND user_id = %s
                """,
                (
                    chat_id,
                    user["id"],
                ),
            )

        conn.commit()

    finally:
        conn.close()

    touch_user(
        user["id"]
    )

    log_usage(
        user["id"],
        "chat",
    )

    return jsonify(
        {
            "ok": True,
            "answer": answer,
            "chat_title":
                current_title,
        }
    )


# ============================================================
# VOICE
# ============================================================

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
                "error":
                    "Not authenticated.",
            }
        ), 401

    if not check_csrf():

        return jsonify(
            {
                "ok": False,
                "error":
                    "Invalid request.",
            }
        ), 403

    try:

        chat_id = int(
            request.form.get(
                "chat_id"
            )
        )

    except (
        TypeError,
        ValueError,
    ):

        return jsonify(
            {
                "ok": False,
                "error":
                    "Invalid chat.",
            }
        ), 400

    audio = request.files.get(
        "audio"
    )

    if not audio:

        return jsonify(
            {
                "ok": False,
                "error":
                    "No audio received.",
            }
        ), 400

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id, title
                FROM web_chats
                WHERE id = %s
                AND user_id = %s
                """,
                (
                    chat_id,
                    user["id"],
                ),
            )

            chat = cur.fetchone()

            if not chat:

                return jsonify(
                    {
                        "ok": False,
                        "error":
                            "Chat not found.",
                    }
                ), 404

    finally:
        conn.close()

    try:

        audio_bytes = audio.read()

        if not audio_bytes:

            return jsonify(
                {
                    "ok": False,
                    "error":
                        "The recording was empty.",
                }
            ), 400

        transcription = (
            groq_client.audio.transcriptions.create(
                model=VOICE_MODEL,
                file=(
                    audio.filename
                    or "voice.webm",
                    audio_bytes,
                    audio.mimetype
                    or "audio/webm",
                ),
            )
        )

        transcript = (
            transcription.text
            or ""
        ).strip()

    except Exception as error:

        logger.exception(
            "Voice transcription error: %s",
            error,
        )

        return jsonify(
            {
                "ok": False,
                "error":
                    "Could not understand the voice recording.",
            }
        ), 500

    if not transcript:

        return jsonify(
            {
                "ok": False,
                "error":
                    "No speech was detected.",
            }
        ), 400

    if len(transcript) > MAX_MESSAGE_LENGTH:

        transcript = transcript[
            :MAX_MESSAGE_LENGTH
        ]

    # Save transcript.
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
                    (
                        %s,
                        %s,
                        %s,
                        %s
                    )
                """,
                (
                    chat_id,
                    user["id"],
                    "user",
                    transcript,
                ),
            )

            current_title = (
                chat["title"]
                or "New chat"
            )

            if current_title == "New chat":

                title = (
                    " ".join(
                        transcript.split()
                    )
                )

                if len(title) > 55:
                    title = (
                        title[:55]
                        .rstrip()
                        + "..."
                    )

                current_title = title

                cur.execute(
                    """
                    UPDATE web_chats
                    SET title = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    AND user_id = %s
                    """,
                    (
                        title,
                        chat_id,
                        user["id"],
                    ),
                )

            else:

                cur.execute(
                    """
                    UPDATE web_chats
                    SET updated_at = NOW()
                    WHERE id = %s
                    AND user_id = %s
                    """,
                    (
                        chat_id,
                        user["id"],
                    ),
                )

        conn.commit()

    finally:
        conn.close()

    try:

        answer = generate_ai_response(
            user["id"],
            chat_id,
            transcript,
        )

    except Exception as error:

        logger.exception(
            "Groq voice response error: %s",
            error,
        )

        return jsonify(
            {
                "ok": False,
                "error":
                    "Sorry, something went wrong while generating the response.",
            }
        ), 500

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
                    (
                        %s,
                        %s,
                        %s,
                        %s
                    )
                """,
                (
                    chat_id,
                    user["id"],
                    "assistant",
                    answer,
                ),
            )

            cur.execute(
                """
                UPDATE web_chats
                SET updated_at = NOW()
                WHERE id = %s
                AND user_id = %s
                """,
                (
                    chat_id,
                    user["id"],
                ),
            )

        conn.commit()

    finally:
        conn.close()

    touch_user(
        user["id"]
    )

    log_usage(
        user["id"],
        "voice",
    )

    return jsonify(
        {
            "ok": True,
            "transcript":
                transcript,
            "answer":
                answer,
            "chat_title":
                current_title,
        }
    )


# ============================================================
# RESET CURRENT CHAT
# ============================================================

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
                "error":
                    "Not authenticated.",
            }
        ), 401

    if not check_csrf():

        return jsonify(
            {
                "ok": False,
                "error":
                    "Invalid request.",
            }
        ), 403

    data = request.get_json(
        silent=True
    ) or {}

    try:

        chat_id = int(
            data.get(
                "chat_id"
            )
        )

    except (
        TypeError,
        ValueError,
    ):

        return jsonify(
            {
                "ok": False,
                "error":
                    "Invalid chat.",
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

    finally:
        conn.close()

    return jsonify(
        {
            "ok": True
        }
    )


# ============================================================
# DELETE ACCOUNT
# ============================================================

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
                "error":
                    "Not authenticated.",
            }
        ), 401

    if not check_csrf():

        return jsonify(
            {
                "ok": False,
                "error":
                    "Invalid request.",
            }
        ), 403

    data = request.get_json(
        silent=True
    ) or {}

    password = (
        data.get(
            "password"
        )
        or ""
    )

    if not password:

        return jsonify(
            {
                "ok": False,
                "error":
                    "Password is required.",
            }
        ), 400

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id,
                    password_hash
                FROM web_users
                WHERE id = %s
                """,
                (user["id"],),
            )

            account = cur.fetchone()

            if not account:

                return jsonify(
                    {
                        "ok": False,
                        "error":
                            "Account not found.",
                    }
                ), 404

            if not check_password_hash(
                account["password_hash"],
                password,
            ):

                return jsonify(
                    {
                        "ok": False,
                        "error":
                            "Incorrect password.",
                    }
                ), 403

            # Delete ONLY this web user's data.
            #
            # Existing Telegram tables are untouched.

            cur.execute(
                """
                DELETE FROM web_password_resets
                WHERE user_id = %s
                """,
                (user["id"],),
            )

            cur.execute(
                """
                DELETE FROM web_usage_events
                WHERE user_id = %s
                """,
                (user["id"],),
            )

            cur.execute(
                """
                DELETE FROM web_messages
                WHERE user_id = %s
                """,
                (user["id"],),
            )

            cur.execute(
                """
                DELETE FROM web_chats
                WHERE user_id = %s
                """,
                (user["id"],),
            )

            cur.execute(
                """
                DELETE FROM web_users
                WHERE id = %s
                """,
                (user["id"],),
            )

        conn.commit()

    except Exception as error:

        conn.rollback()

        logger.exception(
            "Account deletion error: %s",
            error,
        )

        return jsonify(
            {
                "ok": False,
                "error":
                    "Could not delete the account.",
            }
        ), 500

    finally:
        conn.close()

    session.clear()

    return jsonify(
        {
            "ok": True,
            "redirect": "/login",
        }
    )


# ============================================================
# ADMIN
# ============================================================

@app.route("/admin")
def admin():

    user = current_user()

    if not user:

        return redirect("/login")

    if not is_admin(user):

        return redirect("/")

    return render_template(
        "admin.html"
    )


@app.route(
    "/api/admin/stats"
)
def admin_stats():

    user = current_user()

    if not user:

        return jsonify(
            {
                "ok": False,
                "error":
                    "Not authenticated.",
            }
        ), 401

    if not is_admin(user):

        return jsonify(
            {
                "ok": False,
                "error":
                    "Unauthorized.",
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

            # Active today
            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_users
                WHERE last_seen >= CURRENT_DATE
                """
            )

            active_today = cur.fetchone()[
                "count"
            ]

            # Chats
            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_chats
                """
            )

            total_chats = cur.fetchone()[
                "count"
            ]

            # Questions / messages
            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_messages
                WHERE role = 'user'
                """
            )

            total_questions = cur.fetchone()[
                "count"
            ]

            # Voice
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

            # Recent users
            cur.execute(
                """
                SELECT
                    username,
                    email,
                    first_seen,
                    last_seen
                FROM web_users
                ORDER BY first_seen DESC
                LIMIT 20
                """
            )

            recent_users = cur.fetchall()

    finally:
        conn.close()

    return jsonify(
        {
            "ok": True,
            "stats": {
                "total_users":
                    total_users,
                "users_today":
                    users_today,
                "active_today":
                    active_today,
                "total_chats":
                    total_chats,
                "total_questions":
                    total_questions,
                "voice_requests":
                    voice_requests,
            },
            "recent_users": [
                {
                    "username":
                        row["username"],
                    "email":
                        row["email"],
                    "first_seen":
                        row["first_seen"].isoformat(),
                    "last_seen":
                        row["last_seen"].isoformat(),
                }
                for row in recent_users
            ],
        }
    )


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    try:

        conn = get_db()

        try:

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1"
                )

        finally:
            conn.close()

        return jsonify(
            {
                "status":
                    "healthy",
                "service":
                    "AskOra",
            }
        )

    except Exception as error:

        logger.exception(
            "Health check failed: %s",
            error,
        )

        return jsonify(
            {
                "status":
                    "unhealthy",
                "service":
                    "AskOra",
            }
        ), 500


# ============================================================
# STARTUP
# ============================================================

init_db()


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
