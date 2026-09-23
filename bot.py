import os
import re
import secrets
import logging
from functools import wraps
from io import BytesIO
from datetime import datetime, timezone

from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    session,
    redirect,
    url_for,
)
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2.extras import RealDictCursor
from groq import Groq


# ============================================================
# SETTINGS
# ============================================================

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

SESSION_SECRET = os.environ.get(
    "SESSION_SECRET",
    secrets.token_hex(32),
)

ADMIN_USERNAME = os.environ.get(
    "ADMIN_USERNAME",
    "admin",
).strip().lower()

TEXT_MODEL = "openai/gpt-oss-120b"
VOICE_MODEL = "whisper-large-v3-turbo"

PORT = int(os.environ.get("PORT", "10000"))

MAX_MESSAGE_LENGTH = 12000
MAX_AUDIO_SIZE = 15 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

app = Flask(__name__)

app.secret_key = SESSION_SECRET

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=MAX_AUDIO_SIZE,
)

groq_client = Groq(api_key=GROQ_API_KEY)


# ============================================================
# DATABASE
# ============================================================

def get_db():
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        sslmode="require",
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
                    email TEXT,
                    password_hash TEXT NOT NULL,
                    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            # Add email safely to older installations
            cur.execute(
                """
                ALTER TABLE web_users
                ADD COLUMN IF NOT EXISTS email TEXT
                """
            )

            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                idx_web_users_email_lower
                ON web_users (LOWER(email))
                WHERE email IS NOT NULL
                """
            )

            # ------------------------------------------------
            # CHATS
            # ------------------------------------------------

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

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_web_chats_user_updated
                ON web_chats(user_id, updated_at DESC)
                """
            )

            # ------------------------------------------------
            # MESSAGES
            # ------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS web_messages (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL
                        REFERENCES web_users(id)
                        ON DELETE CASCADE,
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
                CREATE INDEX IF NOT EXISTS
                idx_web_messages_chat
                ON web_messages(chat_id, created_at)
                """
            )

            # ------------------------------------------------
            # USAGE EVENTS
            # ------------------------------------------------

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

            # ------------------------------------------------
            # SAFELY MIGRATE OLD MESSAGES
            # ------------------------------------------------

            cur.execute(
                """
                SELECT id
                FROM web_users
                """
            )

            users = cur.fetchall()

            for user in users:

                cur.execute(
                    """
                    SELECT id
                    FROM web_chats
                    WHERE user_id = %s
                    LIMIT 1
                    """,
                    (user["id"],),
                )

                existing_chat = cur.fetchone()

                if not existing_chat:

                    cur.execute(
                        """
                        SELECT id
                        FROM web_messages
                        WHERE user_id = %s
                          AND chat_id IS NULL
                        LIMIT 1
                        """,
                        (user["id"],),
                    )

                    old_message = cur.fetchone()

                    if old_message:

                        cur.execute(
                            """
                            INSERT INTO web_chats
                            (user_id, title)
                            VALUES (%s, %s)
                            RETURNING id
                            """,
                            (
                                user["id"],
                                "Previous chat",
                            ),
                        )

                        new_chat = cur.fetchone()

                        cur.execute(
                            """
                            UPDATE web_messages
                            SET chat_id = %s
                            WHERE user_id = %s
                              AND chat_id IS NULL
                            """,
                            (
                                new_chat["id"],
                                user["id"],
                            ),
                        )

            conn.commit()

    except Exception:
        conn.rollback()
        logging.exception("Database initialization failed")
        raise

    finally:
        conn.close()


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
                SELECT id, username, email,
                       first_seen, last_seen
                FROM web_users
                WHERE id = %s
                """,
                (user_id,),
            )

            return cur.fetchone()

    finally:
        conn.close()


def is_admin_user(user):
    if not user:
        return False

    return (
        user["username"].strip().lower()
        == ADMIN_USERNAME
    )


def login_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        if not session.get("user_id"):
            return redirect(url_for("login"))

        return function(*args, **kwargs)

    return wrapper


def admin_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        user = current_user()

        if not user or not is_admin_user(user):
            return jsonify({
                "error": "Unauthorized"
            }), 403

        return function(*args, **kwargs)

    return wrapper


# ============================================================
# CSRF
# ============================================================

def get_csrf_token():

    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)

    return session["csrf_token"]


def csrf_protect():

    token = request.headers.get(
        "X-CSRF-Token"
    )

    if not token:
        token = request.form.get("csrf_token")

    if not token:
        try:
            token = request.get_json(
                silent=True
            ).get("csrf_token")
        except Exception:
            token = None

    if not token:
        return False

    return secrets.compare_digest(
        token,
        session.get("csrf_token", ""),
    )


# ============================================================
# BASIC VALIDATION
# ============================================================

USERNAME_PATTERN = re.compile(
    r"^[a-z0-9_]{3,32}$"
)


def valid_username(username):
    return bool(
        USERNAME_PATTERN.fullmatch(
            username.lower()
        )
    )


def valid_email(email):

    pattern = re.compile(
        r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )

    return bool(
        pattern.fullmatch(email)
    )


# ============================================================
# AUTH PAGES
# ============================================================

@app.route("/")
def index():

    user = current_user()

    if not user:
        return redirect(url_for("login"))

    return render_template(
        "index.html",
        user=user,
        csrf_token=get_csrf_token(),
        is_admin=is_admin_user(user),
    )


@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "GET":

        if session.get("user_id"):
            return redirect(url_for("index"))

        return render_template(
            "login.html",
            csrf_token=get_csrf_token(),
        )

    if not csrf_protect():

        return jsonify({
            "error": "Invalid security token."
        }), 403

    identifier = (
        request.form.get("identifier", "")
        .strip()
        .lower()
    )

    password = request.form.get(
        "password",
        "",
    )

    if not identifier or not password:

        return render_template(
            "login.html",
            csrf_token=get_csrf_token(),
            error="Please enter your username/email and password.",
        )

    conn = get_db()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT *
                FROM web_users
                WHERE LOWER(username) = %s
                   OR LOWER(email) = %s
                LIMIT 1
                """,
                (
                    identifier,
                    identifier,
                ),
            )

            user = cur.fetchone()

            if not user:

                return render_template(
                    "login.html",
                    csrf_token=get_csrf_token(),
                    error="Invalid username/email or password.",
                )

            if not check_password_hash(
                user["password_hash"],
                password,
            ):

                return render_template(
                    "login.html",
                    csrf_token=get_csrf_token(),
                    error="Invalid username/email or password.",
                )

            cur.execute(
                """
                UPDATE web_users
                SET last_seen = NOW()
                WHERE id = %s
                """,
                (user["id"],),
            )

            conn.commit()

            session.clear()

            session["user_id"] = user["id"]
            session["csrf_token"] = secrets.token_urlsafe(32)

            return redirect(url_for("index"))

    finally:
        conn.close()


@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "GET":

        if session.get("user_id"):
            return redirect(url_for("index"))

        return render_template(
            "register.html",
            csrf_token=get_csrf_token(),
        )

    if not csrf_protect():

        return jsonify({
            "error": "Invalid security token."
        }), 403

    username = (
        request.form.get("username", "")
        .strip()
        .lower()
    )

    email = (
        request.form.get("email", "")
        .strip()
        .lower()
    )

    password = request.form.get(
        "password",
        "",
    )

    if not valid_username(username):

        return render_template(
            "register.html",
            csrf_token=get_csrf_token(),
            error=(
                "Username must be 3–32 characters "
                "using letters, numbers and underscores."
            ),
        )

    if not valid_email(email):

        return render_template(
            "register.html",
            csrf_token=get_csrf_token(),
            error="Please enter a valid email address.",
        )

    if len(password) < 8:

        return render_template(
            "register.html",
            csrf_token=get_csrf_token(),
            error="Password must be at least 8 characters.",
        )

    conn = get_db()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id
                FROM web_users
                WHERE LOWER(username) = %s
                   OR LOWER(email) = %s
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
                    csrf_token=get_csrf_token(),
                    error="Username or email is already in use.",
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
                    generate_password_hash(password),
                ),
            )

            user = cur.fetchone()

            conn.commit()

            session.clear()

            session["user_id"] = user["id"]
            session["csrf_token"] = secrets.token_urlsafe(32)

            return redirect(url_for("index"))

    finally:
        conn.close()


@app.route("/logout", methods=["POST"])
def logout():

    if not csrf_protect():

        return jsonify({
            "error": "Invalid security token."
        }), 403

    session.clear()

    return redirect(url_for("login"))


# ============================================================
# CURRENT USER
# ============================================================

@app.route("/api/me")
@login_required
def api_me():

    user = current_user()

    return jsonify({
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
            "is_admin": is_admin_user(user),
        }
    })


# ============================================================
# CHAT HELPERS
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
                """,
                (
                    chat_id,
                    user_id,
                ),
            )

            return cur.fetchone() is not None

    finally:
        conn.close()


def create_chat_for_user(
    user_id,
    title="New chat",
):

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
                RETURNING id, user_id, title,
                          created_at, updated_at
                """,
                (
                    user_id,
                    title,
                ),
            )

            chat = cur.fetchone()

            conn.commit()

            return chat

    finally:
        conn.close()


# ============================================================
# CHAT LIST
# ============================================================

@app.route("/api/chats")
@login_required
def get_chats():

    user = current_user()

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

            chats = cur.fetchall()

            return jsonify({
                "chats": chats
            })

    finally:
        conn.close()


# ============================================================
# CREATE NEW CHAT
# ============================================================

@app.route("/api/chats", methods=["POST"])
@login_required
def create_chat():

    if not csrf_protect():

        return jsonify({
            "error": "Invalid security token."
        }), 403

    user = current_user()

    chat = create_chat_for_user(
        user["id"],
        "New chat",
    )

    return jsonify({
        "chat": chat
    })


# ============================================================
# CHAT MESSAGES
# ============================================================

@app.route(
    "/api/chats/<int:chat_id>/messages"
)
@login_required
def get_chat_messages(chat_id):

    user = current_user()

    if not user_owns_chat(
        user["id"],
        chat_id,
    ):

        return jsonify({
            "error": "Chat not found."
        }), 404

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
                WHERE user_id = %s
                  AND chat_id = %s
                ORDER BY created_at ASC
                """,
                (
                    user["id"],
                    chat_id,
                ),
            )

            messages = cur.fetchall()

            return jsonify({
                "messages": messages
            })

    finally:
        conn.close()


# ============================================================
# DELETE CHAT
# ============================================================

@app.route(
    "/api/chats/<int:chat_id>",
    methods=["DELETE"],
)
@login_required
def delete_chat(chat_id):

    if not csrf_protect():

        return jsonify({
            "error": "Invalid security token."
        }), 403

    user = current_user()

    conn = get_db()

    try:

        with conn.cursor() as cur:

            # Confirm ownership
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

                return jsonify({
                    "error": "Chat not found."
                }), 404

            # Delete messages belonging to chat
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

            # Delete chat
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

            return jsonify({
                "success": True
            })

    except Exception:

        conn.rollback()

        logging.exception(
            "Failed to delete chat"
        )

        return jsonify({
            "error": "Could not delete chat."
        }), 500

    finally:
        conn.close()


# ============================================================
# RESET CURRENT CHAT
# ============================================================

@app.route(
    "/api/reset",
    methods=["POST"],
)
@login_required
def reset_chat():

    if not csrf_protect():

        return jsonify({
            "error": "Invalid security token."
        }), 403

    data = request.get_json(
        silent=True
    ) or {}

    chat_id = data.get("chat_id")

    if not chat_id:

        return jsonify({
            "error": "Chat ID is required."
        }), 400

    user = current_user()

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
                SET
                    title = 'New chat',
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

            return jsonify({
                "success": True
            })

    finally:
        conn.close()


# ============================================================
# AI CHAT
# ============================================================

@app.route(
    "/api/chat",
    methods=["POST"],
)
@login_required
def chat():

    if not csrf_protect():

        return jsonify({
            "error": "Invalid security token."
        }), 403

    data = request.get_json(
        silent=True
    ) or {}

    message = str(
        data.get("message", "")
    ).strip()

    chat_id = data.get("chat_id")

    if not message:

        return jsonify({
            "error": "Message cannot be empty."
        }), 400

    if len(message) > MAX_MESSAGE_LENGTH:

        return jsonify({
            "error": "Message is too long."
        }), 400

    user = current_user()

    # --------------------------------------------------------
    # CREATE CHAT IF NEEDED
    # --------------------------------------------------------

    if not chat_id:

        chat = create_chat_for_user(
            user["id"],
            "New chat",
        )

        chat_id = chat["id"]

    else:

        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):

            return jsonify({
                "error": "Invalid chat."
            }), 400

        if not user_owns_chat(
            user["id"],
            chat_id,
        ):

            return jsonify({
                "error": "Chat not found."
            }), 404

    conn = get_db()

    try:

        with conn.cursor() as cur:

            # ------------------------------------------------
            # SAVE USER MESSAGE
            # ------------------------------------------------

            cur.execute(
                """
                INSERT INTO web_messages
                (
                    user_id,
                    chat_id,
                    role,
                    content
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    user["id"],
                    chat_id,
                    "user",
                    message,
                ),
            )

            # ------------------------------------------------
            # LOAD RECENT HISTORY
            # ------------------------------------------------

            cur.execute(
                """
                SELECT
                    role,
                    content
                FROM web_messages
                WHERE user_id = %s
                  AND chat_id = %s
                ORDER BY created_at DESC
                LIMIT 20
                """,
                (
                    user["id"],
                    chat_id,
                ),
            )

            history_rows = cur.fetchall()

            history_rows.reverse()

            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are AskOra, a helpful AI assistant. "
                        "Answer clearly, accurately and naturally. "
                        "Use Markdown when useful. "
                        "Do not claim to have generated or analyzed "
                        "images because image features are not enabled. "
                        "Keep answers appropriately concise unless "
                        "the user asks for detail."
                    ),
                }
            ]

            for row in history_rows:

                messages.append({
                    "role": row["role"],
                    "content": row["content"],
                })

            # ------------------------------------------------
            # ASK GROQ
            # ------------------------------------------------

            response = groq_client.chat.completions.create(
                model=TEXT_MODEL,
                messages=messages,
                max_tokens=1800,
            )

            answer = (
                response.choices[0]
                .message
                .content
                .strip()
            )

            # ------------------------------------------------
            # SAVE AI MESSAGE
            # ------------------------------------------------

            cur.execute(
                """
                INSERT INTO web_messages
                (
                    user_id,
                    chat_id,
                    role,
                    content
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    user["id"],
                    chat_id,
                    "assistant",
                    answer,
                ),
            )

            # ------------------------------------------------
            # CHAT TITLE
            # ------------------------------------------------

            cur.execute(
                """
                SELECT title
                FROM web_chats
                WHERE id = %s
                  AND user_id = %s
                """,
                (
                    chat_id,
                    user["id"],
                ),
            )

            current_chat = cur.fetchone()

            if current_chat:

                title = current_chat["title"]

                if title == "New chat":

                    title = message[:60].strip()

                    if not title:
                        title = "New chat"

                    cur.execute(
                        """
                        UPDATE web_chats
                        SET
                            title = %s,
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

            # ------------------------------------------------
            # USAGE EVENT
            # ------------------------------------------------

            cur.execute(
                """
                INSERT INTO web_usage_events
                (
                    user_id,
                    event_type
                )
                VALUES (%s, %s)
                """,
                (
                    user["id"],
                    "chat",
                ),
            )

            # ------------------------------------------------
            # LAST SEEN
            # ------------------------------------------------

            cur.execute(
                """
                UPDATE web_users
                SET last_seen = NOW()
                WHERE id = %s
                """,
                (user["id"],),
            )

            conn.commit()

            return jsonify({
                "answer": answer,
                "chat_id": chat_id,
            })

    except Exception:

        conn.rollback()

        logging.exception(
            "AI chat error"
        )

        return jsonify({
            "error": (
                "Something went wrong while "
                "generating the response."
            )
        }), 500

    finally:
        conn.close()


# ============================================================
# VOICE
# ============================================================

@app.route(
    "/api/voice",
    methods=["POST"],
)
@login_required
def voice():

    if not csrf_protect():

        return jsonify({
            "error": "Invalid security token."
        }), 403

    audio = request.files.get("audio")

    if not audio:

        return jsonify({
            "error": "No audio was received."
        }), 400

    audio_bytes = audio.read()

    if not audio_bytes:

        return jsonify({
            "error": "Audio file is empty."
        }), 400

    if len(audio_bytes) > MAX_AUDIO_SIZE:

        return jsonify({
            "error": "Audio file is too large."
        }), 400

    chat_id = request.form.get(
        "chat_id"
    )

    user = current_user()

    # --------------------------------------------------------
    # CHAT
    # --------------------------------------------------------

    if not chat_id:

        chat = create_chat_for_user(
            user["id"],
            "New chat",
        )

        chat_id = chat["id"]

    else:

        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):

            return jsonify({
                "error": "Invalid chat."
            }), 400

        if not user_owns_chat(
            user["id"],
            chat_id,
        ):

            return jsonify({
                "error": "Chat not found."
            }), 404

    try:

        # ----------------------------------------------------
        # TRANSCRIBE
        # ----------------------------------------------------

        transcription = groq_client.audio.transcriptions.create(
            file=(
                "recording.webm",
                BytesIO(audio_bytes),
                audio.mimetype or "audio/webm",
            ),
            model=VOICE_MODEL,
            response_format="text",
        )

        if isinstance(transcription, str):
            text = transcription.strip()
        else:
            text = str(transcription).strip()

        if not text:

            return jsonify({
                "error": "I couldn't understand the recording."
            }), 400

        # ----------------------------------------------------
        # USE SAME AI CHAT SYSTEM
        # ----------------------------------------------------

        conn = get_db()

        try:

            with conn.cursor() as cur:

                cur.execute(
                    """
                    INSERT INTO web_messages
                    (
                        user_id,
                        chat_id,
                        role,
                        content
                    )
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        user["id"],
                        chat_id,
                        "user",
                        text,
                    ),
                )

                cur.execute(
                    """
                    SELECT
                        role,
                        content
                    FROM web_messages
                    WHERE user_id = %s
                      AND chat_id = %s
                    ORDER BY created_at DESC
                    LIMIT 20
                    """,
                    (
                        user["id"],
                        chat_id,
                    ),
                )

                history_rows = cur.fetchall()

                history_rows.reverse()

                messages = [
                    {
                        "role": "system",
                        "content": (
                            "You are AskOra, a helpful AI assistant. "
                            "Answer clearly, accurately and naturally. "
                            "Use Markdown when useful. "
                            "Keep answers appropriately concise."
                        ),
                    }
                ]

                for row in history_rows:

                    messages.append({
                        "role": row["role"],
                        "content": row["content"],
                    })

                response = (
                    groq_client
                    .chat
                    .completions
                    .create(
                        model=TEXT_MODEL,
                        messages=messages,
                        max_tokens=1800,
                    )
                )

                answer = (
                    response.choices[0]
                    .message
                    .content
                    .strip()
                )

                cur.execute(
                    """
                    INSERT INTO web_messages
                    (
                        user_id,
                        chat_id,
                        role,
                        content
                    )
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        user["id"],
                        chat_id,
                        "assistant",
                        answer,
                    ),
                )

                cur.execute(
                    """
                    SELECT title
                    FROM web_chats
                    WHERE id = %s
                      AND user_id = %s
                    """,
                    (
                        chat_id,
                        user["id"],
                    ),
                )

                current_chat = cur.fetchone()

                if current_chat:

                    title = current_chat["title"]

                    if title == "New chat":

                        title = text[:60].strip()

                        cur.execute(
                            """
                            UPDATE web_chats
                            SET
                                title = %s,
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

                cur.execute(
                    """
                    INSERT INTO web_usage_events
                    (
                        user_id,
                        event_type
                    )
                    VALUES (%s, %s)
                    """,
                    (
                        user["id"],
                        "voice",
                    ),
                )

                cur.execute(
                    """
                    UPDATE web_users
                    SET last_seen = NOW()
                    WHERE id = %s
                    """,
                    (user["id"],),
                )

                conn.commit()

                return jsonify({
                    "transcription": text,
                    "answer": answer,
                    "chat_id": chat_id,
                })

        finally:
            conn.close()

    except Exception:

        logging.exception(
            "Voice processing error"
        )

        return jsonify({
            "error": (
                "Something went wrong while "
                "processing your voice message."
            )
        }), 500


# ============================================================
# ADMIN
# ============================================================

@app.route("/admin")
@login_required
def admin_page():

    user = current_user()

    if not is_admin_user(user):
        return redirect(url_for("index"))

    return render_template(
        "admin.html",
        user=user,
        csrf_token=get_csrf_token(),
    )


@app.route("/api/admin/stats")
@admin_required
def admin_stats():

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM web_users
                """
            )

            total_users = cur.fetchone()["total"]

            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM web_users
                WHERE first_seen::date = CURRENT_DATE
                """
            )

            users_today = cur.fetchone()["total"]

            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM web_users
                WHERE last_seen::date = CURRENT_DATE
                """
            )

            active_today = cur.fetchone()["total"]

            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM web_chats
                """
            )

            total_chats = cur.fetchone()["total"]

            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM web_messages
                """
            )

            total_messages = cur.fetchone()["total"]

            cur.execute(
                """
                SELECT
                    event_type,
                    COUNT(*) AS total
                FROM web_usage_events
                GROUP BY event_type
                ORDER BY total DESC
                """
            )

            events = cur.fetchall()

            cur.execute(
                """
                SELECT
                    u.username,
                    u.email,
                    u.first_seen,
                    u.last_seen,
                    COUNT(DISTINCT c.id) AS chats,
                    COUNT(DISTINCT m.id) AS messages
                FROM web_users u
                LEFT JOIN web_chats c
                    ON c.user_id = u.id
                LEFT JOIN web_messages m
                    ON m.user_id = u.id
                GROUP BY
                    u.id,
                    u.username,
                    u.email,
                    u.first_seen,
                    u.last_seen
                ORDER BY u.last_seen DESC
                LIMIT 100
                """
            )

            users = cur.fetchall()

            return jsonify({
                "total_users": total_users,
                "users_today": users_today,
                "active_today": active_today,
                "total_chats": total_chats,
                "total_messages": total_messages,
                "events": events,
                "users": users,
            })

    finally:
        conn.close()


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "service": "AskOra",
    })


# ============================================================
# STARTUP
# ============================================================

init_db()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
    )
