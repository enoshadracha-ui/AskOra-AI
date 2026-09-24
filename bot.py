import os
import re
import secrets
import hashlib
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps

import psycopg2
from psycopg2.extras import RealDictCursor

from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    redirect,
    url_for,
    session,
)

from flask import Response

from groq import Groq

from werkzeug.security import (
    generate_password_hash,
    check_password_hash,
)


# ============================================================
# CONFIG
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ["SESSION_SECRET"]

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get(
        "COOKIE_SECURE",
        "true"
    ).lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

ADMIN_USERNAME = os.environ.get(
    "ADMIN_USERNAME",
    ""
).strip()

SMTP_HOST = os.environ.get(
    "SMTP_HOST",
    "smtp.gmail.com"
)

SMTP_PORT = int(
    os.environ.get(
        "SMTP_PORT",
        "587"
    )
)

SMTP_USERNAME = os.environ.get(
    "SMTP_USERNAME",
    ""
).strip()

SMTP_PASSWORD = os.environ.get(
    "SMTP_PASSWORD",
    ""
).strip()

SMTP_FROM = os.environ.get(
    "SMTP_FROM",
    SMTP_USERNAME
).strip()


# Groq models
TEXT_MODEL = "openai/gpt-oss-120b"
VOICE_MODEL = "whisper-large-v3-turbo"

# Keep normal answers reasonably short.
MAX_AI_TOKENS = 900


groq_client = Groq(
    api_key=GROQ_API_KEY
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger("askora")


# ============================================================
# DATABASE
# ============================================================

def get_db():
    return psycopg2.connect(
        DATABASE_URL
    )


def init_db():

    conn = get_db()

    try:

        with conn.cursor() as cur:

            # ------------------------------------------------
            # USERS
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS web_users (
                    id BIGSERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    email TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_web_users_email
                ON web_users(email)
            """)

            # ------------------------------------------------
            # CHATS
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS web_chats (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    title TEXT NOT NULL DEFAULT 'New chat',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_web_chats_user
                ON web_chats(user_id)
            """)

            # ------------------------------------------------
            # MESSAGES
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS web_messages (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    user_id BIGINT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                ALTER TABLE web_messages
                ADD COLUMN IF NOT EXISTS chat_id BIGINT
            """)

            cur.execute("""
                ALTER TABLE web_messages
                ADD COLUMN IF NOT EXISTS user_id BIGINT
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_web_messages_chat
                ON web_messages(chat_id)
            """)

            # ------------------------------------------------
            # USAGE EVENTS
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS web_usage_events (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT,
                    event_type TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_web_usage_events_type
                ON web_usage_events(event_type)
            """)

            # ------------------------------------------------
            # PASSWORD RESET
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS web_password_resets (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    token_hash TEXT NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    used_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_web_password_resets_token
                ON web_password_resets(token_hash)
            """)

            # ------------------------------------------------
            # SAFE OLD MESSAGE MIGRATION
            # ------------------------------------------------

            cur.execute("""
                SELECT DISTINCT user_id
                FROM web_messages
                WHERE user_id IS NOT NULL
                  AND chat_id IS NULL
            """)

            old_users = cur.fetchall()

            for row in old_users:

                old_user_id = row[0]

                cur.execute("""
                    SELECT id
                    FROM web_chats
                    WHERE user_id = %s
                    ORDER BY created_at ASC
                    LIMIT 1
                """, (
                    old_user_id,
                ))

                existing_chat = cur.fetchone()

                if existing_chat:

                    chat_id = existing_chat[0]

                else:

                    cur.execute("""
                        INSERT INTO web_chats
                            (user_id, title)
                        VALUES
                            (%s, %s)
                        RETURNING id
                    """, (
                        old_user_id,
                        "Previous chat",
                    ))

                    chat_id = cur.fetchone()[0]

                cur.execute("""
                    UPDATE web_messages
                    SET chat_id = %s
                    WHERE user_id = %s
                      AND chat_id IS NULL
                """, (
                    chat_id,
                    old_user_id,
                ))

        conn.commit()

    finally:
        conn.close()


# ============================================================
# HELPERS
# ============================================================

def utc_now():
    return datetime.now(
        timezone.utc
    )


def get_user_by_id(user_id):

    conn = get_db()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                SELECT *
                FROM web_users
                WHERE id = %s
            """, (
                user_id,
            ))

            return cur.fetchone()

    finally:
        conn.close()


def get_user_by_login(value):

    conn = get_db()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                SELECT *
                FROM web_users
                WHERE LOWER(username) = LOWER(%s)
                   OR LOWER(email) = LOWER(%s)
                LIMIT 1
            """, (
                value,
                value,
            ))

            return cur.fetchone()

    finally:
        conn.close()


def touch_user(user_id):

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute("""
                UPDATE web_users
                SET last_seen = NOW()
                WHERE id = %s
            """, (
                user_id,
            ))

        conn.commit()

    finally:
        conn.close()


def log_event(
    user_id,
    event_type
):

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO web_usage_events
                    (user_id, event_type)
                VALUES
                    (%s, %s)
            """, (
                user_id,
                event_type,
            ))

        conn.commit()

    finally:
        conn.close()


def current_user():

    user_id = session.get(
        "user_id"
    )

    if not user_id:
        return None

    return get_user_by_id(
        user_id
    )


def is_admin_user(user):

    if not user:
        return False

    if not ADMIN_USERNAME:
        return False

    return (
        user["username"].lower()
        == ADMIN_USERNAME.lower()
    )


def login_required(view):

    @wraps(view)
    def wrapped(*args, **kwargs):

        if not session.get("user_id"):

            if request.path.startswith(
                "/api/"
            ):
                return jsonify({
                    "error":
                        "Authentication required."
                }), 401

            return redirect(
                url_for("login")
            )

        return view(
            *args,
            **kwargs
        )

    return wrapped


def admin_required(view):

    @wraps(view)
    def wrapped(*args, **kwargs):

        user = current_user()

        if (
            not user
            or not is_admin_user(user)
        ):

            if request.path.startswith(
                "/api/"
            ):
                return jsonify({
                    "error":
                        "Administrator access required."
                }), 403

            return redirect(
                url_for("index")
            )

        return view(
            *args,
            **kwargs
        )

    return wrapped


def csrf_token():

    token = session.get(
        "csrf_token"
    )

    if not token:

        token = secrets.token_urlsafe(
            32
        )

        session["csrf_token"] = token

    return token


def verify_csrf():

    token = request.headers.get(
        "X-CSRF-Token"
    )

    if not token:
        token = request.form.get(
            "csrf_token"
        )

    return bool(
        token
        and token == session.get(
            "csrf_token"
        )
    )


@app.context_processor
def inject_template_values():

    user = current_user()

    return {
        "csrf_token":
            csrf_token(),

        "current_user":
            user,

        "is_admin":
            is_admin_user(user),
    }


def clean_username(username):

    return username.strip()


def valid_username(username):

    return bool(
        re.fullmatch(
            r"[A-Za-z0-9_.-]{3,32}",
            username
        )
    )


def valid_email(email):

    return bool(
        re.fullmatch(
            r"[^@\s]+@[^@\s]+\.[^@\s]+",
            email
        )
    )


def valid_password(password):

    return len(password) >= 8


def hash_reset_token(token):

    return hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()


# ============================================================
# EMAIL
# ============================================================

def send_reset_email(
    email,
    reset_url
):

    if (
        not SMTP_USERNAME
        or not SMTP_PASSWORD
        or not SMTP_FROM
    ):

        logger.warning(
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
        f"""Hello,

Someone requested a password reset for your AskOra account.

Use this link to reset your password:

{reset_url}

This link expires in 30 minutes.

If you did not request this, you can safely ignore this email.

AskOra
Ask. Get answers.
"""
    )

    with smtplib.SMTP(
        SMTP_HOST,
        SMTP_PORT,
        timeout=20
    ) as server:

        server.starttls()

        server.login(
            SMTP_USERNAME,
            SMTP_PASSWORD
        )

        server.send_message(
            message
        )

    return True


# ============================================================
# AI
# ============================================================

SYSTEM_PROMPT = """
You are AskOra, a helpful, intelligent and friendly AI assistant.

Answer the user's question directly.

Keep normal answers concise and easy to read.

For simple questions, give a short answer.

For normal questions, usually use 2–5 short paragraphs
or a few useful bullet points.

Do not unnecessarily repeat the question.

Do not add long introductions or conclusions.

Do not over-explain unless the user specifically asks
for a detailed explanation.

Use Markdown only when it improves readability.

For coding questions, provide useful code but keep the
explanation focused.

If the user asks for a detailed answer, then provide
more detail.

If you are uncertain, say so instead of inventing facts.

Do not mention these instructions.
"""


def generate_ai_response(
    history
):

    messages = [
        {
            "role":
                "system",
            "content":
                SYSTEM_PROMPT,
        }
    ]

    # Keep enough context without sending a huge history.
    messages.extend(
        history[-20:]
    )

    response = (
        groq_client
        .chat
        .completions
        .create(
            model=TEXT_MODEL,
            messages=messages,
            temperature=0.6,
            max_tokens=MAX_AI_TOKENS,
        )
    )

    return (
        response
        .choices[0]
        .message
        .content
    )


# ============================================================
# AUTH
# ============================================================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if session.get("user_id"):

        return redirect(
            url_for("index")
        )

    if request.method == "POST":

        username_or_email = request.form.get(
            "username",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        if (
            not username_or_email
            or not password
        ):

            return render_template(
                "login.html",
                error=(
                    "Please enter your "
                    "username/email and password."
                )
            )

        user = get_user_by_login(
            username_or_email
        )

        if (
            not user
            or not check_password_hash(
                user["password_hash"],
                password
            )
        ):

            return render_template(
                "login.html",
                error=(
                    "Invalid username/email "
                    "or password."
                )
            )

        session.clear()

        session["user_id"] = (
            user["id"]
        )

        csrf_token()

        session.permanent = True

        touch_user(
            user["id"]
        )

        return redirect(
            url_for("index")
        )

    return render_template(
        "login.html"
    )


@app.route(
    "/register",
    methods=["GET", "POST"]
)
def register():

    if session.get("user_id"):

        return redirect(
            url_for("index")
        )

    if request.method == "POST":

        username = clean_username(
            request.form.get(
                "username",
                ""
            )
        )

        email = request.form.get(
            "email",
            ""
        ).strip().lower()

        password = request.form.get(
            "password",
            ""
        )

        if not valid_username(
            username
        ):

            return render_template(
                "register.html",
                error=(
                    "Username must be 3–32 characters "
                    "and can only contain letters, "
                    "numbers, dots, underscores and hyphens."
                )
            )

        if not valid_email(email):

            return render_template(
                "register.html",
                error=(
                    "Please enter a valid email address."
                )
            )

        if not valid_password(password):

            return render_template(
                "register.html",
                error=(
                    "Password must be at least 8 characters."
                )
            )

        conn = get_db()

        try:

            with conn.cursor(
                cursor_factory=RealDictCursor
            ) as cur:

                cur.execute("""
                    SELECT id
                    FROM web_users
                    WHERE LOWER(username) = LOWER(%s)
                """, (
                    username,
                ))

                if cur.fetchone():

                    return render_template(
                        "register.html",
                        error=(
                            "That username is already taken."
                        )
                    )

                cur.execute("""
                    SELECT id
                    FROM web_users
                    WHERE LOWER(email) = LOWER(%s)
                """, (
                    email,
                ))

                if cur.fetchone():

                    return render_template(
                        "register.html",
                        error=(
                            "That email is already registered."
                        )
                    )

                cur.execute("""
                    INSERT INTO web_users
                        (
                            username,
                            email,
                            password_hash
                        )
                    VALUES
                        (%s, %s, %s)
                    RETURNING id
                """, (
                    username,
                    email,
                    generate_password_hash(
                        password
                    ),
                ))

                user = cur.fetchone()

            conn.commit()

        finally:
            conn.close()

        session.clear()

        session["user_id"] = (
            user["id"]
        )

        csrf_token()

        session.permanent = True

        log_event(
            user["id"],
            "registration"
        )

        return redirect(
            url_for("index")
        )

    return render_template(
        "register.html"
    )


@app.route(
    "/logout",
    methods=["POST"]
)
def logout():

    if not verify_csrf():

        return jsonify({
            "error":
                "Invalid CSRF token."
        }), 403

    session.clear()

    return redirect(
        url_for("login")
    )


# ============================================================
# DELETE ACCOUNT
# ============================================================

@app.route(
    "/api/account/delete",
    methods=["POST"]
)
@login_required
def delete_account():

    if not verify_csrf():

        return jsonify({
            "error":
                "Invalid CSRF token."
        }), 403

    user_id = session["user_id"]

    data = request.get_json(
        silent=True
    ) or {}

    password = str(
        data.get(
            "password",
            ""
        )
    )

    if not password:

        return jsonify({
            "error":
                "Password is required."
        }), 400

    user = get_user_by_id(
        user_id
    )

    if not user:

        session.clear()

        return jsonify({
            "success": True
        })

    if not check_password_hash(
        user["password_hash"],
        password
    ):

        return jsonify({
            "error":
                "Incorrect password."
        }), 401

    conn = get_db()

    try:

        with conn.cursor() as cur:

            # Only AskOra web data is deleted.
            # Existing Telegram tables are untouched.

            cur.execute("""
                DELETE FROM web_password_resets
                WHERE user_id = %s
            """, (
                user_id,
            ))

            cur.execute("""
                DELETE FROM web_messages
                WHERE user_id = %s
            """, (
                user_id,
            ))

            cur.execute("""
                DELETE FROM web_chats
                WHERE user_id = %s
            """, (
                user_id,
            ))

            cur.execute("""
                DELETE FROM web_usage_events
                WHERE user_id = %s
            """, (
                user_id,
            ))

            cur.execute("""
                DELETE FROM web_users
                WHERE id = %s
            """, (
                user_id,
            ))

        conn.commit()

    finally:
        conn.close()

    session.clear()

    return jsonify({
        "success": True
    })


# ============================================================
# PASSWORD RESET
# ============================================================

@app.route(
    "/forgot-password",
    methods=["GET", "POST"]
)
def forgot_password():

    if request.method == "POST":

        email = request.form.get(
            "email",
            ""
        ).strip().lower()

        generic_message = (
            "If an account exists for that email, "
            "a password reset link has been sent."
        )

        if not valid_email(email):

            return render_template(
                "forgot_password.html",
                message=generic_message,
            )

        conn = get_db()

        try:

            with conn.cursor(
                cursor_factory=RealDictCursor
            ) as cur:

                cur.execute("""
                    SELECT *
                    FROM web_users
                    WHERE LOWER(email) = LOWER(%s)
                    LIMIT 1
                """, (
                    email,
                ))

                user = cur.fetchone()

                if user:

                    token = (
                        secrets.token_urlsafe(
                            48
                        )
                    )

                    token_hash = (
                        hash_reset_token(
                            token
                        )
                    )

                    expires_at = (
                        utc_now()
                        + timedelta(
                            minutes=30
                        )
                    )

                    cur.execute("""
                        INSERT INTO web_password_resets
                            (
                                user_id,
                                token_hash,
                                expires_at
                            )
                        VALUES
                            (%s, %s, %s)
                    """, (
                        user["id"],
                        token_hash,
                        expires_at,
                    ))

                    reset_url = url_for(
                        "reset_password",
                        token=token,
                        _external=True,
                    )

                    try:

                        send_reset_email(
                            user["email"],
                            reset_url,
                        )

                    except Exception:

                        logger.exception(
                            "Could not send reset email."
                        )

            conn.commit()

        finally:
            conn.close()

        return render_template(
            "forgot_password.html",
            message=generic_message,
        )

    return render_template(
        "forgot_password.html"
    )


@app.route(
    "/reset-password",
    methods=["GET", "POST"]
)
def reset_password():

    token = request.args.get(
        "token",
        ""
    ).strip()

    if not token:

        return render_template(
            "reset_password.html",
            error=(
                "Invalid or missing reset token."
            )
        )

    token_hash = (
        hash_reset_token(token)
    )

    conn = get_db()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                SELECT *
                FROM web_password_resets
                WHERE token_hash = %s
                  AND used_at IS NULL
                  AND expires_at > NOW()
                ORDER BY created_at DESC
                LIMIT 1
            """, (
                token_hash,
            ))

            reset = cur.fetchone()

            if not reset:

                return render_template(
                    "reset_password.html",
                    error=(
                        "This reset link is invalid "
                        "or has expired."
                    )
                )

            if request.method == "POST":

                password = request.form.get(
                    "password",
                    ""
                )

                confirm_password = request.form.get(
                    "confirm_password",
                    ""
                )

                if not valid_password(
                    password
                ):

                    return render_template(
                        "reset_password.html",
                        error=(
                            "Password must be at least "
                            "8 characters."
                        ),
                        token=token,
                    )

                if password != confirm_password:

                    return render_template(
                        "reset_password.html",
                        error=(
                            "The passwords do not match."
                        ),
                        token=token,
                    )

                cur.execute("""
                    UPDATE web_users
                    SET password_hash = %s
                    WHERE id = %s
                """, (
                    generate_password_hash(
                        password
                    ),
                    reset["user_id"],
                ))

                cur.execute("""
                    UPDATE web_password_resets
                    SET used_at = NOW()
                    WHERE id = %s
                """, (
                    reset["id"],
                ))

                conn.commit()

                return redirect(
                    url_for("login")
                )

    finally:
        conn.close()

    return render_template(
        "reset_password.html",
        token=token,
    )


# ============================================================
# MAIN APP
# ============================================================

@app.route("/")
@login_required
def index():

    return render_template(
        "index.html"
    )


@app.route("/api/me")
@login_required
def api_me():

    user = current_user()

    return jsonify({
        "id":
            user["id"],

        "username":
            user["username"],

        "email":
            user["email"],

        "is_admin":
            is_admin_user(user),
    })


# ============================================================
# CHATS
# ============================================================

@app.route(
    "/api/chats",
    methods=["GET"]
)
@login_required
def get_chats():

    user_id = session["user_id"]

    conn = get_db()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                SELECT
                    c.id,
                    c.title,
                    c.created_at,
                    c.updated_at,
                    COUNT(m.id) AS message_count
                FROM web_chats c
                LEFT JOIN web_messages m
                    ON m.chat_id = c.id
                WHERE c.user_id = %s
                GROUP BY
                    c.id,
                    c.title,
                    c.created_at,
                    c.updated_at
                ORDER BY c.updated_at DESC
            """, (
                user_id,
            ))

            chats = cur.fetchall()

    finally:
        conn.close()

    return jsonify([
        {
            "id":
                chat["id"],

            "title":
                chat["title"],

            "message_count":
                int(
                    chat["message_count"]
                ),

            "created_at":
                chat["created_at"].isoformat(),

            "updated_at":
                chat["updated_at"].isoformat(),
        }
        for chat in chats
    ])


@app.route(
    "/api/chats",
    methods=["POST"]
)
@login_required
def create_chat():

    if not verify_csrf():

        return jsonify({
            "error":
                "Invalid CSRF token."
        }), 403

    user_id = session["user_id"]

    data = request.get_json(
        silent=True
    ) or {}

    title = str(
        data.get(
            "title",
            "New chat"
        )
    ).strip()[:100]

    if not title:

        title = "New chat"

    conn = get_db()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                INSERT INTO web_chats
                    (
                        user_id,
                        title
                    )
                VALUES
                    (%s, %s)
                RETURNING *
            """, (
                user_id,
                title,
            ))

            chat = cur.fetchone()

        conn.commit()

    finally:
        conn.close()

    return jsonify({
        "id":
            chat["id"],

        "title":
            chat["title"],
    }), 201


@app.route(
    "/api/chats/<int:chat_id>/messages",
    methods=["GET"]
)
@login_required
def get_messages(chat_id):

    user_id = session["user_id"]

    conn = get_db()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                SELECT id, title
                FROM web_chats
                WHERE id = %s
                  AND user_id = %s
            """, (
                chat_id,
                user_id,
            ))

            chat = cur.fetchone()

            if not chat:

                return jsonify({
                    "error":
                        "Chat not found."
                }), 404

            cur.execute("""
                SELECT
                    id,
                    role,
                    content,
                    created_at
                FROM web_messages
                WHERE chat_id = %s
                  AND user_id = %s
                ORDER BY created_at ASC, id ASC
            """, (
                chat_id,
                user_id,
            ))

            messages = cur.fetchall()

    finally:
        conn.close()

    return jsonify({
        "chat": {
            "id":
                chat["id"],

            "title":
                chat["title"],
        },

        "messages": [
            {
                "id":
                    message["id"],

                "role":
                    message["role"],

                "content":
                    message["content"],

                "created_at":
                    message["created_at"].isoformat(),
            }

            for message in messages
        ],
    })


@app.route(
    "/api/chats/<int:chat_id>",
    methods=["DELETE"]
)
@login_required
def delete_chat(chat_id):

    if not verify_csrf():

        return jsonify({
            "error":
                "Invalid CSRF token."
        }), 403

    user_id = session["user_id"]

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT id
                FROM web_chats
                WHERE id = %s
                  AND user_id = %s
            """, (
                chat_id,
                user_id,
            ))

            if not cur.fetchone():

                return jsonify({
                    "error":
                        "Chat not found."
                }), 404

            cur.execute("""
                DELETE FROM web_messages
                WHERE chat_id = %s
                  AND user_id = %s
            """, (
                chat_id,
                user_id,
            ))

            cur.execute("""
                DELETE FROM web_chats
                WHERE id = %s
                  AND user_id = %s
            """, (
                chat_id,
                user_id,
            ))

        conn.commit()

    finally:
        conn.close()

    return jsonify({
        "success": True
    })


# ============================================================
# CHAT
# ============================================================

@app.route(
    "/api/chat",
    methods=["POST"]
)
@login_required
def chat_api():

    if not verify_csrf():

        return jsonify({
            "error":
                "Invalid CSRF token."
        }), 403

    user_id = session["user_id"]

    data = request.get_json(
        silent=True
    ) or {}

    chat_id = data.get(
        "chat_id"
    )

    message = str(
        data.get(
            "message",
            ""
        )
    ).strip()

    if not message:

        return jsonify({
            "error":
                "Message cannot be empty."
        }), 400

    if len(message) > 12000:

        return jsonify({
            "error":
                "Message is too long."
        }), 400

    conn = get_db()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            if chat_id:

                cur.execute("""
                    SELECT *
                    FROM web_chats
                    WHERE id = %s
                      AND user_id = %s
                """, (
                    chat_id,
                    user_id,
                ))

                chat = cur.fetchone()

                if not chat:

                    return jsonify({
                        "error":
                            "Chat not found."
                    }), 404

            else:

                cur.execute("""
                    INSERT INTO web_chats
                        (
                            user_id,
                            title
                        )
                    VALUES
                        (%s, %s)
                    RETURNING *
                """, (
                    user_id,
                    message[:60],
                ))

                chat = cur.fetchone()

                chat_id = chat["id"]

            cur.execute("""
                INSERT INTO web_messages
                    (
                        chat_id,
                        user_id,
                        role,
                        content
                    )
                VALUES
                    (%s, %s, %s, %s)
            """, (
                chat_id,
                user_id,
                "user",
                message,
            ))

            cur.execute("""
                SELECT
                    role,
                    content
                FROM web_messages
                WHERE chat_id = %s
                  AND user_id = %s
                ORDER BY created_at ASC, id ASC
                LIMIT 20
            """, (
                chat_id,
                user_id,
            ))

            rows = cur.fetchall()

        conn.commit()

    finally:
        conn.close()

    history = [
        {
            "role":
                row["role"],

            "content":
                row["content"],
        }

        for row in rows
    ]

    try:

        answer = generate_ai_response(
            history
        )

    except Exception as error:

        logger.exception(
            "Groq chat error: %s",
            error
        )

        return jsonify({
            "error":
                "AskOra could not generate "
                "a response right now."
        }), 500

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO web_messages
                    (
                        chat_id,
                        user_id,
                        role,
                        content
                    )
                VALUES
                    (%s, %s, %s, %s)
            """, (
                chat_id,
                user_id,
                "assistant",
                answer,
            ))

            cur.execute("""
                UPDATE web_chats
                SET updated_at = NOW()
                WHERE id = %s
                  AND user_id = %s
            """, (
                chat_id,
                user_id,
            ))

        conn.commit()

    finally:
        conn.close()

    touch_user(user_id)

    log_event(
        user_id,
        "chat"
    )

    return jsonify({
        "chat_id":
            chat_id,

        "answer":
            answer,
    })


# ============================================================
# VOICE
# ============================================================

@app.route(
    "/api/voice",
    methods=["POST"]
)
@login_required
def voice_api():

    if not verify_csrf():

        return jsonify({
            "error":
                "Invalid CSRF token."
        }), 403

    user_id = session["user_id"]

    audio = request.files.get(
        "audio"
    )

    if not audio:

        return jsonify({
            "error":
                "No audio was provided."
        }), 400

    audio_bytes = audio.read()

    if not audio_bytes:

        return jsonify({
            "error":
                "Audio file is empty."
        }), 400

    if len(audio_bytes) > 15 * 1024 * 1024:

        return jsonify({
            "error":
                "Audio file is too large."
        }), 400

    try:

        transcription = (
            groq_client
            .audio
            .transcriptions
            .create(
                file=(
                    audio.filename
                    or "recording.webm",
                    audio_bytes,
                ),
                model=VOICE_MODEL,
                response_format="json",
            )
        )

        text = transcription.text.strip()

    except Exception:

        logger.exception(
            "Voice transcription error."
        )

        return jsonify({
            "error":
                "Voice transcription failed."
        }), 500

    if not text:

        return jsonify({
            "error":
                "No speech was detected."
        }), 400

    chat_id = request.form.get(
        "chat_id"
    )

    conn = get_db()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            if chat_id:

                try:
                    chat_id = int(
                        chat_id
                    )
                except ValueError:
                    chat_id = None

            if chat_id:

                cur.execute("""
                    SELECT *
                    FROM web_chats
                    WHERE id = %s
                      AND user_id = %s
                """, (
                    chat_id,
                    user_id,
                ))

                chat = cur.fetchone()

                if not chat:

                    return jsonify({
                        "error":
                            "Chat not found."
                    }), 404

            else:

                cur.execute("""
                    INSERT INTO web_chats
                        (
                            user_id,
                            title
                        )
                    VALUES
                        (%s, %s)
                    RETURNING *
                """, (
                    user_id,
                    text[:60],
                ))

                chat = cur.fetchone()

                chat_id = chat["id"]

            cur.execute("""
                INSERT INTO web_messages
                    (
                        chat_id,
                        user_id,
                        role,
                        content
                    )
                VALUES
                    (%s, %s, %s, %s)
            """, (
                chat_id,
                user_id,
                "user",
                text,
            ))

            cur.execute("""
                SELECT
                    role,
                    content
                FROM web_messages
                WHERE chat_id = %s
                  AND user_id = %s
                ORDER BY created_at ASC, id ASC
                LIMIT 20
            """, (
                chat_id,
                user_id,
            ))

            rows = cur.fetchall()

        conn.commit()

    finally:
        conn.close()

    history = [
        {
            "role":
                row["role"],

            "content":
                row["content"],
        }

        for row in rows
    ]

    try:

        answer = generate_ai_response(
            history
        )

    except Exception:

        logger.exception(
            "Voice AI response error."
        )

        return jsonify({
            "error":
                "AskOra could not generate "
                "a response."
        }), 500

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO web_messages
                    (
                        chat_id,
                        user_id,
                        role,
                        content
                    )
                VALUES
                    (%s, %s, %s, %s)
            """, (
                chat_id,
                user_id,
                "assistant",
                answer,
            ))

            cur.execute("""
                UPDATE web_chats
                SET updated_at = NOW()
                WHERE id = %s
                  AND user_id = %s
            """, (
                chat_id,
                user_id,
            ))

        conn.commit()

    finally:
        conn.close()

    touch_user(
        user_id
    )

    log_event(
        user_id,
        "voice"
    )

    return jsonify({
        "chat_id":
            chat_id,

        "transcription":
            text,

        "answer":
            answer,
    })


# ============================================================
# ADMIN
# ============================================================

@app.route("/admin")
@admin_required
def admin():

    return render_template(
        "admin.html"
    )


@app.route("/api/admin/stats")
@admin_required
def admin_stats():

    conn = get_db()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM web_users
            """)

            total_users = (
                cur.fetchone()["count"]
            )

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM web_users
                WHERE first_seen >= CURRENT_DATE
            """)

            users_today = (
                cur.fetchone()["count"]
            )

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM web_users
                WHERE last_seen >= CURRENT_DATE
            """)

            active_today = (
                cur.fetchone()["count"]
            )

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM web_chats
            """)

            total_chats = (
                cur.fetchone()["count"]
            )

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM web_messages
                WHERE role = 'user'
            """)

            total_questions = (
                cur.fetchone()["count"]
            )

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM web_usage_events
                WHERE event_type = 'voice'
            """)

            voice_requests = (
                cur.fetchone()["count"]
            )

            cur.execute("""
                SELECT
                    event_type,
                    COUNT(*) AS count
                FROM web_usage_events
                GROUP BY event_type
                ORDER BY count DESC
            """)

            events = cur.fetchall()

            cur.execute("""
                SELECT
                    username,
                    email,
                    first_seen,
                    last_seen
                FROM web_users
                ORDER BY first_seen DESC
                LIMIT 20
            """)

            recent_users = cur.fetchall()

    finally:
        conn.close()

    return jsonify({

        "total_users":
            int(total_users),

        "users_today":
            int(users_today),

        "active_today":
            int(active_today),

        "total_chats":
            int(total_chats),

        "total_questions":
            int(total_questions),

        "voice_requests":
            int(voice_requests),

        "events": [
            {
                "event_type":
                    event["event_type"],

                "count":
                    int(event["count"]),
            }

            for event in events
        ],

        "recent_users": [
            {
                "username":
                    user["username"],

                "email":
                    user["email"],

                "first_seen":
                    user["first_seen"].isoformat(),

                "last_seen":
                    user["last_seen"].isoformat(),
            }

            for user in recent_users
        ],
    })


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

                cur.fetchone()

        finally:
            conn.close()

        return jsonify({
            "status":
                "ok"
        })

    except Exception:

        logger.exception(
            "Health check failed."
        )

        return jsonify({
            "status":
                "database_error"
        }), 500


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(404)
def not_found(error):

    if request.path.startswith(
        "/api/"
    ):

        return jsonify({
            "error":
                "Not found."
        }), 404

    return Response(
        "Page not found.",
        status=404,
        mimetype="text/plain",
    )


@app.errorhandler(500)
def server_error(error):

    logger.exception(
        "Server error."
    )

    if request.path.startswith(
        "/api/"
    ):

        return jsonify({
            "error":
                "Internal server error."
        }), 500

    return Response(
        "Something went wrong.",
        status=500,
        mimetype="text/plain",
    )


# ============================================================
# STARTUP
# ============================================================

init_db()

logger.info(
    "AskOra database initialized."
)

logger.info(
    "AskOra application ready."
)


if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
