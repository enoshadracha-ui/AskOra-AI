import os
import re
import secrets
import hashlib
import smtplib
import logging
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps

import psycopg2
from psycopg2.extras import RealDictCursor

from flask import (
    Flask,
    request,
    jsonify,
    session,
    redirect,
    url_for,
    render_template,
)

from werkzeug.security import (
    generate_password_hash,
    check_password_hash,
)

from groq import Groq


# ============================================================
# APP CONFIG
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ["SESSION_SECRET"]

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("COOKIE_SECURE", "true").lower() == "true"
)

app.config["ADMIN_USERNAME"] = os.environ.get(
    "ADMIN_USERNAME",
    "",
)

DATABASE_URL = os.environ["DATABASE_URL"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

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

GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_VOICE_MODEL = "whisper-large-v3-turbo"

groq_client = Groq(
    api_key=GROQ_API_KEY,
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger(__name__)


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

            # ------------------------------------------------
            # MESSAGES
            # ------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS web_messages (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    user_id BIGINT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            # ------------------------------------------------
            # USAGE EVENTS
            # ------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS web_usage_events (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT,
                    event_type TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            # ------------------------------------------------
            # PASSWORD RESETS
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
            # SAFE MIGRATIONS
            # ------------------------------------------------

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
                ALTER TABLE web_chats
                ADD COLUMN IF NOT EXISTS updated_at
                TIMESTAMPTZ NOT NULL DEFAULT NOW()
                """
            )

            # ------------------------------------------------
            # INDEXES
            # ------------------------------------------------

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_web_chats_user_updated
                ON web_chats(user_id, updated_at DESC)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_web_messages_chat_created
                ON web_messages(chat_id, created_at)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_web_messages_user_created
                ON web_messages(user_id, created_at)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_web_usage_user_created
                ON web_usage_events(user_id, created_at)
                """
            )

            # ------------------------------------------------
            # MIGRATE OLD MESSAGES WITHOUT CHAT ID
            # ------------------------------------------------

            cur.execute(
                """
                SELECT DISTINCT user_id
                FROM web_messages
                WHERE user_id IS NOT NULL
                AND chat_id IS NULL
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

                existing = cur.fetchone()

                if existing:
                    chat_id = existing["id"]

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

    finally:
        conn.close()


# ============================================================
# NORMALIZATION
# ============================================================

def normalize_username(value):
    if value is None:
        return ""

    value = str(value).strip().lower()

    if value.startswith("@"):
        value = value[1:]

    return value


def normalize_email(value):
    if value is None:
        return ""

    return str(value).strip().lower()


# ============================================================
# CSRF
# ============================================================

def csrf_token():
    token = session.get("csrf_token")

    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token

    return token


def require_csrf():
    expected = session.get("csrf_token")

    supplied = (
        request.headers.get("X-CSRF-Token")
        or request.form.get("csrf_token")
    )

    if not expected or not supplied:
        return False

    return secrets.compare_digest(
        expected,
        supplied,
    )


def csrf_protected(function):
    @wraps(function)
    def wrapper(*args, **kwargs):

        if not require_csrf():
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "Invalid security token. "
                        "Please refresh the page."
                    ),
                }
            ), 403

        return function(*args, **kwargs)

    return wrapper


# ============================================================
# AUTH HELPERS
# ============================================================

def current_user():
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

            return cur.fetchone()

    finally:
        conn.close()


def login_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):

        if not session.get("user_id"):

            if request.path.startswith("/api/"):
                return jsonify(
                    {
                        "ok": False,
                        "error": "Please log in.",
                    }
                ), 401

            return redirect(
                url_for("login")
            )

        return function(*args, **kwargs)

    return wrapper


def is_admin(user):
    if not user:
        return False

    username = normalize_username(
        user["username"]
    )

    admin_username = normalize_username(
        app.config.get(
            "ADMIN_USERNAME",
            "",
        )
    )

    return (
        username != ""
        and admin_username != ""
        and username == admin_username
    )


# ============================================================
# USER ACTIVITY
# ============================================================

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


def log_event(user_id, event_type):
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


# ============================================================
# HOME
# ============================================================

@app.route("/")
@login_required
def home():
    user = current_user()

    return render_template(
        "index.html",
        user=user,
        csrf_token=csrf_token(),
    )


# ============================================================
# LOGIN
# ============================================================

@app.route(
    "/login",
    methods=["GET", "POST"],
)
def login():

    if session.get("user_id"):
        return redirect(
            url_for("home")
        )

    if request.method == "GET":
        return render_template(
            "login.html"
        )

    username_or_email = (
        request.form.get("username")
        or request.form.get("email")
        or ""
    ).strip()

    password = request.form.get(
        "password",
        "",
    )

    if not username_or_email or not password:
        return render_template(
            "login.html",
            error=(
                "Enter your username/email "
                "and password."
            ),
        )

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

    finally:
        conn.close()

    if not user:
        return render_template(
            "login.html",
            error=(
                "Invalid username/email "
                "or password."
            ),
        )

    if not check_password_hash(
        user["password_hash"],
        password,
    ):
        return render_template(
            "login.html",
            error=(
                "Invalid username/email "
                "or password."
            ),
        )

    session.clear()

    session["user_id"] = user["id"]

    session["csrf_token"] = (
        secrets.token_urlsafe(32)
    )

    touch_user(user["id"])

    return redirect(
        url_for("home")
    )


# ============================================================
# REGISTER
# ============================================================

@app.route(
    "/register",
    methods=["GET", "POST"],
)
def register():

    if session.get("user_id"):
        return redirect(
            url_for("home")
        )

    if request.method == "GET":
        return render_template(
            "register.html"
        )

    username = normalize_username(
        request.form.get(
            "username",
            "",
        )
    )

    email = normalize_email(
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
        return render_template(
            "register.html",
            error="Username is required.",
        )

    if not re.match(
        r"^[a-z0-9_]{3,30}$",
        username,
    ):
        return render_template(
            "register.html",
            error=(
                "Username must be 3-30 characters "
                "and use only letters, numbers "
                "and underscores."
            ),
        )

    if not email or "@" not in email:
        return render_template(
            "register.html",
            error="Enter a valid email address.",
        )

    if len(password) < 8:
        return render_template(
            "register.html",
            error=(
                "Password must be at least "
                "8 characters."
            ),
        )

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
                return render_template(
                    "register.html",
                    error=(
                        "Username or email "
                        "is already in use."
                    ),
                )

            cur.execute(
                """
                INSERT INTO web_users
                (
                    username,
                    email,
                    password_hash
                )
                VALUES (%s, %s, %s)
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

    finally:
        conn.close()

    session.clear()

    session["user_id"] = user_id

    session["csrf_token"] = (
        secrets.token_urlsafe(32)
    )

    return redirect(
        url_for("home")
    )


# ============================================================
# LOGOUT
# ============================================================

@app.route(
    "/logout",
    methods=["GET", "POST"],
)
def logout():

    session.clear()

    response = redirect(
        url_for("login")
    )

    response.delete_cookie(
        app.config.get(
            "SESSION_COOKIE_NAME",
            "session",
        )
    )

    return response


# ============================================================
# API ME
# ============================================================

@app.route("/api/me")
def api_me():

    user = current_user()

    if not user:
        return jsonify(
            {
                "ok": True,
                "authenticated": False,
            }
        )

    touch_user(user["id"])

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


# ============================================================
# FORGOT PASSWORD
# ============================================================

@app.route(
    "/forgot-password",
    methods=["GET", "POST"],
)
def forgot_password():

    if request.method == "GET":
        return render_template(
            "forgot_password.html"
        )

    email = normalize_email(
        request.form.get(
            "email",
            "",
        )
    )

    if not email:
        return render_template(
            "forgot_password.html",
            error="Enter your email address.",
        )

    conn = get_db()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, email
                FROM web_users
                WHERE LOWER(email) = LOWER(%s)
                LIMIT 1
                """,
                (email,),
            )

            user = cur.fetchone()

    finally:
        conn.close()

    if not user:
        return render_template(
            "forgot_password.html",
            message=(
                "If an account exists for that email, "
                "a reset code has been sent."
            ),
        )

    code = f"{secrets.randbelow(1000000):06d}"

    token_hash = hashlib.sha256(
        code.encode()
    ).hexdigest()

    expires_at = (
        datetime.now(timezone.utc)
        + timedelta(minutes=10)
    )

    conn = get_db()

    try:
        with conn.cursor() as cur:

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
                VALUES (%s, %s, %s)
                """,
                (
                    user["id"],
                    token_hash,
                    expires_at,
                ),
            )

        conn.commit()

    finally:
        conn.close()

    try:
        send_reset_email(
            user["email"],
            code,
        )

    except Exception as error:

        logger.exception(
            "Password reset email failed: %s",
            error,
        )

        return render_template(
            "forgot_password.html",
            error=(
                "We couldn't send the reset email "
                "right now. Please try again later."
            ),
        )

    return render_template(
        "forgot_password.html",
        message=(
            "A 6-digit reset code has been "
            "sent to your email."
        ),
    )


# ============================================================
# RESET PASSWORD
# ============================================================

@app.route(
    "/reset-password",
    methods=["GET", "POST"],
)
def reset_password():

    if request.method == "GET":
        return render_template(
            "reset_password.html"
        )

    email = normalize_email(
        request.form.get(
            "email",
            "",
        )
    )

    code = request.form.get(
        "code",
        "",
    ).strip()

    password = request.form.get(
        "password",
        "",
    )

    if (
        not email
        or not re.match(
            r"^\d{6}$",
            code,
        )
        or len(password) < 8
    ):
        return render_template(
            "reset_password.html",
            error=(
                "Enter your email, 6-digit code "
                "and a password of at least "
                "8 characters."
            ),
        )

    code_hash = hashlib.sha256(
        code.encode()
    ).hexdigest()

    conn = get_db()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    r.id,
                    r.user_id
                FROM web_password_resets r
                JOIN web_users u
                    ON u.id = r.user_id
                WHERE LOWER(u.email) = LOWER(%s)
                AND r.token_hash = %s
                AND r.used_at IS NULL
                AND r.expires_at > NOW()
                ORDER BY r.created_at DESC
                LIMIT 1
                """,
                (
                    email,
                    code_hash,
                ),
            )

            reset = cur.fetchone()

            if not reset:
                return render_template(
                    "reset_password.html",
                    error=(
                        "Invalid or expired "
                        "reset code."
                    ),
                )

            password_hash = (
                generate_password_hash(
                    password
                )
            )

            cur.execute(
                """
                UPDATE web_users
                SET password_hash = %s
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

    return redirect(
        url_for("login")
    )


# ============================================================
# EMAIL
# ============================================================

def send_reset_email(recipient, code):

    if not SMTP_USERNAME or not SMTP_PASSWORD:
        raise RuntimeError(
            "SMTP is not configured."
        )

    message = EmailMessage()

    message["Subject"] = (
        "AskOra password reset code"
    )

    message["From"] = SMTP_FROM
    message["To"] = recipient

    message.set_content(
        f"""
Your AskOra password reset code is:

{code}

This code expires in 10 minutes.

If you did not request a password reset,
you can ignore this email.

— AskOra
""".strip()
    )

    with smtplib.SMTP(
        SMTP_HOST,
        SMTP_PORT,
    ) as server:

        server.starttls()

        server.login(
            SMTP_USERNAME,
            SMTP_PASSWORD,
        )

        server.send_message(
            message
        )


# ============================================================
# CHATS
# ============================================================

@app.route(
    "/api/chats",
    methods=["GET", "POST"],
)
@login_required
def chats_api():

    user = current_user()
    user_id = user["id"]

    if request.method == "GET":

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

                rows = cur.fetchall()

        finally:
            conn.close()

        return jsonify(
            {
                "ok": True,
                "chats": [
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "created_at": (
                            row["created_at"].isoformat()
                            if row["created_at"]
                            else None
                        ),
                        "updated_at": (
                            row["updated_at"].isoformat()
                            if row["updated_at"]
                            else None
                        ),
                    }
                    for row in rows
                ],
            }
        )

    # POST = new chat

    if not require_csrf():
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Invalid security token. "
                    "Please refresh the page."
                ),
            }
        ), 403

    body = (
        request.get_json(
            silent=True
        )
        or {}
    )

    title = str(
        body.get(
            "title",
            "New chat",
        )
    ).strip()

    if not title:
        title = "New chat"

    title = title[:120]

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
                VALUES (%s, %s)
                RETURNING
                    id,
                    title,
                    created_at,
                    updated_at
                """,
                (
                    user_id,
                    title,
                ),
            )

            row = cur.fetchone()

        conn.commit()

    finally:
        conn.close()

    return jsonify(
        {
            "ok": True,
            "chat": {
                "id": row["id"],
                "title": row["title"],
                "created_at": (
                    row["created_at"].isoformat()
                ),
                "updated_at": (
                    row["updated_at"].isoformat()
                ),
            },
        }
    )


# ============================================================
# CHAT OWNERSHIP
# ============================================================

def user_owns_chat(user_id, chat_id):

    conn = get_db()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id
                FROM web_chats
                WHERE id = %s
                AND user_id = %s
                LIMIT 1
                """,
                (
                    chat_id,
                    user_id,
                ),
            )

            row = cur.fetchone()

            return bool(row)

    finally:
        conn.close()


# ============================================================
# CHAT MESSAGES
# ============================================================

@app.route(
    "/api/chats/<int:chat_id>/messages"
)
@login_required
def chat_messages(chat_id):

    user = current_user()

    if not user_owns_chat(
        user["id"],
        chat_id,
    ):
        return jsonify(
            {
                "ok": False,
                "error": "Chat not found.",
            }
        ), 404

    conn = get_db()

    try:
        with conn.cursor() as cur:

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
                ORDER BY created_at ASC
                """,
                (
                    chat_id,
                    user["id"],
                ),
            )

            rows = cur.fetchall()

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
                    "created_at": (
                        row["created_at"].isoformat()
                        if row["created_at"]
                        else None
                    ),
                }
                for row in rows
            ],
        }
    )


# ============================================================
# DELETE CHAT
# ============================================================

@app.route(
    "/api/chats/<int:chat_id>",
    methods=["DELETE"],
)
@login_required
@csrf_protected
def delete_chat(chat_id):

    user = current_user()
    user_id = user["id"]

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
                    user_id,
                ),
            )

            chat = cur.fetchone()

            if not chat:
                return jsonify(
                    {
                        "ok": False,
                        "error": "Chat not found.",
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
                    user_id,
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
                    user_id,
                ),
            )

        conn.commit()

    finally:
        conn.close()

    return jsonify(
        {
            "ok": True,
        }
    )


# ============================================================
# CHAT TITLE
# ============================================================

def make_chat_title(message):

    clean = re.sub(
        r"\s+",
        " ",
        message.strip(),
    )

    if not clean:
        return "New chat"

    return clean[:80]


# ============================================================
# COMPACT AI ANSWER
# ============================================================

def compact_answer(answer, detailed=False):

    if not answer:
        return (
            "Sorry, I couldn't generate "
            "an answer right now."
        )

    answer = answer.strip()

    max_chars = (
        5000
        if detailed
        else 3000
    )

    if len(answer) <= max_chars:
        return answer

    shortened = answer[:max_chars]

    last_break = max(
        shortened.rfind("\n"),
        shortened.rfind(". "),
        shortened.rfind("! "),
        shortened.rfind("? "),
    )

    if last_break > max_chars * 0.65:
        shortened = shortened[
            :last_break + 1
        ]

    return (
        shortened.rstrip()
        + "\n\n"
        + "If you want, I can explain this "
        + "in more detail."
    )


# ============================================================
# AI RESPONSE
# ============================================================

def generate_ai_answer(
    user_message,
    history,
    detailed=False,
):

    system_prompt = """
You are AskOra, a helpful AI assistant.

Your job is to answer clearly, naturally and accurately.

Keep normal answers concise and useful.
Do not unnecessarily repeat the user's question.

Use Markdown when it genuinely improves readability.

If the user asks for a simple explanation,
explain it simply.

If the user asks for detailed information,
you may provide more detail.

Do not claim to have performed actions
you cannot actually perform.

Be friendly but do not use excessive filler.
""".strip()

    messages = [
        {
            "role": "system",
            "content": system_prompt,
        }
    ]

    for item in history[-12:]:

        role = item.get("role")

        content = item.get(
            "content",
            "",
        )

        if role in (
            "user",
            "assistant",
        ):
            messages.append(
                {
                    "role": role,
                    "content": content,
                }
            )

    messages.append(
        {
            "role": "user",
            "content": user_message,
        }
    )

    max_tokens = (
        700
        if detailed
        else 350
    )

    response = (
        groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=max_tokens,
        )
    )

    answer = (
        response.choices[0]
        .message
        .content
    )

    return compact_answer(
        answer,
        detailed=detailed,
    )


# ============================================================
# CHAT API
# ============================================================

@app.route(
    "/api/chat",
    methods=["POST"],
)
@login_required
@csrf_protected
def api_chat():

    user = current_user()
    user_id = user["id"]

    body = (
        request.get_json(
            silent=True
        )
        or {}
    )

    message = str(
        body.get(
            "message",
            "",
        )
    ).strip()

    chat_id = body.get("chat_id")

    if not message:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Please enter a message."
                ),
            }
        ), 400

    if len(message) > 12000:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Your message is too long."
                ),
            }
        ), 400

    try:
        chat_id = int(chat_id)

    except (
        TypeError,
        ValueError,
    ):
        chat_id = None

    # --------------------------------------------------------
    # CREATE OR VERIFY CHAT
    # --------------------------------------------------------

    conn = get_db()

    try:
        with conn.cursor() as cur:

            if chat_id:

                cur.execute(
                    """
                    SELECT id, title
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

                if not chat:
                    return jsonify(
                        {
                            "ok": False,
                            "error": (
                                "Chat not found."
                            ),
                        }
                    ), 404

            else:

                title = make_chat_title(
                    message
                )

                cur.execute(
                    """
                    INSERT INTO web_chats
                    (
                        user_id,
                        title
                    )
                    VALUES (%s, %s)
                    RETURNING
                        id,
                        title
                    """,
                    (
                        user_id,
                        title,
                    ),
                )

                chat = cur.fetchone()
                chat_id = chat["id"]

            # ------------------------------------------------
            # GET RECENT HISTORY
            # ------------------------------------------------

            cur.execute(
                """
                SELECT
                    role,
                    content
                FROM web_messages
                WHERE chat_id = %s
                AND user_id = %s
                ORDER BY created_at DESC
                LIMIT 12
                """,
                (
                    chat_id,
                    user_id,
                ),
            )

            history_rows = cur.fetchall()

            history_rows.reverse()

        conn.commit()

    finally:
        conn.close()

    history = [
        {
            "role": row["role"],
            "content": row["content"],
        }
        for row in history_rows
    ]

    detailed = bool(
        re.search(
            r"\b("
            r"detailed|"
            r"in detail|"
            r"explain fully|"
            r"step by step|"
            r"deeply|"
            r"comprehensive"
            r")\b",
            message,
            re.IGNORECASE,
        )
    )

    # --------------------------------------------------------
    # GENERATE AI
    # --------------------------------------------------------

    try:

        answer = generate_ai_answer(
            message,
            history,
            detailed=detailed,
        )

    except Exception as error:

        logger.exception(
            "Groq chat error: %s",
            error,
        )

        return jsonify(
            {
                "ok": False,
                "error": (
                    "Sorry, something went wrong "
                    "while generating the response."
                ),
            }
        ), 500

    # --------------------------------------------------------
    # SAVE MESSAGES
    # --------------------------------------------------------

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
                (%s, %s, 'user', %s)
                """,
                (
                    chat_id,
                    user_id,
                    message,
                ),
            )

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
                (%s, %s, 'assistant', %s)
                """,
                (
                    chat_id,
                    user_id,
                    answer,
                ),
            )

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_messages
                WHERE chat_id = %s
                AND user_id = %s
                AND role = 'user'
                """,
                (
                    chat_id,
                    user_id,
                ),
            )

            count = cur.fetchone()["count"]

            if count == 1:

                title = make_chat_title(
                    message
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
                        title,
                        chat_id,
                        user_id,
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
                        user_id,
                    ),
                )

        conn.commit()

    finally:
        conn.close()

    log_event(
        user_id,
        "chat",
    )

    final_title = (
        make_chat_title(message)
        if count == 1
        else chat["title"]
    )

    return jsonify(
        {
            "ok": True,
            "chat_id": chat_id,
            "title": final_title,
            "answer": answer,
        }
    )


# ============================================================
# VOICE TRANSCRIPTION
# ============================================================

@app.route(
    "/api/voice",
    methods=["POST"],
)
@login_required
@csrf_protected
def api_voice():

    user = current_user()
    user_id = user["id"]

    audio = request.files.get(
        "audio"
    )

    if not audio:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "No audio recording "
                    "was received."
                ),
            }
        ), 400

    try:

        audio_bytes = audio.read()

        if not audio_bytes:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "The recording was empty."
                    ),
                }
            ), 400

        if len(audio_bytes) > 15 * 1024 * 1024:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "The recording is too large."
                    ),
                }
            ), 400

        transcription = (
            groq_client.audio.transcriptions.create(
                file=(
                    audio.filename
                    or "recording.webm",
                    audio_bytes,
                ),
                model=GROQ_VOICE_MODEL,
                response_format="json",
                temperature=0,
            )
        )

        text = getattr(
            transcription,
            "text",
            "",
        )

        text = str(
            text or ""
        ).strip()

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

        log_event(
            user_id,
            "voice",
        )

        return jsonify(
            {
                "ok": True,
                "text": text,
            }
        )

    except Exception as error:

        logger.exception(
            "Voice transcription error: %s",
            error,
        )

        return jsonify(
            {
                "ok": False,
                "error": (
                    "Voice transcription failed. "
                    "Please try again."
                ),
            }
        ), 500


# ============================================================
# RESET CURRENT CHAT CONTEXT
# ============================================================

@app.route(
    "/api/reset",
    methods=["POST"],
)
@login_required
@csrf_protected
def api_reset():

    return jsonify(
        {
            "ok": True,
        }
    )


# ============================================================
# DELETE ACCOUNT
# ============================================================

@app.route(
    "/api/account/delete",
    methods=["POST"],
)
@login_required
@csrf_protected
def delete_account():

    user = current_user()
    user_id = user["id"]

    conn = get_db()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                DELETE FROM web_messages
                WHERE user_id = %s
                """,
                (user_id,),
            )

            cur.execute(
                """
                DELETE FROM web_chats
                WHERE user_id = %s
                """,
                (user_id,),
            )

            cur.execute(
                """
                DELETE FROM web_usage_events
                WHERE user_id = %s
                """,
                (user_id,),
            )

            cur.execute(
                """
                DELETE FROM web_password_resets
                WHERE user_id = %s
                """,
                (user_id,),
            )

            cur.execute(
                """
                DELETE FROM web_users
                WHERE id = %s
                """,
                (user_id,),
            )

        conn.commit()

    finally:
        conn.close()

    session.clear()

    return jsonify(
        {
            "ok": True,
        }
    )


# ============================================================
# ADMIN CHECK
# ============================================================

def admin_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        user = current_user()

        if not user:
            return redirect(
                url_for("login")
            )

        if not is_admin(user):
            return (
                "Forbidden",
                403,
            )

        return function(
            *args,
            **kwargs
        )

    return wrapper


# ============================================================
# ADMIN PAGE
# ============================================================

@app.route("/admin")
@admin_required
def admin():

    return render_template(
        "admin.html"
    )


# ============================================================
# ADMIN STATS
# ============================================================

@app.route(
    "/api/admin/stats"
)
@admin_required
def admin_stats():

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

            total_users = (
                cur.fetchone()["count"]
            )

            # Active users today

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_users
                WHERE last_seen >= CURRENT_DATE
                """
            )

            active_users_today = (
                cur.fetchone()["count"]
            )

            # New users today

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_users
                WHERE first_seen >= CURRENT_DATE
                """
            )

            new_users_today = (
                cur.fetchone()["count"]
            )

            # Total chats

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_chats
                """
            )

            total_chats = (
                cur.fetchone()["count"]
            )

            # Total questions

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_messages
                WHERE role = 'user'
                """
            )

            total_questions = (
                cur.fetchone()["count"]
            )

            # Voice requests

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_usage_events
                WHERE event_type = 'voice'
                """
            )

            voice_requests = (
                cur.fetchone()["count"]
            )

    finally:
        conn.close()

    return jsonify(
        {
            "ok": True,
            "stats": {
                "total_users": total_users,
                "active_users_today": (
                    active_users_today
                ),
                "total_chats": total_chats,
                "total_questions": (
                    total_questions
                ),
                "voice_requests": (
                    voice_requests
                ),
                "new_users_today": (
                    new_users_today
                ),
            },
        }
    )


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify(
        {
            "status": "ok",
            "service": "AskOra",
        }
    )


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(404)
def not_found(error):

    if request.path.startswith("/api/"):
        return jsonify(
            {
                "ok": False,
                "error": "Not found.",
            }
        ), 404

    return (
        "Page not found.",
        404,
    )


@app.errorhandler(405)
def method_not_allowed(error):

    if request.path.startswith("/api/"):
        return jsonify(
            {
                "ok": False,
                "error": "Method not allowed.",
            }
        ), 405

    return (
        "Method not allowed.",
        405,
    )


@app.errorhandler(500)
def internal_error(error):

    logger.exception(
        "Internal server error: %s",
        error,
    )

    if request.path.startswith("/api/"):
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Internal server error."
                ),
            }
        ), 500

    return (
        "Internal server error.",
        500,
    )


# ============================================================
# STARTUP
# ============================================================

try:

    init_db()

    logger.info(
        "AskOra database initialized."
    )

except Exception as error:

    logger.exception(
        "Database initialization failed: %s",
        error,
    )


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

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
