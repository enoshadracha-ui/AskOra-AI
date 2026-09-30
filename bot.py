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


# ============================================================
# APP CONFIG
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ["SESSION_SECRET"]

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("COOKIE_SECURE", "false").lower() == "true"
)

DATABASE_URL = os.environ["DATABASE_URL"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").strip().lower()

TEXT_MODEL = "openai/gpt-oss-120b"
VOICE_MODEL = "whisper-large-v3-turbo"

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
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
    )


def init_database():
    """
    Safe database initialization.

    Existing data is preserved.
    Existing Telegram tables are untouched.
    """

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
                    username VARCHAR(100) UNIQUE NOT NULL,
                    email VARCHAR(255) NOT NULL,
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
                    user_id BIGINT NOT NULL
                        REFERENCES web_users(id)
                        ON DELETE CASCADE,
                    title VARCHAR(255) NOT NULL DEFAULT 'New chat',
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
                    chat_id BIGINT
                        REFERENCES web_chats(id)
                        ON DELETE CASCADE,
                    user_id BIGINT
                        REFERENCES web_users(id)
                        ON DELETE CASCADE,
                    role VARCHAR(20) NOT NULL,
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
                    user_id BIGINT
                        REFERENCES web_users(id)
                        ON DELETE SET NULL,
                    event_type VARCHAR(100) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            # ------------------------------------------------
            # OLD PASSWORD RESET TABLE
            # ------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS web_password_resets (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT,
                    token TEXT,
                    expires_at TIMESTAMPTZ,
                    used BOOLEAN DEFAULT FALSE
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

            # ------------------------------------------------
            # INDEXES
            # ------------------------------------------------

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_web_chats_user_id
                ON web_chats(user_id)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_web_chats_updated_at
                ON web_chats(updated_at)
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

            # ------------------------------------------------
            # MIGRATE OLD WEB MESSAGES
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

                old_user_id = row["user_id"]

                cur.execute(
                    """
                    SELECT id
                    FROM web_chats
                    WHERE user_id = %s
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (old_user_id,),
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
                            old_user_id,
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
                        old_user_id,
                    ),
                )

        conn.commit()

    except Exception:
        conn.rollback()
        logger.exception("Database initialization failed")
        raise

    finally:
        conn.close()


# ============================================================
# AUTH HELPERS
# ============================================================

def current_user_id():
    return session.get("user_id")


def get_current_user():
    user_id = current_user_id()

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


def is_admin_user(user=None):

    if user is None:
        user = get_current_user()

    if not user:
        return False

    if not ADMIN_USERNAME:
        return False

    return (
        str(user["username"]).strip().lower()
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


# ============================================================
# CSRF
# ============================================================

def get_csrf_token():

    token = session.get("csrf_token")

    if not token:

        import secrets

        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token

    return token


def verify_csrf():

    expected = session.get("csrf_token")

    if not expected:
        return False

    supplied = request.headers.get("X-CSRF-Token")

    if not supplied:

        if request.is_json:
            body = request.get_json(silent=True) or {}
            supplied = body.get("csrf_token")
        else:
            supplied = request.form.get("csrf_token")

    return bool(
        supplied
        and supplied == expected
    )


# ============================================================
# LOGIN REQUIRED
# ============================================================

def login_required_response():

    return jsonify(
        {
            "ok": False,
            "authenticated": False,
            "logged_in": False,
            "error": "Authentication required.",
        }
    ), 401


# ============================================================
# BASIC ROUTES
# ============================================================

@app.route("/")
def index():

    if not current_user_id():
        return redirect(url_for("login"))

    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():

    if current_user_id():
        return redirect(url_for("index"))

    if request.method == "GET":
        return render_template("login.html")

    login_value = (
        request.form.get("login")
        or request.form.get("username")
        or request.form.get("email")
        or ""
    ).strip()

    password = request.form.get("password", "")

    if not login_value or not password:

        return render_template(
            "login.html",
            error="Please enter your username/email and password.",
        )

    conn = get_db()

    try:
        with conn.cursor() as cur:

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
                (
                    login_value,
                    login_value,
                ),
            )

            user = cur.fetchone()

    finally:
        conn.close()

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

    session.clear()

    session["user_id"] = user["id"]
    session["username"] = user["username"]

    get_csrf_token()

    touch_user(user["id"])

    return redirect(url_for("index"))


# ============================================================
# REGISTER
# ============================================================

@app.route("/register", methods=["GET", "POST"])
def register():

    if current_user_id():
        return redirect(url_for("index"))

    if request.method == "GET":
        return render_template("register.html")

    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not username or not email or not password:

        return render_template(
            "register.html",
            error="Please fill in all fields.",
        )

    if len(username) < 3:

        return render_template(
            "register.html",
            error="Username must be at least 3 characters.",
        )

    if len(password) < 6:

        return render_template(
            "register.html",
            error="Password must be at least 6 characters.",
        )

    if not re.match(
        r"^[A-Za-z0-9_.-]+$",
        username,
    ):

        return render_template(
            "register.html",
            error="Username can only contain letters, numbers, dots, underscores and hyphens.",
        )

    if not re.match(
        r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
        email,
    ):

        return render_template(
            "register.html",
            error="Please enter a valid email address.",
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

                conn.rollback()

                return render_template(
                    "register.html",
                    error="Username or email is already registered.",
                )

            password_hash = generate_password_hash(
                password
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
                    (
                        %s,
                        %s,
                        %s
                    )
                RETURNING id, username, email
                """,
                (
                    username,
                    email,
                    password_hash,
                ),
            )

            user = cur.fetchone()

        conn.commit()

    except Exception:

        conn.rollback()

        logger.exception("Registration error")

        return render_template(
            "register.html",
            error="Something went wrong while creating your account.",
        )

    finally:
        conn.close()

    session.clear()

    session["user_id"] = user["id"]
    session["username"] = user["username"]

    get_csrf_token()

    return redirect(url_for("index"))


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("login"))


# ============================================================
# CURRENT USER
# ============================================================

@app.route("/api/me")
def api_me():

    user = get_current_user()

    if not user:

        return jsonify(
            {
                "ok": False,
                "authenticated": False,
                "logged_in": False,
            }
        )

    touch_user(user["id"])

    admin = is_admin_user(user)

    return jsonify(
        {
            "ok": True,
            "authenticated": True,
            "logged_in": True,

            "user": {
                "id": user["id"],
                "username": user["username"],
                "email": user["email"],
                "is_admin": admin,
            },

            "username": user["username"],
            "email": user["email"],
            "is_admin": admin,

            "csrf_token": get_csrf_token(),
        }
    )


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


def get_chat_for_user(
    user_id,
    chat_id,
):

    conn = get_db()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id,
                    user_id,
                    title,
                    created_at,
                    updated_at
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

            return cur.fetchone()

    finally:
        conn.close()


def make_chat_title(message):

    title = re.sub(
        r"\s+",
        " ",
        message,
    ).strip()

    if len(title) > 60:

        title = (
            title[:60].rstrip()
            + "..."
        )

    return title or "New chat"


def get_chat_history(
    user_id,
    chat_id,
    limit=20,
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
                WHERE chat_id = %s
                  AND user_id = %s
                ORDER BY created_at DESC, id DESC
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

            return rows

    finally:
        conn.close()


# ============================================================
# CHATS - LIST
# ============================================================

@app.route("/api/chats", methods=["GET"])
def api_chats():

    user = get_current_user()

    if not user:
        return login_required_response()

    conn = get_db()

    try:
        with conn.cursor() as cur:

            # IMPORTANT:
            # Only return chats that actually contain messages.
            # Empty "New chat" records are not shown in History.

            cur.execute(
                """
                SELECT
                    c.id,
                    c.title,
                    c.created_at,
                    c.updated_at
                FROM web_chats c
                WHERE c.user_id = %s
                  AND EXISTS (
                      SELECT 1
                      FROM web_messages m
                      WHERE m.chat_id = c.id
                        AND m.user_id = c.user_id
                  )
                ORDER BY c.updated_at DESC, c.id DESC
                """,
                (user["id"],),
            )

            chats = cur.fetchall()

    finally:
        conn.close()

    return jsonify(
        {
            "ok": True,
            "chats": chats,
        }
    )


# ============================================================
# CREATE CHAT
# ============================================================

@app.route("/api/chats", methods=["POST"])
def api_create_chat():

    user = get_current_user()

    if not user:
        return login_required_response()

    if not verify_csrf():

        return jsonify(
            {
                "ok": False,
                "error": "Invalid CSRF token.",
            }
        ), 403

    body = request.get_json(silent=True) or {}

    title = (
        body.get("title")
        or "New chat"
    ).strip()

    if not title:
        title = "New chat"

    title = title[:255]

    try:

        chat = create_chat_for_user(
            user["id"],
            title,
        )

    except Exception:

        logger.exception(
            "Chat creation failed"
        )

        return jsonify(
            {
                "ok": False,
                "error": "Could not create chat.",
            }
        ), 500

    return jsonify(
        {
            "ok": True,
            "chat": chat,
        }
    )


# ============================================================
# CHAT MESSAGES
# ============================================================

@app.route(
    "/api/chats/<int:chat_id>/messages",
    methods=["GET"],
)
def api_chat_messages(chat_id):

    user = get_current_user()

    if not user:
        return login_required_response()

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
                    chat_id,
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

    chat = get_chat_for_user(
        user["id"],
        chat_id,
    )

    return jsonify(
        {
            "ok": True,
            "chat": chat,
            "messages": messages,
        }
    )


# ============================================================
# DELETE CHAT
# ============================================================

@app.route(
    "/api/chats/<int:chat_id>",
    methods=["DELETE"],
)
def api_delete_chat(chat_id):

    user = get_current_user()

    if not user:
        return login_required_response()

    if not verify_csrf():

        return jsonify(
            {
                "ok": False,
                "error": "Invalid CSRF token.",
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

    except Exception:

        conn.rollback()

        logger.exception(
            "Chat deletion failed"
        )

        return jsonify(
            {
                "ok": False,
                "error": "Could not delete chat.",
            }
        ), 500

    finally:
        conn.close()

    if not deleted:

        return jsonify(
            {
                "ok": False,
                "error": "Chat not found.",
            }
        ), 404

    return jsonify(
        {
            "ok": True,
            "deleted_chat_id": chat_id,
        }
    )


# ============================================================
# AI
# ============================================================

SYSTEM_PROMPT = """
You are AskOra, a helpful, intelligent and friendly AI assistant.

Your job is to answer the user's questions clearly and accurately.

Rules:
- Be helpful and natural.
- Give direct answers.
- Explain things simply when appropriate.
- Use Markdown when it improves readability.
- Do not unnecessarily repeat the user's question.
- If you are unsure about something, say so rather than inventing facts.
- Keep answers reasonably concise unless the user asks for detail.
"""


def generate_ai_response(messages):

    response = groq_client.chat.completions.create(
        model=TEXT_MODEL,
        messages=messages,
        temperature=0.7,
        max_tokens=2000,
    )

    return (
        response.choices[0]
        .message
        .content
        .strip()
    )


# ============================================================
# SAVE MESSAGE
# ============================================================

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
                    (
                        %s,
                        %s,
                        %s,
                        %s
                    )
                RETURNING
                    id,
                    chat_id,
                    user_id,
                    role,
                    content,
                    created_at
                """,
                (
                    chat_id,
                    user_id,
                    role,
                    content,
                ),
            )

            message = cur.fetchone()

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

        return message

    except Exception:

        conn.rollback()

        raise

    finally:
        conn.close()


# ============================================================
# UPDATE CHAT TITLE
# ============================================================

def update_chat_title_if_new(
    user_id,
    chat_id,
    first_message,
):

    title = make_chat_title(
        first_message
    )

    conn = get_db()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE web_chats
                SET
                    title = %s,
                    updated_at = NOW()
                WHERE id = %s
                  AND user_id = %s
                  AND (
                      title IS NULL
                      OR title = ''
                      OR title = 'New chat'
                  )
                """,
                (
                    title,
                    chat_id,
                    user_id,
                ),
            )

        conn.commit()

    except Exception:

        conn.rollback()

        raise

    finally:
        conn.close()

    return get_chat_for_user(
        user_id,
        chat_id,
    )


# ============================================================
# CHAT WITH AI
# ============================================================

@app.route("/api/chat", methods=["POST"])
def api_chat():

    user = get_current_user()

    if not user:
        return login_required_response()

    if not verify_csrf():

        return jsonify(
            {
                "ok": False,
                "error": "Invalid CSRF token.",
            }
        ), 403

    body = request.get_json(silent=True) or {}

    message = (
        body.get("message")
        or body.get("text")
        or ""
    ).strip()

    chat_id = body.get("chat_id")

    if not message:

        return jsonify(
            {
                "ok": False,
                "error": "Message cannot be empty.",
            }
        ), 400

    if len(message) > 20000:

        return jsonify(
            {
                "ok": False,
                "error": "Message is too long.",
            }
        ), 400

    # --------------------------------------------------------
    # GET OR CREATE CHAT
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

            return jsonify(
                {
                    "ok": False,
                    "error": "Invalid chat ID.",
                }
            ), 400

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

    # --------------------------------------------------------
    # CHECK WHETHER THIS IS THE FIRST MESSAGE
    # --------------------------------------------------------

    conn = get_db()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_messages
                WHERE chat_id = %s
                  AND user_id = %s
                """,
                (
                    chat_id,
                    user["id"],
                ),
            )

            message_count = int(
                cur.fetchone()["count"]
            )

    finally:
        conn.close()

    is_first_message = message_count == 0

    # --------------------------------------------------------
    # GET AI HISTORY BEFORE SAVING NEW MESSAGE
    # --------------------------------------------------------

    history = get_chat_history(
        user["id"],
        chat_id,
        limit=20,
    )

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        }
    ]

    for item in history:

        if item["role"] not in (
            "user",
            "assistant",
        ):
            continue

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

    # --------------------------------------------------------
    # SAVE USER MESSAGE FIRST
    # --------------------------------------------------------

    try:

        save_message(
            user["id"],
            chat_id,
            "user",
            message,
        )

        # VERY IMPORTANT:
        # The first message immediately becomes the title.
        # This happens before the AI response is generated.

        if is_first_message:

            update_chat_title_if_new(
                user["id"],
                chat_id,
                message,
            )

    except Exception:

        logger.exception(
            "Could not save user message"
        )

        return jsonify(
            {
                "ok": False,
                "error": "Could not save your message.",
            }
        ), 500

    # --------------------------------------------------------
    # GENERATE AI ANSWER
    # --------------------------------------------------------

    try:

        answer = generate_ai_response(
            messages
        )

    except Exception:

        logger.exception(
            "Groq error"
        )

        # Even if AI fails, the conversation remains
        # saved with the correct title.

        chat = get_chat_for_user(
            user["id"],
            chat_id,
        )

        return jsonify(
            {
                "ok": False,
                "error": "Something went wrong while generating the response.",
                "chat_id": chat_id,
                "chat": chat,
            }
        ), 500

    if not answer:

        answer = (
            "Sorry, I couldn't generate a response."
        )

    # --------------------------------------------------------
    # SAVE ASSISTANT MESSAGE
    # --------------------------------------------------------

    try:

        save_message(
            user["id"],
            chat_id,
            "assistant",
            answer,
        )

    except Exception:

        logger.exception(
            "Could not save assistant message"
        )

        return jsonify(
            {
                "ok": False,
                "error": "The response was generated but could not be saved.",
            }
        ), 500

    # --------------------------------------------------------
    # USAGE EVENT
    # --------------------------------------------------------

    try:

        conn = get_db()

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
                        %s
                    )
                """,
                (
                    user["id"],
                    "question",
                ),
            )

        conn.commit()

    except Exception:

        logger.exception(
            "Could not record question event"
        )

    finally:

        try:
            conn.close()
        except Exception:
            pass

    # --------------------------------------------------------
    # RETURN THE ACTUAL SAVED CHAT
    # --------------------------------------------------------

    chat = get_chat_for_user(
        user["id"],
        chat_id,
    )

    return jsonify(
        {
            "ok": True,

            "chat_id": chat_id,

            "chat": chat,

            "answer": answer,
            "response": answer,
        }
    )


# ============================================================
# VOICE
# ============================================================

@app.route("/api/voice", methods=["POST"])
def api_voice():

    user = get_current_user()

    if not user:
        return login_required_response()

    if not verify_csrf():

        return jsonify(
            {
                "ok": False,
                "error": "Invalid CSRF token.",
            }
        ), 403

    audio = (
        request.files.get("audio")
        or request.files.get("file")
        or request.files.get("voice")
    )

    if not audio:

        return jsonify(
            {
                "ok": False,
                "error": "No audio file was received.",
            }
        ), 400

    # --------------------------------------------------------
    # Determine chat
    # --------------------------------------------------------

    chat_id = request.form.get("chat_id")

    if chat_id:

        try:
            chat_id = int(chat_id)

        except (TypeError, ValueError):

            return jsonify(
                {
                    "ok": False,
                    "error": "Invalid chat ID.",
                }
            ), 400

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

    else:

        chat = create_chat_for_user(
            user["id"],
            "New chat",
        )

        chat_id = chat["id"]

    try:

        audio_bytes = audio.read()

        if not audio_bytes:

            return jsonify(
                {
                    "ok": False,
                    "error": "The audio recording is empty.",
                }
            ), 400

        filename = (
            audio.filename
            or "voice.webm"
        )

        transcription = (
            groq_client.audio.transcriptions.create(
                file=(
                    filename,
                    audio_bytes,
                ),
                model=VOICE_MODEL,
            )
        )

        text = (
            getattr(
                transcription,
                "text",
                "",
            )
            or ""
        ).strip()

        if not text:

            return jsonify(
                {
                    "ok": False,
                    "error": "I couldn't understand the recording.",
                }
            ), 400

        # ----------------------------------------------------
        # Use normal chat system for the voice question.
        # ----------------------------------------------------

        history = get_chat_history(
            user["id"],
            chat_id,
            limit=20,
        )

        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            }
        ]

        for item in history:

            if item["role"] not in (
                "user",
                "assistant",
            ):
                continue

            messages.append(
                {
                    "role": item["role"],
                    "content": item["content"],
                }
            )

        messages.append(
            {
                "role": "user",
                "content": text,
            }
        )

        # ----------------------------------------------------
        # Determine if first message
        # ----------------------------------------------------

        conn = get_db()

        try:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM web_messages
                    WHERE chat_id = %s
                      AND user_id = %s
                    """,
                    (
                        chat_id,
                        user["id"],
                    ),
                )

                message_count = int(
                    cur.fetchone()["count"]
                )

        finally:
            conn.close()

        is_first_message = message_count == 0

        # ----------------------------------------------------
        # Save voice transcript as user message
        # ----------------------------------------------------

        save_message(
            user["id"],
            chat_id,
            "user",
            text,
        )

        if is_first_message:

            update_chat_title_if_new(
                user["id"],
                chat_id,
                text,
            )

        # ----------------------------------------------------
        # Generate answer
        # ----------------------------------------------------

        answer = generate_ai_response(
            messages
        )

        if not answer:

            answer = (
                "Sorry, I couldn't generate a response."
            )

        # ----------------------------------------------------
        # Save assistant answer
        # ----------------------------------------------------

        save_message(
            user["id"],
            chat_id,
            "assistant",
            answer,
        )

        # ----------------------------------------------------
        # Usage events
        # ----------------------------------------------------

        conn = get_db()

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
                            %s
                        )
                    """,
                    (
                        user["id"],
                        "voice",
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
                            %s
                        )
                    """,
                    (
                        user["id"],
                        "question",
                    ),
                )

            conn.commit()

        finally:
            conn.close()

        chat = get_chat_for_user(
            user["id"],
            chat_id,
        )

        return jsonify(
            {
                "ok": True,
                "chat_id": chat_id,
                "chat": chat,
                "text": text,
                "transcript": text,
                "answer": answer,
                "response": answer,
            }
        )

    except Exception:

        logger.exception(
            "Voice processing failed"
        )

        return jsonify(
            {
                "ok": False,
                "error": "Something went wrong while processing your voice recording.",
            }
        ), 500


# ============================================================
# RESET CURRENT CHAT
# ============================================================

@app.route("/api/reset", methods=["POST"])
def api_reset():

    user = get_current_user()

    if not user:
        return login_required_response()

    if not verify_csrf():

        return jsonify(
            {
                "ok": False,
                "error": "Invalid CSRF token.",
            }
        ), 403

    body = request.get_json(silent=True) or {}

    chat_id = body.get("chat_id")

    if not chat_id:

        return jsonify(
            {
                "ok": False,
                "error": "Chat ID is required.",
            }
        ), 400

    try:
        chat_id = int(chat_id)

    except (TypeError, ValueError):

        return jsonify(
            {
                "ok": False,
                "error": "Invalid chat ID.",
            }
        ), 400

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

    except Exception:

        conn.rollback()

        logger.exception(
            "Could not reset chat"
        )

        return jsonify(
            {
                "ok": False,
                "error": "Could not reset chat.",
            }
        ), 500

    finally:
        conn.close()

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
def api_delete_account():

    user = get_current_user()

    if not user:
        return login_required_response()

    if not verify_csrf():

        return jsonify(
            {
                "ok": False,
                "error": "Invalid CSRF token.",
            }
        ), 403

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                DELETE FROM web_users
                WHERE id = %s
                """,
                (user["id"],),
            )

        conn.commit()

    except Exception:

        conn.rollback()

        logger.exception(
            "Account deletion failed"
        )

        return jsonify(
            {
                "ok": False,
                "error": "Could not delete account.",
            }
        ), 500

    finally:
        conn.close()

    session.clear()

    return jsonify(
        {
            "ok": True,
        }
    )


# ============================================================
# ADMIN
# ============================================================

@app.route("/admin")
def admin():

    user = get_current_user()

    if not user:
        return redirect(url_for("login"))

    if not is_admin_user(user):
        return "Access denied", 403

    return render_template(
        "admin.html",
    )


@app.route("/api/admin/stats")
def api_admin_stats():

    user = get_current_user()

    if not user:
        return login_required_response()

    if not is_admin_user(user):

        return jsonify(
            {
                "ok": False,
                "error": "Access denied.",
            }
        ), 403

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_users
                """
            )

            total_users = cur.fetchone()["count"]

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

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_chats c
                WHERE EXISTS (
                    SELECT 1
                    FROM web_messages m
                    WHERE m.chat_id = c.id
                )
                """
            )

            total_chats = cur.fetchone()["count"]

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_usage_events
                WHERE event_type = 'question'
                """
            )

            total_questions = (
                cur.fetchone()["count"]
            )

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
                "active_users_today": active_users_today,
                "total_chats": total_chats,
                "total_questions": total_questions,
                "voice_requests": voice_requests,
                "new_users_today": new_users_today,
            },

            "total_users": total_users,
            "active_users_today": active_users_today,
            "total_chats": total_chats,
            "total_questions": total_questions,
            "voice_requests": voice_requests,
            "new_users_today": new_users_today,
        }
    )


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    try:

        conn = get_db()

        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()

        conn.close()

        return jsonify(
            {
                "ok": True,
                "status": "healthy",
            }
        )

    except Exception:

        logger.exception(
            "Health check database error"
        )

        return jsonify(
            {
                "ok": False,
                "status": "unhealthy",
            }
        ), 500


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

    return "Page not found", 404


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
                "error": "Internal server error.",
            }
        ), 500

    return "Internal server error", 500


# ============================================================
# STARTUP
# ============================================================

try:

    init_database()

    logger.info(
        "Database initialized successfully."
    )

except Exception:

    logger.exception(
        "Database initialization failed during startup."
    )


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            5000,
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
