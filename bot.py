import os
import re
import secrets
import logging
from io import BytesIO
from functools import wraps
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

import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.exceptions import RequestEntityTooLarge
from email.utils import parseaddr
from groq import Groq


# ============================================================
# SETTINGS
# ============================================================

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

PORT = int(os.environ.get("PORT", "10000"))

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

MAX_VOICE_SIZE = 15 * 1024 * 1024


# ============================================================
# APP
# ============================================================

app = Flask(__name__)

app.secret_key = SESSION_SECRET

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=MAX_VOICE_SIZE,
)

groq_client = Groq(api_key=GROQ_API_KEY)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# DATABASE
# ============================================================

def get_db():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_db()

    try:
        cur = conn.cursor()

        # ----------------------------------------------------
        # Existing web users table
        # ----------------------------------------------------

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS web_users (
                id BIGSERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        # Add email safely if this table already existed
        cur.execute(
            """
            ALTER TABLE web_users
            ADD COLUMN IF NOT EXISTS email TEXT
            """
        )

        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_web_users_email_lower
            ON web_users (LOWER(email))
            WHERE email IS NOT NULL
            """
        )

        # ----------------------------------------------------
        # Chats
        # ----------------------------------------------------

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
            CREATE INDEX IF NOT EXISTS idx_web_chats_user_updated
            ON web_chats(user_id, updated_at DESC)
            """
        )

        # ----------------------------------------------------
        # Existing messages table
        # ----------------------------------------------------

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

        # Add chat_id to existing table
        cur.execute(
            """
            ALTER TABLE web_messages
            ADD COLUMN IF NOT EXISTS chat_id BIGINT
            """
        )

        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_web_messages_chat
            ON web_messages(chat_id, created_at)
            """
        )

        # ----------------------------------------------------
        # Usage events
        # ----------------------------------------------------

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS web_usage_events (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT
                    REFERENCES web_users(id)
                    ON DELETE SET NULL,
                event_type TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        # ----------------------------------------------------
        # Migrate old messages into a previous chat
        # ----------------------------------------------------

        cur.execute(
            """
            SELECT id
            FROM web_users
            WHERE id IN (
                SELECT DISTINCT user_id
                FROM web_messages
                WHERE chat_id IS NULL
            )
            """
        )

        users_with_old_messages = [
            row[0] for row in cur.fetchall()
        ]

        for user_id in users_with_old_messages:

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
                chat_id = existing_chat[0]
            else:
                cur.execute(
                    """
                    INSERT INTO web_chats
                        (user_id, title)
                    VALUES
                        (%s, %s)
                    RETURNING id
                    """,
                    (user_id, "Previous chat"),
                )

                chat_id = cur.fetchone()[0]

            cur.execute(
                """
                UPDATE web_messages
                SET chat_id = %s
                WHERE user_id = %s
                  AND chat_id IS NULL
                """,
                (chat_id, user_id),
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

        logger.info("Database initialized successfully.")

    except Exception:
        conn.rollback()
        logger.exception("Database initialization failed.")
        raise

    finally:
        conn.close()


# ============================================================
# SECURITY
# ============================================================

def get_csrf_token():
    token = session.get("csrf_token")

    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token

    return token


def csrf_valid():
    supplied = request.headers.get("X-CSRF-Token")

    if not supplied:
        supplied = request.form.get("csrf_token")

    expected = session.get("csrf_token")

    return (
        supplied
        and expected
        and secrets.compare_digest(supplied, expected)
    )


def login_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):

        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({
                    "error": "Authentication required."
                }), 401

            return redirect(url_for("login"))

        return function(*args, **kwargs)

    return wrapper


def admin_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):

        user = get_current_user()

        if not user:
            if request.path.startswith("/api/"):
                return jsonify({
                    "error": "Authentication required."
                }), 401

            return redirect(url_for("login"))

        if user["username"].lower() != ADMIN_USERNAME:
            if request.path.startswith("/api/"):
                return jsonify({
                    "error": "Admin access required."
                }), 403

            return "Forbidden", 403

        return function(*args, **kwargs)

    return wrapper


# ============================================================
# VALIDATION
# ============================================================

USERNAME_PATTERN = re.compile(
    r"^[a-z0-9_]{3,32}$"
)


def valid_username(username):
    return bool(
        USERNAME_PATTERN.fullmatch(username)
    )


def valid_email(email):
    email = email.strip().lower()

    if len(email) > 254:
        return False

    name, address = parseaddr(email)

    if not address or address != email:
        return False

    return bool(
        re.fullmatch(
            r"[^@\s]+@[^@\s]+\.[^@\s]+",
            email,
        )
    )


# ============================================================
# USER HELPERS
# ============================================================

def get_current_user():
    user_id = session.get("user_id")

    if not user_id:
        return None

    conn = get_db()

    try:
        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

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


def log_event(user_id, event_type):
    conn = get_db()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO web_usage_events
                (user_id, event_type)
            VALUES
                (%s, %s)
            """,
            (user_id, event_type),
        )

        conn.commit()

    except Exception:
        conn.rollback()
        logger.exception("Could not log event.")

    finally:
        conn.close()


# ============================================================
# AUTH PAGES
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if session.get("user_id"):
        return redirect(url_for("home"))

    error = None

    if request.method == "POST":

        identifier = request.form.get(
            "identifier",
            "",
        ).strip().lower()

        password = request.form.get(
            "password",
            "",
        )

        if not csrf_valid():
            error = "Security check failed."

        elif not identifier or not password:
            error = "Please enter your login details."

        else:
            conn = get_db()

            try:
                cur = conn.cursor(
                    cursor_factory=RealDictCursor
                )

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

                if (
                    user
                    and check_password_hash(
                        user["password_hash"],
                        password,
                    )
                ):
                    session.clear()

                    session["user_id"] = user["id"]
                    session["csrf_token"] = secrets.token_urlsafe(32)

                    cur.execute(
                        """
                        UPDATE web_users
                        SET last_seen = NOW()
                        WHERE id = %s
                        """,
                        (user["id"],),
                    )

                    conn.commit()

                    log_event(
                        user["id"],
                        "login",
                    )

                    return redirect(url_for("home"))

                error = "Invalid username/email or password."

            finally:
                conn.close()

    return render_template(
        "login.html",
        csrf_token=get_csrf_token(),
        error=error,
    )


@app.route("/register", methods=["GET", "POST"])
def register():

    if session.get("user_id"):
        return redirect(url_for("home"))

    error = None

    if request.method == "POST":

        username = request.form.get(
            "username",
            "",
        ).strip().lower()

        email = request.form.get(
            "email",
            "",
        ).strip().lower()

        password = request.form.get(
            "password",
            "",
        )

        if not csrf_valid():
            error = "Security check failed."

        elif not valid_username(username):
            error = (
                "Username must be 3-32 characters "
                "using lowercase letters, numbers or underscores."
            )

        elif not valid_email(email):
            error = "Please enter a valid email address."

        elif len(password) < 8:
            error = "Password must be at least 8 characters."

        else:
            conn = get_db()

            try:
                cur = conn.cursor()

                cur.execute(
                    """
                    SELECT id
                    FROM web_users
                    WHERE LOWER(username) = %s
                    LIMIT 1
                    """,
                    (username,),
                )

                if cur.fetchone():
                    error = "That username is already taken."

                else:
                    cur.execute(
                        """
                        SELECT id
                        FROM web_users
                        WHERE LOWER(email) = %s
                        LIMIT 1
                        """,
                        (email,),
                    )

                    if cur.fetchone():
                        error = "That email is already registered."

                    else:
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
                                generate_password_hash(
                                    password
                                ),
                            ),
                        )

                        user_id = cur.fetchone()[0]

                        conn.commit()

                        session.clear()

                        session["user_id"] = user_id
                        session["csrf_token"] = secrets.token_urlsafe(32)

                        log_event(
                            user_id,
                            "register",
                        )

                        return redirect(url_for("home"))

            except Exception:
                conn.rollback()
                logger.exception(
                    "Registration failed."
                )
                error = "Could not create your account."

            finally:
                conn.close()

    return render_template(
        "register.html",
        csrf_token=get_csrf_token(),
        error=error,
    )


@app.route("/logout", methods=["POST", "GET"])
def logout():

    user_id = session.get("user_id")

    if user_id:
        log_event(
            user_id,
            "logout",
        )

    session.clear()

    if request.method == "GET":
        return redirect(url_for("login"))

    return jsonify({
        "ok": True
    })


# ============================================================
# MAIN PAGE
# ============================================================

@app.route("/")
@login_required
def home():

    user = get_current_user()

    return render_template(
        "index.html",
        user=user,
        csrf_token=get_csrf_token(),
    )


# ============================================================
# CURRENT USER
# ============================================================

@app.route("/api/me")
@login_required
def api_me():

    user = get_current_user()

    if not user:
        return jsonify({
            "error": "User not found."
        }), 404

    return jsonify({
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "is_admin": (
            user["username"].lower()
            == ADMIN_USERNAME
        ),
    })


# ============================================================
# CHATS
# ============================================================

@app.route("/api/chats", methods=["GET"])
@login_required
def get_chats():

    user_id = session["user_id"]

    conn = get_db()

    try:
        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        cur.execute(
            """
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
                c.id
            ORDER BY
                c.updated_at DESC
            """,
            (user_id,),
        )

        chats = cur.fetchall()

        return jsonify({
            "chats": chats
        })

    finally:
        conn.close()


@app.route("/api/chats", methods=["POST"])
@login_required
def create_chat():

    if not csrf_valid():
        return jsonify({
            "error": "Security check failed."
        }), 403

    user_id = session["user_id"]

    conn = get_db()

    try:
        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        cur.execute(
            """
            INSERT INTO web_chats
                (user_id, title)
            VALUES
                (%s, %s)
            RETURNING
                id,
                title,
                created_at,
                updated_at
            """,
            (
                user_id,
                "New chat",
            ),
        )

        chat = cur.fetchone()

        conn.commit()

        log_event(
            user_id,
            "new_chat",
        )

        return jsonify({
            "chat": chat
        })

    finally:
        conn.close()


@app.route(
    "/api/chats/<int:chat_id>/messages",
    methods=["GET"],
)
@login_required
def get_chat_messages(chat_id):

    user_id = session["user_id"]

    conn = get_db()

    try:
        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

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

        if not cur.fetchone():
            return jsonify({
                "error": "Chat not found."
            }), 404

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
                user_id,
            ),
        )

        messages = cur.fetchall()

        return jsonify({
            "messages": messages
        })

    finally:
        conn.close()


@app.route(
    "/api/chats/<int:chat_id>",
    methods=["DELETE"],
)
@login_required
def delete_chat(chat_id):

    if not csrf_valid():
        return jsonify({
            "error": "Security check failed."
        }), 403

    user_id = session["user_id"]

    conn = get_db()

    try:
        cur = conn.cursor()

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

        if not cur.fetchone():
            return jsonify({
                "error": "Chat not found."
            }), 404

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

        log_event(
            user_id,
            "delete_chat",
        )

        return jsonify({
            "ok": True
        })

    finally:
        conn.close()


# ============================================================
# AI CHAT
# ============================================================

@app.route("/api/chat", methods=["POST"])
@login_required
def api_chat():

    if not csrf_valid():
        return jsonify({
            "error": "Security check failed."
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

    if len(message) > 12000:
        return jsonify({
            "error": "Message is too long."
        }), 400

    user_id = session["user_id"]

    conn = get_db()

    try:
        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        # ----------------------------------------------------
        # Make sure chat belongs to user
        # ----------------------------------------------------

        if chat_id:

            try:
                chat_id = int(chat_id)
            except (ValueError, TypeError):
                chat_id = None

        if not chat_id:

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
                    "New chat",
                ),
            )

            chat_id = cur.fetchone()["id"]

        else:

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

            if not cur.fetchone():
                return jsonify({
                    "error": "Chat not found."
                }), 404

        # ----------------------------------------------------
        # Save user message
        # ----------------------------------------------------

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
                user_id,
                "user",
                message,
            ),
        )

        # ----------------------------------------------------
        # Get recent conversation
        # ----------------------------------------------------

        cur.execute(
            """
            SELECT
                role,
                content
            FROM web_messages
            WHERE chat_id = %s
              AND user_id = %s
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (
                chat_id,
                user_id,
            ),
        )

        history = list(
            reversed(cur.fetchall())
        )

        # ----------------------------------------------------
        # AI messages
        # ----------------------------------------------------

        ai_messages = [
            {
                "role": "system",
                "content": (
                    "You are AskOra, a helpful, smart, "
                    "friendly and reliable AI assistant. "
                    "Answer clearly and naturally. "
                    "Use simple explanations when possible. "
                    "Do not pretend to know things you do not know."
                ),
            }
        ]

        for item in history:

            role = item["role"]

            if role not in (
                "user",
                "assistant",
            ):
                continue

            ai_messages.append({
                "role": role,
                "content": item["content"],
            })

        # ----------------------------------------------------
        # Call Groq
        # ----------------------------------------------------

        response = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=ai_messages,
            temperature=0.7,
            max_tokens=2000,
        )

        answer = (
            response.choices[0]
            .message
            .content
            .strip()
        )

        # ----------------------------------------------------
        # Save assistant answer
        # ----------------------------------------------------

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
                user_id,
                "assistant",
                answer,
            ),
        )

        # ----------------------------------------------------
        # Generate title from first user message
        # ----------------------------------------------------

        cur.execute(
            """
            SELECT title
            FROM web_chats
            WHERE id = %s
            """,
            (chat_id,),
        )

        current_chat = cur.fetchone()

        if (
            current_chat
            and current_chat["title"]
            == "New chat"
        ):

            title = message[:50].strip()

            if len(message) > 50:
                title += "..."

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
                    title or "New chat",
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

        cur.execute(
            """
            UPDATE web_users
            SET last_seen = NOW()
            WHERE id = %s
            """,
            (user_id,),
        )

        conn.commit()

        log_event(
            user_id,
            "chat_message",
        )

        return jsonify({
            "ok": True,
            "chat_id": chat_id,
            "answer": answer,
        })

    except Exception as error:

        conn.rollback()

        logger.exception(
            "AI chat error: %s",
            error,
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

@app.route("/api/voice", methods=["POST"])
@login_required
def api_voice():

    if not csrf_valid():
        return jsonify({
            "error": "Security check failed."
        }), 403

    audio = request.files.get("audio")

    if not audio:
        return jsonify({
            "error": "No audio was received."
        }), 400

    audio_bytes = audio.read()

    if not audio_bytes:
        return jsonify({
            "error": "The recording was empty."
        }), 400

    if len(audio_bytes) > MAX_VOICE_SIZE:
        return jsonify({
            "error": "Voice recording is too large."
        }), 413

    user_id = session["user_id"]

    filename = audio.filename or "recording.webm"

    mimetype = (
        audio.mimetype
        or "audio/webm"
    )

    try:

        transcription = groq_client.audio.transcriptions.create(
            file=(
                filename,
                BytesIO(audio_bytes),
                mimetype,
            ),
            model=VOICE_MODEL,
            response_format="text",
        )

        text = str(
            transcription
        ).strip()

        if not text:
            return jsonify({
                "error": "I couldn't understand the recording."
            }), 400

        # Send transcription into the normal chat flow
        data = {
            "message": text,
            "chat_id": request.form.get(
                "chat_id"
            ),
        }

        # Reuse AI logic by directly doing it here
        chat_id = data["chat_id"]

        conn = get_db()

        try:

            cur = conn.cursor(
                cursor_factory=RealDictCursor
            )

            if chat_id:

                try:
                    chat_id = int(chat_id)
                except (ValueError, TypeError):
                    chat_id = None

            if not chat_id:

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
                        "New chat",
                    ),
                )

                chat_id = cur.fetchone()["id"]

            else:

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

                if not cur.fetchone():
                    return jsonify({
                        "error": "Chat not found."
                    }), 404

            # Save transcription
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
                    user_id,
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
                WHERE chat_id = %s
                  AND user_id = %s
                ORDER BY created_at DESC
                LIMIT 20
                """,
                (
                    chat_id,
                    user_id,
                ),
            )

            history = list(
                reversed(cur.fetchall())
            )

            ai_messages = [
                {
                    "role": "system",
                    "content": (
                        "You are AskOra, a helpful, smart, "
                        "friendly and reliable AI assistant. "
                        "Answer clearly and naturally."
                    ),
                }
            ]

            for item in history:

                if item["role"] in (
                    "user",
                    "assistant",
                ):
                    ai_messages.append({
                        "role": item["role"],
                        "content": item["content"],
                    })

            response = groq_client.chat.completions.create(
                model=TEXT_MODEL,
                messages=ai_messages,
                temperature=0.7,
                max_tokens=2000,
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
                    user_id,
                    "assistant",
                    answer,
                ),
            )

            cur.execute(
                """
                SELECT title
                FROM web_chats
                WHERE id = %s
                """,
                (chat_id,),
            )

            current_chat = cur.fetchone()

            if (
                current_chat
                and current_chat["title"]
                == "New chat"
            ):

                title = text[:50].strip()

                if len(text) > 50:
                    title += "..."

                cur.execute(
                    """
                    UPDATE web_chats
                    SET
                        title = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        title or "Voice chat",
                        chat_id,
                    ),
                )

            else:

                cur.execute(
                    """
                    UPDATE web_chats
                    SET updated_at = NOW()
                    WHERE id = %s
                    """,
                    (chat_id,),
                )

            cur.execute(
                """
                UPDATE web_users
                SET last_seen = NOW()
                WHERE id = %s
                """,
                (user_id,),
            )

            conn.commit()

            log_event(
                user_id,
                "voice_message",
            )

            return jsonify({
                "ok": True,
                "chat_id": chat_id,
                "transcription": text,
                "answer": answer,
            })

        finally:
            conn.close()

    except Exception as error:

        logger.exception(
            "Voice error: %s",
            error,
        )

        return jsonify({
            "error": (
                "Something went wrong processing "
                "your voice message."
            )
        }), 500


# ============================================================
# ADMIN
# ============================================================

@app.route("/admin")
@admin_required
def admin():

    return render_template(
        "admin.html",
        csrf_token=get_csrf_token(),
    )


@app.route("/api/admin/stats")
@admin_required
def admin_stats():

    conn = get_db()

    try:

        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        # Users
        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM web_users
            """
        )

        total_users = cur.fetchone()["count"]

        # Users today
        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM web_users
            WHERE first_seen >= CURRENT_DATE
            """
        )

        users_today = cur.fetchone()["count"]

        # Active today
        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM web_users
            WHERE last_seen >= CURRENT_DATE
            """
        )

        active_today = cur.fetchone()["count"]

        # Chats
        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM web_chats
            """
        )

        total_chats = cur.fetchone()["count"]

        # Messages
        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM web_messages
            """
        )

        total_messages = cur.fetchone()["count"]

        # Events
        cur.execute(
            """
            SELECT
                event_type,
                COUNT(*) AS count
            FROM web_usage_events
            GROUP BY event_type
            ORDER BY count DESC
            """
        )

        events = cur.fetchall()

        # User list
        cur.execute(
            """
            SELECT
                u.id,
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
                u.id

            ORDER BY
                u.last_seen DESC

            LIMIT 100
            """
        )

        users = cur.fetchall()

        return jsonify({
            "stats": {
                "total_users": total_users,
                "users_today": users_today,
                "active_today": active_today,
                "total_chats": total_chats,
                "total_messages": total_messages,
            },
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

    try:

        conn = get_db()

        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()

        finally:
            conn.close()

        return jsonify({
            "status": "ok",
            "service": "AskOra",
        })

    except Exception:

        return jsonify({
            "status": "error",
        }), 500


@app.errorhandler(RequestEntityTooLarge)
def handle_large_file(error):

    return jsonify({
        "error": "The uploaded file is too large."
    }), 413


# ============================================================
# STARTUP
# ============================================================

try:
    init_db()
except Exception:
    logger.exception(
        "Startup database initialization failed."
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
    )
