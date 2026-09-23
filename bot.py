import os
import re
import base64
import secrets
import logging
from functools import wraps
from io import BytesIO

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
VISION_MODEL = "qwen/qwen3.8-27b"
VOICE_MODEL = "whisper-large-v3-turbo"

MAX_MESSAGE_LENGTH = 12000
MAX_IMAGE_SIZE = 20 * 1024 * 1024
MAX_VOICE_SIZE = 15 * 1024 * 1024


ALLOWED_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}


# ============================================================
# APP
# ============================================================

app = Flask(__name__)

app.secret_key = SESSION_SECRET

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=MAX_IMAGE_SIZE,
)


groq_client = Groq(
    api_key=GROQ_API_KEY
)


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
                    password_hash TEXT NOT NULL,
                    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

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

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_web_usage_events_created
                ON web_usage_events(created_at)
                """
            )

            # ------------------------------------------------
            # MIGRATE OLD MESSAGES
            # ------------------------------------------------

            cur.execute(
                """
                SELECT DISTINCT user_id
                FROM web_messages
                WHERE chat_id IS NULL
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

                chat = cur.fetchone()

                if chat:

                    chat_id = chat["id"]

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

        logger.info(
            "AskOra database initialized."
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


def csrf_required():

    supplied = request.headers.get(
        "X-CSRF-Token"
    )

    if not supplied:

        supplied = request.form.get(
            "csrf_token"
        )

    expected = session.get(
        "csrf_token"
    )

    return (
        bool(expected)
        and bool(supplied)
        and secrets.compare_digest(
            supplied,
            expected,
        )
    )


# ============================================================
# AUTH
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


def is_admin_user(user):

    if not user:
        return False

    return (
        user["username"].lower()
        == ADMIN_USERNAME
    )


def login_required(func):

    @wraps(func)
    def wrapper(*args, **kwargs):

        if not session.get("user_id"):

            if request.path.startswith("/api/"):

                return jsonify(
                    {
                        "error":
                        "Authentication required."
                    }
                ), 401

            return redirect(
                url_for("login")
            )

        return func(*args, **kwargs)

    return wrapper


def admin_required(func):

    @wraps(func)
    def wrapper(*args, **kwargs):

        user = current_user()

        if not user:

            if request.path.startswith("/api/"):

                return jsonify(
                    {
                        "error":
                        "Authentication required."
                    }
                ), 401

            return redirect(
                url_for("login")
            )

        if not is_admin_user(user):

            if request.path.startswith("/api/"):

                return jsonify(
                    {
                        "error":
                        "Admin access required."
                    }
                ), 403

            return "Access denied", 403

        return func(*args, **kwargs)

    return wrapper


# ============================================================
# VALIDATION
# ============================================================

USERNAME_RE = re.compile(
    r"^[a-z0-9_]{3,32}$"
)

EMAIL_RE = re.compile(
    r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
)


def valid_username(username):

    return bool(
        USERNAME_RE.fullmatch(
            username.lower()
        )
    )


def valid_email(email):

    return bool(
        EMAIL_RE.fullmatch(email)
    )


# ============================================================
# HOME
# ============================================================

@app.route("/")
def index():

    user = current_user()

    if not user:

        return redirect(
            url_for("login")
        )

    return render_template(
        "index.html",
        user=user,
        csrf_token=get_csrf_token(),
        is_admin=is_admin_user(user),
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
            url_for("index")
        )

    if request.method == "GET":

        return render_template(
            "register.html",
            csrf_token=get_csrf_token(),
        )

    if not csrf_required():

        return jsonify(
            {
                "error":
                "Invalid security token."
            }
        ), 403

    username = (
        request.form.get(
            "username",
            "",
        )
        .strip()
        .lower()
    )

    email = (
        request.form.get(
            "email",
            "",
        )
        .strip()
        .lower()
    )

    password = request.form.get(
        "password",
        "",
    )

    if not valid_username(username):

        return jsonify(
            {
                "error":
                "Username must be 3–32 characters and use only letters, numbers and underscores."
            }
        ), 400

    if not valid_email(email):

        return jsonify(
            {
                "error":
                "Enter a valid email address."
            }
        ), 400

    if len(password) < 8:

        return jsonify(
            {
                "error":
                "Password must be at least 8 characters."
            }
        ), 400

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id
                FROM web_users
                WHERE LOWER(username) = LOWER(%s)
                """,
                (username,),
            )

            if cur.fetchone():

                return jsonify(
                    {
                        "error":
                        "That username is already taken."
                    }
                ), 409

            cur.execute(
                """
                SELECT id
                FROM web_users
                WHERE LOWER(email) = LOWER(%s)
                """,
                (email,),
            )

            if cur.fetchone():

                return jsonify(
                    {
                        "error":
                        "That email is already registered."
                    }
                ), 409

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
                VALUES (%s, %s, %s)
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

        session.clear()

        session["user_id"] = user["id"]

        session["csrf_token"] = (
            secrets.token_urlsafe(32)
        )

        return redirect(
            url_for("index")
        )

    except Exception:

        conn.rollback()

        logger.exception(
            "Registration error."
        )

        return jsonify(
            {
                "error":
                "Could not create the account."
            }
        ), 500

    finally:

        conn.close()


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
            url_for("index")
        )

    if request.method == "GET":

        return render_template(
            "login.html",
            csrf_token=get_csrf_token(),
        )

    if not csrf_required():

        return jsonify(
            {
                "error":
                "Invalid security token."
            }
        ), 403

    login_value = (
        request.form.get(
            "login",
            "",
        )
        .strip()
    )

    password = request.form.get(
        "password",
        "",
    )

    if not login_value or not password:

        return jsonify(
            {
                "error":
                "Enter your username/email and password."
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
                    login_value,
                    login_value,
                ),
            )

            user = cur.fetchone()

            if not user:

                return jsonify(
                    {
                        "error":
                        "Invalid username/email or password."
                    }
                ), 401

            if not check_password_hash(
                user["password_hash"],
                password,
            ):

                return jsonify(
                    {
                        "error":
                        "Invalid username/email or password."
                    }
                ), 401

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

        session["csrf_token"] = (
            secrets.token_urlsafe(32)
        )

        return redirect(
            url_for("index")
        )

    finally:

        conn.close()


# ============================================================
# LOGOUT
# ============================================================

@app.route(
    "/logout",
    methods=["POST"],
)
@login_required
def logout():

    if not csrf_required():

        return jsonify(
            {
                "error":
                "Invalid security token."
            }
        ), 403

    session.clear()

    return redirect(
        url_for("login")
    )


# ============================================================
# USER API
# ============================================================

@app.route("/api/me")
@login_required
def api_me():

    user = current_user()

    return jsonify(
        {
            "user": {
                "id": user["id"],
                "username": user["username"],
                "email": user["email"],
                "is_admin": is_admin_user(user),
            }
        }
    )


# ============================================================
# CHATS
# ============================================================

@app.route(
    "/api/chats",
    methods=["GET"],
)
@login_required
def get_chats():

    user = current_user()

    conn = get_db()

    try:

        with conn.cursor() as cur:

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
                    c.id,
                    c.title,
                    c.created_at,
                    c.updated_at
                ORDER BY c.updated_at DESC
                """,
                (user["id"],),
            )

            chats = cur.fetchall()

        return jsonify(
            {
                "chats": chats
            }
        )

    finally:

        conn.close()


@app.route(
    "/api/chats",
    methods=["POST"],
)
@login_required
def create_chat():

    if not csrf_required():

        return jsonify(
            {
                "error":
                "Invalid security token."
            }
        ), 403

    user = current_user()

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
                    user["id"],
                    "New chat",
                ),
            )

            chat = cur.fetchone()

            conn.commit()

        return jsonify(
            {
                "chat": chat
            }
        )

    finally:

        conn.close()


@app.route(
    "/api/chats/<int:chat_id>/messages",
    methods=["GET"],
)
@login_required
def get_chat_messages(chat_id):

    user = current_user()

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
                        "error":
                        "Chat not found."
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

        return jsonify(
            {
                "chat": chat,
                "messages": messages,
            }
        )

    finally:

        conn.close()


@app.route(
    "/api/chats/<int:chat_id>",
    methods=["DELETE"],
)
@login_required
def delete_chat(chat_id):

    if not csrf_required():

        return jsonify(
            {
                "error":
                "Invalid security token."
            }
        ), 403

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
                DELETE FROM web_chats
                WHERE id = %s
                AND user_id = %s
                RETURNING id
                """,
                (
                    chat_id,
                    user["id"],
                ),
            )

            deleted = cur.fetchone()

            if not deleted:

                conn.rollback()

                return jsonify(
                    {
                        "error":
                        "Chat not found."
                    }
                ), 404

            conn.commit()

        return jsonify(
            {
                "success": True
            }
        )

    finally:

        conn.close()


# ============================================================
# TEXT + IMAGE CHAT
# ============================================================

@app.route(
    "/api/chat",
    methods=["POST"],
)
@login_required
def api_chat():

    if not csrf_required():

        return jsonify(
            {
                "error":
                "Invalid security token."
            }
        ), 403

    user = current_user()

    message = (
        request.form.get(
            "message",
            "",
        )
        .strip()
    )

    chat_id = request.form.get(
        "chat_id"
    )

    image = request.files.get(
        "image"
    )

    if not message and not image:

        return jsonify(
            {
                "error":
                "Enter a message or select an image."
            }
        ), 400

    if len(message) > MAX_MESSAGE_LENGTH:

        return jsonify(
            {
                "error":
                "That message is too long."
            }
        ), 400

    image_bytes = None
    image_mime = None

    if image:

        image_bytes = image.read()

        if not image_bytes:

            return jsonify(
                {
                    "error":
                    "The image is empty."
                }
            ), 400

        if len(image_bytes) > MAX_IMAGE_SIZE:

            return jsonify(
                {
                    "error":
                    "The image is too large. Maximum size is 20 MB."
                }
            ), 400

        image_mime = (
            image.mimetype
            or "image/jpeg"
        )

        if image_mime not in ALLOWED_IMAGE_TYPES:

            return jsonify(
                {
                    "error":
                    "Please upload a JPG, PNG, WEBP or GIF image."
                }
            ), 400

    conn = get_db()

    try:

        with conn.cursor() as cur:

            # ------------------------------------------------
            # Find/create chat
            # ------------------------------------------------

            if chat_id:

                try:

                    chat_id = int(chat_id)

                except (
                    TypeError,
                    ValueError,
                ):

                    return jsonify(
                        {
                            "error":
                            "Invalid chat."
                        }
                    ), 400

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
                            "error":
                            "Chat not found."
                        }
                    ), 404

            else:

                cur.execute(
                    """
                    INSERT INTO web_chats
                    (
                        user_id,
                        title
                    )
                    VALUES (%s, %s)
                    RETURNING id, title
                    """,
                    (
                        user["id"],
                        "New chat",
                    ),
                )

                chat = cur.fetchone()

                chat_id = chat["id"]

            # ------------------------------------------------
            # Save user text
            # ------------------------------------------------

            saved_content = message

            if image:

                if saved_content:

                    saved_content = (
                        "🖼️ Image attached\n\n"
                        + saved_content
                    )

                else:

                    saved_content = (
                        "🖼️ Image attached"
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
                    "user",
                    saved_content,
                ),
            )

            # ------------------------------------------------
            # History
            # ------------------------------------------------

            cur.execute(
                """
                SELECT
                    role,
                    content
                FROM web_messages
                WHERE chat_id = %s
                AND user_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 20
                """,
                (
                    chat_id,
                    user["id"],
                ),
            )

            history_rows = cur.fetchall()

            history_rows.reverse()

            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are AskOra, a helpful AI assistant. "
                        "Answer clearly and naturally. "
                        "Use Markdown when it improves readability. "
                        "For complex questions, organize the answer "
                        "with headings, bullets or numbered steps. "
                        "Do not claim to see an image unless an image "
                        "was actually supplied."
                    ),
                }
            ]

            for row in history_rows:

                messages.append(
                    {
                        "role": row["role"],
                        "content": row["content"],
                    }
                )

            # ------------------------------------------------
            # TEXT ONLY
            # ------------------------------------------------

            if not image:

                response = (
                    groq_client
                    .chat
                    .completions
                    .create(
                        model=TEXT_MODEL,
                        messages=messages,
                        max_completion_tokens=2000,
                    )
                )

            # ------------------------------------------------
            # IMAGE + TEXT
            # ------------------------------------------------

            else:

                encoded = base64.b64encode(
                    image_bytes
                ).decode("utf-8")

                image_url = (
                    f"data:{image_mime};base64,{encoded}"
                )

                # Build a vision request using the
                # conversation context plus the new image.

                vision_messages = []

                for msg in messages[:-1]:

                    vision_messages.append(msg)

                user_prompt = message

                if not user_prompt:

                    user_prompt = (
                        "Analyze this image and explain "
                        "what you can identify in it."
                    )

                vision_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": user_prompt,
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": image_url,
                                },
                            },
                        ],
                    }
                )

                response = (
                    groq_client
                    .chat
                    .completions
                    .create(
                        model=VISION_MODEL,
                        messages=vision_messages,
                        max_completion_tokens=3000,
                    )
                )

            answer = (
                response
                .choices[0]
                .message
                .content
                or ""
            ).strip()

            if not answer:

                answer = (
                    "Sorry, I couldn't generate an answer."
                )

            # ------------------------------------------------
            # Save assistant answer
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
            # Chat title
            # ------------------------------------------------

            cur.execute(
                """
                SELECT title
                FROM web_chats
                WHERE id = %s
                """,
                (chat_id,),
            )

            current_chat = cur.fetchone()

            current_title = (
                current_chat["title"]
                if current_chat
                else "New chat"
            )

            if current_title == "New chat":

                title_source = (
                    message
                    if message
                    else "Image analysis"
                )

                title_source = re.sub(
                    r"\s+",
                    " ",
                    title_source,
                ).strip()

                if len(title_source) > 60:

                    title_source = (
                        title_source[:57]
                        + "..."
                    )

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
                        title_source,
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
            # Usage
            # ------------------------------------------------

            event_type = (
                "image"
                if image
                else "chat"
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
                    event_type,
                ),
            )

            conn.commit()

        return jsonify(
            {
                "answer": answer,
                "chat_id": chat_id,
                "has_image": bool(image),
            }
        )

    except Exception as error:

        conn.rollback()

        logger.exception(
            "Chat/image error: %s",
            error,
        )

        return jsonify(
            {
                "error":
                "Something went wrong while generating the response."
            }
        ), 500

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
def api_voice():

    if not csrf_required():

        return jsonify(
            {
                "error":
                "Invalid security token."
            }
        ), 403

    user = current_user()

    audio = request.files.get(
        "audio"
    )

    chat_id = request.form.get(
        "chat_id"
    )

    if not audio:

        return jsonify(
            {
                "error":
                "No audio was received."
            }
        ), 400

    audio_bytes = audio.read()

    if not audio_bytes:

        return jsonify(
            {
                "error":
                "The audio recording is empty."
            }
        ), 400

    if len(audio_bytes) > MAX_VOICE_SIZE:

        return jsonify(
            {
                "error":
                "The audio file is too large."
            }
        ), 400

    conn = get_db()

    try:

        with conn.cursor() as cur:

            # ------------------------------------------------
            # Find/create chat
            # ------------------------------------------------

            if chat_id:

                try:

                    chat_id = int(chat_id)

                except (
                    TypeError,
                    ValueError,
                ):

                    chat_id = None

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
                        user["id"],
                    ),
                )

                if not cur.fetchone():

                    chat_id = None

            if not chat_id:

                cur.execute(
                    """
                    INSERT INTO web_chats
                    (
                        user_id,
                        title
                    )
                    VALUES (%s, %s)
                    RETURNING id
                    """,
                    (
                        user["id"],
                        "New chat",
                    ),
                )

                chat_id = cur.fetchone()["id"]

            # ------------------------------------------------
            # Whisper
            # ------------------------------------------------

            filename = (
                audio.filename
                or "recording.webm"
            )

            mime_type = (
                audio.mimetype
                or "audio/webm"
            )

            transcription_response = (
                groq_client
                .audio
                .transcriptions
                .create(
                    file=(
                        filename,
                        BytesIO(audio_bytes),
                        mime_type,
                    ),
                    model=VOICE_MODEL,
                    response_format="text",
                )
            )

            if isinstance(
                transcription_response,
                str,
            ):

                transcription = (
                    transcription_response
                    .strip()
                )

            else:

                transcription = (
                    getattr(
                        transcription_response,
                        "text",
                        "",
                    )
                    or ""
                ).strip()

            if not transcription:

                return jsonify(
                    {
                        "error":
                        "I couldn't understand the recording."
                    }
                ), 400

            # ------------------------------------------------
            # Save user voice message
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
                    transcription,
                ),
            )

            # ------------------------------------------------
            # History
            # ------------------------------------------------

            cur.execute(
                """
                SELECT
                    role,
                    content
                FROM web_messages
                WHERE chat_id = %s
                AND user_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 20
                """,
                (
                    chat_id,
                    user["id"],
                ),
            )

            history_rows = cur.fetchall()

            history_rows.reverse()

            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are AskOra, a helpful AI assistant. "
                        "Give clear, accurate and natural answers."
                    ),
                }
            ]

            for row in history_rows:

                messages.append(
                    {
                        "role": row["role"],
                        "content": row["content"],
                    }
                )

            # ------------------------------------------------
            # AI
            # ------------------------------------------------

            response = (
                groq_client
                .chat
                .completions
                .create(
                    model=TEXT_MODEL,
                    messages=messages,
                    max_completion_tokens=2000,
                )
            )

            answer = (
                response
                .choices[0]
                .message
                .content
                or ""
            ).strip()

            if not answer:

                answer = (
                    "Sorry, I couldn't generate an answer."
                )

            # ------------------------------------------------
            # Save answer
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

            conn.commit()

        return jsonify(
            {
                "transcription": transcription,
                "answer": answer,
                "chat_id": chat_id,
            }
        )

    except Exception as error:

        conn.rollback()

        logger.exception(
            "Voice error: %s",
            error,
        )

        return jsonify(
            {
                "error":
                "Something went wrong while processing your voice message."
            }
        ), 500

    finally:

        conn.close()


# ============================================================
# ADMIN
# ============================================================

@app.route("/admin")
@admin_required
def admin_page():

    user = current_user()

    return render_template(
        "admin.html",
        user=user,
        csrf_token=get_csrf_token(),
    )


@app.route(
    "/api/admin/stats"
)
@admin_required
def admin_stats():

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
                WHERE first_seen >= CURRENT_DATE
                """
            )

            users_today = cur.fetchone()["count"]

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_users
                WHERE last_seen >= CURRENT_DATE
                """
            )

            active_today = cur.fetchone()["count"]

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_chats
                """
            )

            total_chats = cur.fetchone()["count"]

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM web_messages
                """
            )

            total_messages = cur.fetchone()["count"]

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

            cur.execute(
                """
                SELECT
                    u.id,
                    u.username,
                    u.email,
                    u.first_seen,
                    u.last_seen,
                    COUNT(DISTINCT c.id)
                        AS chat_count,
                    COUNT(DISTINCT m.id)
                        AS message_count
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
                LIMIT 200
                """
            )

            users = cur.fetchall()

        return jsonify(
            {
                "stats": {
                    "total_users": total_users,
                    "users_today": users_today,
                    "active_today": active_today,
                    "total_chats": total_chats,
                    "total_messages": total_messages,
                },
                "events": events,
                "users": users,
            }
        )

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

            with conn.cursor() as cur:

                cur.execute(
                    "SELECT 1"
                )

                cur.fetchone()

        finally:

            conn.close()

        return jsonify(
            {
                "status": "ok",
                "service": "AskOra",
            }
        )

    except Exception as error:

        logger.exception(
            "Health check failed."
        )

        return jsonify(
            {
                "status": "error",
                "service": "AskOra",
            }
        ), 500


# ============================================================
# ERRORS
# ============================================================

@app.errorhandler(413)
def request_too_large(error):

    return jsonify(
        {
            "error":
            "The uploaded file is too large."
        }
    ), 413


@app.errorhandler(404)
def not_found(error):

    if request.path.startswith("/api/"):

        return jsonify(
            {
                "error":
                "Endpoint not found."
            }
        ), 404

    return "Page not found", 404


@app.errorhandler(500)
def internal_error(error):

    logger.exception(
        "Internal server error."
    )

    if request.path.startswith("/api/"):

        return jsonify(
            {
                "error":
                "Internal server error."
            }
        ), 500

    return "Internal server error", 500


# ============================================================
# DATABASE STARTUP
# ============================================================

try:

    init_database()

except Exception:

    logger.exception(
        "AskOra database startup failed."
    )


# ============================================================
# LOCAL RUN
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
    )
