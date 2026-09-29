import os
import re
import logging
from datetime import datetime, timezone

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import psycopg2
from psycopg2.extras import RealDictCursor

from groq import Groq

from werkzeug.security import (
    check_password_hash,
    generate_password_hash,
)

# =========================================================
# CONFIG
# =========================================================

app = Flask(__name__)

app.secret_key = os.environ["SESSION_SECRET"]

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("COOKIE_SECURE", "true").lower() == "true"
)

DATABASE_URL = os.environ["DATABASE_URL"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").strip().lower()

GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_VOICE_MODEL = "whisper-large-v3-turbo"

groq_client = Groq(api_key=GROQ_API_KEY)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


# =========================================================
# DATABASE
# =========================================================

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def init_database():
    conn = get_db_connection()

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
                    email TEXT UNIQUE NOT NULL,
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
                    user_id BIGINT NOT NULL,
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
                    chat_id BIGINT,
                    user_id BIGINT,
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
                    user_id BIGINT,
                    event_type TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            # -------------------------------------------------
            # SAFE MIGRATIONS
            # -------------------------------------------------

            cur.execute(
                """
                ALTER TABLE web_chats
                ADD COLUMN IF NOT EXISTS updated_at
                TIMESTAMPTZ NOT NULL DEFAULT NOW()
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

            # -------------------------------------------------
            # INDEXES
            # -------------------------------------------------

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_web_chats_user_id
                ON web_chats(user_id)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_web_chats_updated_at
                ON web_chats(updated_at DESC)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_web_messages_chat_id
                ON web_messages(chat_id)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_web_messages_user_id
                ON web_messages(user_id)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_web_usage_user_id
                ON web_usage_events(user_id)
                """
            )

            # -------------------------------------------------
            # MIGRATE OLD WEB MESSAGES WITHOUT CHAT ID
            # -------------------------------------------------

            cur.execute(
                """
                SELECT DISTINCT user_id
                FROM web_messages
                WHERE user_id IS NOT NULL
                  AND chat_id IS NULL
                """
            )

            users_with_old_messages = cur.fetchall()

            for row in users_with_old_messages:

                user_id = row[0]

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
                        VALUES (%s, %s)
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

        conn.commit()

    except Exception:
        conn.rollback()
        logger.exception("Database initialization error")
        raise

    finally:
        conn.close()


# =========================================================
# AUTH HELPERS
# =========================================================

def current_user_id():
    return session.get("user_id")


def login_required():
    return current_user_id() is not None


def get_current_user():
    user_id = current_user_id()

    if not user_id:
        return None

    conn = get_db_connection()

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
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


def is_admin_user(user):
    if not user:
        return False

    return (
        ADMIN_USERNAME
        and user["username"].strip().lower()
        == ADMIN_USERNAME
    )


# =========================================================
# CSRF
# =========================================================

def get_csrf_token():
    token = session.get("csrf_token")

    if not token:
        import secrets

        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token

    return token


def verify_csrf():
    expected = session.get("csrf_token")

    supplied = (
        request.headers.get("X-CSRF-Token")
        or request.form.get("csrf_token")
        or request.json.get("csrf_token")
        if request.is_json
        else request.form.get("csrf_token")
    )

    if not expected or not supplied:
        return False

    return supplied == expected


# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():
    if not login_required():
        return redirect(url_for("login"))

    return render_template("index.html")


# =========================================================
# LOGIN
# =========================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "GET":

        if login_required():
            return redirect("/")

        return render_template("login.html")

    # -----------------------------------------------------
    # POST LOGIN
    # -----------------------------------------------------

    login_value = request.form.get("login", "").strip()
    password = request.form.get("password", "")

    if not login_value or not password:
        return render_template(
            "login.html",
            error="Please enter your username, email and password.",
        )

    conn = get_db_connection()

    try:

        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            # Accept either username OR email.
            #
            # LOWER() makes email/username matching
            # case-insensitive.

            cur.execute(
                """
                SELECT
                    id,
                    username,
                    email,
                    password_hash
                FROM web_users
                WHERE LOWER(username) = LOWER(%s)
                   OR LOWER(email) = LOWER(%s)
                LIMIT 1
                """,
                (login_value, login_value),
            )

            user = cur.fetchone()

        if not user:

            return render_template(
                "login.html",
                error="Invalid username/email or password.",
            )

        if not check_password_hash(
            user["password_hash"],
            password,
        ):

            return render_template(
                "login.html",
                error="Invalid username/email or password.",
            )

        # -------------------------------------------------
        # LOGIN SUCCESS
        # -------------------------------------------------

        session.clear()

        session["user_id"] = user["id"]
        session["username"] = user["username"]

        get_csrf_token()

        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE web_users
                SET last_seen = NOW()
                WHERE id = %s
                """,
                (user["id"],),
            )

        conn.commit()

        logger.info(
            "Successful login for user %s",
            user["username"],
        )

        return redirect("/")

    except Exception:

        conn.rollback()

        logger.exception("Login error")

        return render_template(
            "login.html",
            error=(
                "Something went wrong while logging in. "
                "Please try again."
            ),
        )

    finally:
        conn.close()


# =========================================================
# REGISTER
# =========================================================

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "GET":

        if login_required():
            return redirect("/")

        return render_template("register.html")

    # -----------------------------------------------------
    # ACCEPT NORMAL FORM DATA
    # -----------------------------------------------------

    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not username or not email or not password:
        return render_template(
            "register.html",
            error="Please fill in your username, email and password.",
        )

    # -----------------------------------------------------
    # USERNAME VALIDATION
    # -----------------------------------------------------

    if not re.fullmatch(
        r"[A-Za-z0-9_]{3,30}",
        username,
    ):
        return render_template(
            "register.html",
            error=(
                "Username must be 3–30 characters and "
                "contain only letters, numbers and underscores."
            ),
        )

    # -----------------------------------------------------
    # PASSWORD VALIDATION
    # -----------------------------------------------------

    if len(password) < 8:
        return render_template(
            "register.html",
            error="Password must be at least 8 characters.",
        )

    # -----------------------------------------------------
    # EMAIL BASIC VALIDATION
    # -----------------------------------------------------

    if not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[^@\s]+",
        email,
    ):
        return render_template(
            "register.html",
            error="Please enter a valid email address.",
        )

    password_hash = generate_password_hash(password)

    conn = get_db_connection()

    try:

        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            # Check username.

            cur.execute(
                """
                SELECT id
                FROM web_users
                WHERE LOWER(username) = LOWER(%s)
                LIMIT 1
                """,
                (username,),
            )

            if cur.fetchone():

                return render_template(
                    "register.html",
                    error="That username is already in use.",
                )

            # Check email.

            cur.execute(
                """
                SELECT id
                FROM web_users
                WHERE LOWER(email) = LOWER(%s)
                LIMIT 1
                """,
                (email,),
            )

            if cur.fetchone():

                return render_template(
                    "register.html",
                    error="That email is already registered.",
                )

            # Create user.

            cur.execute(
                """
                INSERT INTO web_users
                (
                    username,
                    email,
                    password_hash,
                    first_seen,
                    last_seen
                )
                VALUES
                (
                    %s,
                    %s,
                    %s,
                    NOW(),
                    NOW()
                )
                RETURNING id, username
                """,
                (
                    username,
                    email,
                    password_hash,
                ),
            )

            user = cur.fetchone()

        conn.commit()

        # Automatically log the user in.

        session.clear()

        session["user_id"] = user["id"]
        session["username"] = user["username"]

        get_csrf_token()

        logger.info(
            "New user registered: %s",
            username,
        )

        return redirect("/")

    except Exception:

        conn.rollback()

        logger.exception("Registration error")

        return render_template(
            "register.html",
            error="Something went wrong while creating your account.",
        )

    finally:
        conn.close()


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout", methods=["GET", "POST"])
def logout():

    session.clear()

    return redirect("/login")


# =========================================================
# CURRENT USER
# =========================================================

@app.route("/api/me")
def api_me():

    user = get_current_user()

    if not user:
        return jsonify(
            {
                "ok": False,
                "logged_in": False,
            }
        )

    return jsonify(
        {
            "ok": True,
            "logged_in": True,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "email": user["email"],
            },
            "username": user["username"],
            "email": user["email"],
            "is_admin": is_admin_user(user),
            "csrf_token": get_csrf_token(),
        }
    )


# =========================================================
# CHAT HELPERS
# =========================================================

def create_chat(user_id, title="New chat"):

    conn = get_db_connection()

    try:

        with conn.cursor(cursor_factory=RealDictCursor) as cur:

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
                    user_id,
                    title,
                    created_at,
                    updated_at
                """,
                (
                    user_id,
                    title,
                ),
            )

            chat = cur.fetchone()

        conn.commit()

        return chat

    except Exception:

        conn.rollback()

        raise

    finally:
        conn.close()


def user_owns_chat(user_id, chat_id):

    conn = get_db_connection()

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

            return cur.fetchone() is not None

    finally:
        conn.close()


# =========================================================
# LIST / CREATE CHATS
# =========================================================

@app.route("/api/chats", methods=["GET", "POST"])
def api_chats():

    if not login_required():
        return jsonify(
            {
                "ok": False,
                "error": "Not logged in.",
            }
        ), 401

    user_id = current_user_id()

    if request.method == "POST":

        chat = create_chat(
            user_id,
            "New chat",
        )

        return jsonify(
            {
                "ok": True,
                "chat": dict(chat),
            }
        )

    conn = get_db_connection()

    try:

        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            cur.execute(
                """
                SELECT
                    c.id,
                    c.title,
                    c.created_at,
                    c.updated_at
                FROM web_chats c
                WHERE c.user_id = %s
                ORDER BY c.updated_at DESC
                """,
                (user_id,),
            )

            chats = cur.fetchall()

        return jsonify(
            {
                "ok": True,
                "chats": [
                    dict(chat)
                    for chat in chats
                ],
            }
        )

    finally:
        conn.close()


# =========================================================
# CHAT MESSAGES
# =========================================================

@app.route(
    "/api/chats/<int:chat_id>/messages",
    methods=["GET"],
)
def api_chat_messages(chat_id):

    if not login_required():
        return jsonify(
            {
                "ok": False,
                "error": "Not logged in.",
            }
        ), 401

    user_id = current_user_id()

    conn = get_db_connection()

    try:

        with conn.cursor(cursor_factory=RealDictCursor) as cur:

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

        return jsonify(
            {
                "ok": True,
                "messages": [
                    dict(message)
                    for message in messages
                ],
            }
        )

    finally:
        conn.close()


# =========================================================
# DELETE CHAT
# =========================================================

@app.route(
    "/api/chats/<int:chat_id>",
    methods=["DELETE"],
)
def delete_chat(chat_id):

    if not login_required():
        return jsonify(
            {
                "ok": False,
                "error": "Not logged in.",
            }
        ), 401

    if not verify_csrf():
        return jsonify(
            {
                "ok": False,
                "error": "Invalid CSRF token.",
            }
        ), 403

    user_id = current_user_id()

    conn = get_db_connection()

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

        return jsonify(
            {
                "ok": True,
            }
        )

    except Exception:

        conn.rollback()

        logger.exception("Delete chat error")

        return jsonify(
            {
                "ok": False,
                "error": "Could not delete chat.",
            }
        ), 500

    finally:
        conn.close()


# =========================================================
# AI RESPONSE HELPERS
# =========================================================

def wants_detail(message):

    return bool(
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


def compact_answer(text):

    if not text:
        return ""

    text = text.strip()

    # Keep normal responses reasonably short.
    max_chars = 4500

    if len(text) <= max_chars:
        return text

    trimmed = text[:max_chars]

    last_stop = max(
        trimmed.rfind("."),
        trimmed.rfind("!"),
        trimmed.rfind("?"),
    )

    if last_stop > 2500:
        return trimmed[:last_stop + 1]

    return trimmed + "…"


def get_chat_history(
    user_id,
    chat_id,
    limit=12,
):

    conn = get_db_connection()

    try:

        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            cur.execute(
                """
                SELECT
                    role,
                    content
                FROM web_messages
                WHERE chat_id = %s
                  AND user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (
                    chat_id,
                    user_id,
                    limit,
                ),
            )

            rows = cur.fetchall()

        rows.reverse()

        return [
            {
                "role": row["role"],
                "content": row["content"],
            }
            for row in rows
        ]

    finally:
        conn.close()


# =========================================================
# CHAT WITH AI
# =========================================================

@app.route("/api/chat", methods=["POST"])
def api_chat():

    if not login_required():
        return jsonify(
            {
                "ok": False,
                "error": "Not logged in.",
            }
        ), 401

    if not verify_csrf():
        return jsonify(
            {
                "ok": False,
                "error": "Invalid CSRF token.",
            }
        ), 403

    data = request.get_json(silent=True) or {}

    message = str(
        data.get("message", "")
    ).strip()

    chat_id = data.get("chat_id")

    if not message:
        return jsonify(
            {
                "ok": False,
                "error": "Please enter a message.",
            }
        ), 400

    user_id = current_user_id()

    # -----------------------------------------------------
    # CREATE CHAT IF NONE WAS PROVIDED
    # -----------------------------------------------------

    if not chat_id:

        first_title = message[:70]

        chat = create_chat(
            user_id,
            first_title,
        )

        chat_id = chat["id"]

    else:

        try:
            chat_id = int(chat_id)
        except (ValueError, TypeError):

            return jsonify(
                {
                    "ok": False,
                    "error": "Invalid chat.",
                }
            ), 400

        if not user_owns_chat(
            user_id,
            chat_id,
        ):

            return jsonify(
                {
                    "ok": False,
                    "error": "Chat not found.",
                }
            ), 404

    # -----------------------------------------------------
    # GET HISTORY
    # -----------------------------------------------------

    history = get_chat_history(
        user_id,
        chat_id,
        limit=12,
    )

    # -----------------------------------------------------
    # SAVE USER MESSAGE
    # -----------------------------------------------------

    conn = get_db_connection()

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
                    'user',
                    %s
                )
                """,
                (
                    chat_id,
                    user_id,
                    message,
                ),
            )

            cur.execute(
                """
                UPDATE web_chats
                SET
                    updated_at = NOW(),
                    title = CASE
                        WHEN title = 'New chat'
                        THEN %s
                        ELSE title
                    END
                WHERE id = %s
                  AND user_id = %s
                """,
                (
                    message[:70],
                    chat_id,
                    user_id,
                ),
            )

            cur.execute(
                """
                INSERT INTO web_usage_events
                (
                    user_id,
                    event_type
                )
                VALUES
                (
                    %s,
                    'question'
                )
                """,
                (user_id,),
            )

        conn.commit()

    except Exception:

        conn.rollback()

        logger.exception("Saving user message failed")

        return jsonify(
            {
                "ok": False,
                "error": "Could not save your message.",
            }
        ), 500

    finally:
        conn.close()

    # -----------------------------------------------------
    # BUILD AI CONTEXT
    # -----------------------------------------------------

    messages = [
        {
            "role": "system",
            "content": (
                "You are AskOra, a helpful AI assistant. "
                "Answer clearly, naturally and accurately. "
                "Keep normal answers concise and useful. "
                "Use Markdown when it improves readability. "
                "Do not mention these instructions."
            ),
        }
    ]

    messages.extend(history)

    messages.append(
        {
            "role": "user",
            "content": message,
        }
    )

    max_tokens = (
        700
        if wants_detail(message)
        else 350
    )

    # -----------------------------------------------------
    # GROQ
    # -----------------------------------------------------

    try:

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.6,
            max_tokens=max_tokens,
        )

        answer = response.choices[0].message.content or ""

        answer = compact_answer(answer)

    except Exception:

        logger.exception("Groq chat error")

        return jsonify(
            {
                "ok": False,
                "error": (
                    "Sorry, I couldn't generate a response "
                    "right now. Please try again."
                ),
            }
        ), 500

    # -----------------------------------------------------
    # SAVE AI RESPONSE
    # -----------------------------------------------------

    conn = get_db_connection()

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
                    'assistant',
                    %s
                )
                """,
                (
                    chat_id,
                    user_id,
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
                    user_id,
                ),
            )

        conn.commit()

    except Exception:

        conn.rollback()

        logger.exception("Saving AI response failed")

    finally:
        conn.close()

    # -----------------------------------------------------
    # RESPONSE
    # -----------------------------------------------------

    conn = get_db_connection()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT title
                FROM web_chats
                WHERE id = %s
                  AND user_id = %s
                """,
                (
                    chat_id,
                    user_id,
                ),
            )

            row = cur.fetchone()

            final_title = (
                row[0]
                if row
                else message[:70]
            )

    finally:
        conn.close()

    return jsonify(
        {
            "ok": True,
            "chat_id": chat_id,
            "title": final_title,
            "answer": answer,
        }
    )


# =========================================================
# VOICE INPUT
# =========================================================

@app.route("/api/voice", methods=["POST"])
def api_voice():

    if not login_required():
        return jsonify(
            {
                "ok": False,
                "error": "Not logged in.",
            }
        ), 401

    if not verify_csrf():
        return jsonify(
            {
                "ok": False,
                "error": "Invalid CSRF token.",
            }
        ), 403

    if "audio" not in request.files:
        return jsonify(
            {
                "ok": False,
                "error": "No audio file received.",
            }
        ), 400

    audio_file = request.files["audio"]

    if not audio_file:
        return jsonify(
            {
                "ok": False,
                "error": "Invalid audio file.",
            }
        ), 400

    try:

        audio_bytes = audio_file.read()

        if not audio_bytes:
            return jsonify(
                {
                    "ok": False,
                    "error": "Audio file is empty.",
                }
            ), 400

        transcription = groq_client.audio.transcriptions.create(
            file=(
                audio_file.filename
                or "voice.webm",
                audio_bytes,
            ),
            model=GROQ_VOICE_MODEL,
        )

        text = getattr(
            transcription,
            "text",
            "",
        )

        text = text.strip()

        if not text:
            return jsonify(
                {
                    "ok": False,
                    "error": "I couldn't understand the recording.",
                }
            ), 400

        user_id = current_user_id()

        conn = get_db_connection()

        try:

            with conn.cursor() as cur:

                cur.execute(
                    """
                    INSERT INTO web_usage_events
                    (
                        user_id,
                        event_type
                    )
                    VALUES
                    (
                        %s,
                        'voice'
                    )
                    """,
                    (user_id,),
                )

            conn.commit()

        finally:
            conn.close()

        return jsonify(
            {
                "ok": True,
                "text": text,
            }
        )

    except Exception:

        logger.exception("Voice transcription error")

        return jsonify(
            {
                "ok": False,
                "error": (
                    "Sorry, I couldn't process "
                    "your voice recording."
                ),
            }
        ), 500


# =========================================================
# RESET CURRENT CHAT
# =========================================================

@app.route("/api/reset", methods=["POST"])
def api_reset():

    if not login_required():
        return jsonify(
            {
                "ok": False,
                "error": "Not logged in.",
            }
        ), 401

    if not verify_csrf():
        return jsonify(
            {
                "ok": False,
                "error": "Invalid CSRF token.",
            }
        ), 403

    data = request.get_json(silent=True) or {}

    chat_id = data.get("chat_id")

    if not chat_id:
        return jsonify(
            {
                "ok": False,
                "error": "No chat selected.",
            }
        ), 400

    user_id = current_user_id()

    conn = get_db_connection()

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
                    user_id,
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
                    user_id,
                ),
            )

        conn.commit()

        return jsonify(
            {
                "ok": True,
            }
        )

    except Exception:

        conn.rollback()

        logger.exception("Reset chat error")

        return jsonify(
            {
                "ok": False,
                "error": "Could not reset chat.",
            }
        ), 500

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

    if not login_required():
        return jsonify(
            {
                "ok": False,
                "error": "Not logged in.",
            }
        ), 401

    if not verify_csrf():
        return jsonify(
            {
                "ok": False,
                "error": "Invalid CSRF token.",
            }
        ), 403

    user_id = current_user_id()

    conn = get_db_connection()

    try:

        with conn.cursor() as cur:

            # Delete AskOra web messages.

            cur.execute(
                """
                DELETE FROM web_messages
                WHERE user_id = %s
                """,
                (user_id,),
            )

            # Delete AskOra chats.

            cur.execute(
                """
                DELETE FROM web_chats
                WHERE user_id = %s
                """,
                (user_id,),
            )

            # Delete AskOra usage records.

            cur.execute(
                """
                DELETE FROM web_usage_events
                WHERE user_id = %s
                """,
                (user_id,),
            )

            # Delete AskOra account.

            cur.execute(
                """
                DELETE FROM web_users
                WHERE id = %s
                """,
                (user_id,),
            )

        conn.commit()

        session.clear()

        return jsonify(
            {
                "ok": True,
            }
        )

    except Exception:

        conn.rollback()

        logger.exception("Account deletion error")

        return jsonify(
            {
                "ok": False,
                "error": "Could not delete your account.",
            }
        ), 500

    finally:
        conn.close()


# =========================================================
# ADMIN
# =========================================================

def admin_required():

    user = get_current_user()

    if not user:
        return False

    return is_admin_user(user)


@app.route("/admin")
def admin():

    if not login_required():
        return redirect("/login")

    if not admin_required():
        return redirect("/")

    return render_template("admin.html")


# =========================================================
# ADMIN STATS
# =========================================================

@app.route("/api/admin/stats")
def admin_stats():

    if not login_required():
        return jsonify(
            {
                "ok": False,
                "error": "Not logged in.",
            }
        ), 401

    if not admin_required():
        return jsonify(
            {
                "ok": False,
                "error": "Not authorized.",
            }
        ), 403

    conn = get_db_connection()

    try:

        with conn.cursor() as cur:

            # Total users.

            cur.execute(
                """
                SELECT COUNT(*)
                FROM web_users
                """
            )

            total_users = cur.fetchone()[0]

            # Active users today.

            cur.execute(
                """
                SELECT COUNT(*)
                FROM web_users
                WHERE last_seen >= CURRENT_DATE
                """
            )

            active_users_today = cur.fetchone()[0]

            # New users today.

            cur.execute(
                """
                SELECT COUNT(*)
                FROM web_users
                WHERE first_seen >= CURRENT_DATE
                """
            )

            new_users_today = cur.fetchone()[0]

            # Total chats.

            cur.execute(
                """
                SELECT COUNT(*)
                FROM web_chats
                """
            )

            total_chats = cur.fetchone()[0]

            # Total questions.

            cur.execute(
                """
                SELECT COUNT(*)
                FROM web_usage_events
                WHERE event_type = 'question'
                """
            )

            total_questions = cur.fetchone()[0]

            # Voice requests.

            cur.execute(
                """
                SELECT COUNT(*)
                FROM web_usage_events
                WHERE event_type = 'voice'
                """
            )

            voice_requests = cur.fetchone()[0]

        return jsonify(
            {
                "ok": True,

                "stats": {
                    "total_users": total_users,
                    "active_users_today": active_users_today,
                    "total_chats": total_chats,
                    "total_questions": total_questions,
                    "voice_requests": voice_requests,
                    "new_users_today": new_users_today,
                },

                # Also return direct fields so the
                # frontend can use either structure.

                "total_users": total_users,
                "active_users_today": active_users_today,
                "total_chats": total_chats,
                "total_questions": total_questions,
                "voice_requests": voice_requests,
                "new_users_today": new_users_today,
            }
        )

    except Exception:

        logger.exception("Admin stats error")

        return jsonify(
            {
                "ok": False,
                "error": "Could not load admin statistics.",
            }
        ), 500

    finally:
        conn.close()


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route("/health")
def health():

    try:

        conn = get_db_connection()

        try:

            with conn.cursor() as cur:
                cur.execute("SELECT 1")

        finally:
            conn.close()

        return jsonify(
            {
                "ok": True,
                "status": "healthy",
            }
        )

    except Exception:

        logger.exception("Health check failed")

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

    if request.path.startswith("/api/"):

        return jsonify(
            {
                "ok": False,
                "error": "Not found.",
            }
        ), 404

    return "Page not found.", 404


@app.errorhandler(500)
def server_error(error):

    logger.exception(
        "Internal server error: %s",
        error,
    )

    if request.path.startswith("/api/"):

        return jsonify(
            {
                "ok": False,
                "error": "Internal server error.",
            }
        ), 500

    return "Internal server error.", 500


# =========================================================
# STARTUP
# =========================================================

try:
    init_database()
    logger.info("Database initialized successfully.")

except Exception:
    logger.exception(
        "Database initialization failed."
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
