import os
import re
import secrets
import logging
from functools import wraps
from io import BytesIO

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    jsonify,
    abort,
)
from werkzeug.security import generate_password_hash, check_password_hash
from groq import Groq


# ============================================================
# CONFIG
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ["SESSION_SECRET"]

DATABASE_URL = os.environ["DATABASE_URL"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

ADMIN_USERNAME = os.environ.get(
    "ADMIN_USERNAME",
    "admin"
).strip().lower()

TEXT_MODEL = "openai/gpt-oss-120b"
VOICE_MODEL = "whisper-large-v3-turbo"

MAX_AUDIO_BYTES = 15 * 1024 * 1024

groq_client = Groq(api_key=GROQ_API_KEY)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


# ============================================================
# DATABASE
# ============================================================

def get_db():
    return psycopg2.connect(DATABASE_URL)


def init_database():
    conn = get_db()

    try:
        with conn.cursor() as cur:

            # ------------------------------------------------
            # WEB USERS
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS web_users (
                    id BIGSERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                ALTER TABLE web_users
                ADD COLUMN IF NOT EXISTS email TEXT
            """)

            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                idx_web_users_email_lower
                ON web_users (LOWER(email))
                WHERE email IS NOT NULL
            """)

            # ------------------------------------------------
            # WEB CHATS
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS web_chats (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL
                        REFERENCES web_users(id)
                        ON DELETE CASCADE,
                    title TEXT NOT NULL DEFAULT 'New chat',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_web_chats_user_updated
                ON web_chats(user_id, updated_at DESC)
            """)

            # ------------------------------------------------
            # WEB MESSAGES
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS web_messages (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL
                        REFERENCES web_users(id)
                        ON DELETE CASCADE,
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
                CREATE INDEX IF NOT EXISTS
                idx_web_messages_chat_created
                ON web_messages(chat_id, created_at)
            """)

            # ------------------------------------------------
            # USAGE EVENTS
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS web_usage_events (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT
                        REFERENCES web_users(id)
                        ON DELETE SET NULL,
                    event_type TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_web_usage_events_created
                ON web_usage_events(created_at)
            """)

            # ------------------------------------------------
            # MOVE OLD WEB MESSAGES INTO A CHAT
            # ------------------------------------------------

            cur.execute("""
                SELECT DISTINCT user_id
                FROM web_messages
                WHERE chat_id IS NULL
            """)

            old_users = cur.fetchall()

            for row in old_users:
                user_id = row[0]

                cur.execute("""
                    SELECT id
                    FROM web_chats
                    WHERE user_id = %s
                    ORDER BY created_at ASC
                    LIMIT 1
                """, (user_id,))

                chat = cur.fetchone()

                if chat:
                    chat_id = chat[0]

                else:
                    cur.execute("""
                        INSERT INTO web_chats
                            (user_id, title)
                        VALUES
                            (%s, %s)
                        RETURNING id
                    """, (
                        user_id,
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
                    user_id,
                ))

        conn.commit()

        logging.info(
            "AskOra database initialized successfully."
        )

    except Exception:
        conn.rollback()
        logging.exception(
            "Database initialization failed."
        )
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
    sent = request.headers.get("X-CSRF-Token")

    if not sent:
        sent = request.form.get("csrf_token")

    expected = session.get("csrf_token")

    return (
        bool(sent)
        and bool(expected)
        and secrets.compare_digest(
            sent,
            expected
        )
    )


def require_csrf():
    if not csrf_valid():
        abort(
            400,
            description="Invalid security token."
        )


# ============================================================
# USER HELPERS
# ============================================================

def current_user():
    user_id = session.get("user_id")

    if not user_id:
        return None

    conn = get_db()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                SELECT
                    id,
                    username,
                    email,
                    first_seen,
                    last_seen
                FROM web_users
                WHERE id = %s
            """, (user_id,))

            return cur.fetchone()

    finally:
        conn.close()


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):

        if not session.get("user_id"):
            return redirect(url_for("login"))

        return func(*args, **kwargs)

    return wrapper


def api_login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):

        if not session.get("user_id"):
            return jsonify({
                "ok": False,
                "error": "You are not logged in."
            }), 401

        return func(*args, **kwargs)

    return wrapper


def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):

        user = current_user()

        if not user:
            return redirect(url_for("login"))

        if user["username"].lower() != ADMIN_USERNAME:
            abort(403)

        return func(*args, **kwargs)

    return wrapper


def valid_username(username):
    return bool(
        re.fullmatch(
            r"[a-z0-9_]{3,32}",
            username
        )
    )


def valid_email(email):
    return bool(
        re.fullmatch(
            r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
            email
        )
    )


# ============================================================
# CHAT HELPERS
# ============================================================

def create_chat(user_id, title="New chat"):
    conn = get_db()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                INSERT INTO web_chats
                    (user_id, title)
                VALUES
                    (%s, %s)
                RETURNING
                    id,
                    title,
                    created_at,
                    updated_at
            """, (
                user_id,
                title[:80],
            ))

            chat = cur.fetchone()

        conn.commit()

        return chat

    finally:
        conn.close()


def chat_belongs_to_user(user_id, chat_id):
    conn = get_db()

    try:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT 1
                FROM web_chats
                WHERE id = %s
                AND user_id = %s
            """, (
                chat_id,
                user_id,
            ))

            return cur.fetchone() is not None

    finally:
        conn.close()


def get_or_create_chat(user_id, chat_id=None):

    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        chat_id = None

    if (
        chat_id
        and chat_belongs_to_user(
            user_id,
            chat_id
        )
    ):
        return chat_id

    chat = create_chat(
        user_id,
        "New chat"
    )

    return chat["id"]


def make_title(text):
    text = re.sub(
        r"\s+",
        " ",
        text.strip()
    )

    if not text:
        return "New chat"

    return text[:60]


def log_event(user_id, event_type):
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

    except Exception:
        conn.rollback()

        logging.exception(
            "Could not save usage event."
        )

    finally:
        conn.close()


# ============================================================
# AI
# ============================================================

def ask_ai(user_id, chat_id, current_message):

    conn = get_db()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                SELECT
                    role,
                    content
                FROM web_messages
                WHERE chat_id = %s
                AND user_id = %s
                ORDER BY created_at DESC
                LIMIT 20
            """, (
                chat_id,
                user_id,
            ))

            history = list(
                reversed(
                    cur.fetchall()
                )
            )

    finally:
        conn.close()

    messages = [
        {
            "role": "system",
            "content": (
                "You are AskOra, a helpful AI assistant. "
                "Answer clearly, accurately and naturally. "
                "Use proper Markdown formatting when useful. "
                "Use real headings, bullet lists, numbered lists, "
                "tables and code blocks where appropriate. "
                "Do not put Markdown symbols together incorrectly. "
                "Keep answers easy to read."
            ),
        }
    ]

    for item in history:
        messages.append({
            "role": item["role"],
            "content": item["content"],
        })

    response = groq_client.chat.completions.create(
        model=TEXT_MODEL,
        messages=messages,
        max_tokens=1400,
    )

    return response.choices[0].message.content


# ============================================================
# PAGES
# ============================================================

@app.route("/")
def home():

    if not session.get("user_id"):
        return redirect(
            url_for("login")
        )

    user = current_user()

    return render_template(
        "index.html",
        user=user,
        csrf_token=get_csrf_token(),
        is_admin=(
            user["username"].lower()
            == ADMIN_USERNAME
        ),
    )


# ============================================================
# REGISTER
# ============================================================

@app.route(
    "/register",
    methods=["GET", "POST"]
)
def register():

    error = None

    if request.method == "POST":

        require_csrf()

        username = (
            request.form.get(
                "username",
                ""
            )
            .strip()
            .lower()
        )

        email = (
            request.form.get(
                "email",
                ""
            )
            .strip()
            .lower()
        )

        password = request.form.get(
            "password",
            ""
        )

        confirm_password = request.form.get(
            "confirm_password",
            ""
        )

        if not valid_username(username):

            error = (
                "Username must be 3–32 characters "
                "and use only letters, numbers "
                "and underscores."
            )

        elif not valid_email(email):

            error = (
                "Please enter a valid email address."
            )

        elif len(password) < 8:

            error = (
                "Password must be at least 8 characters."
            )

        elif password != confirm_password:

            error = (
                "Passwords do not match."
            )

        else:

            conn = get_db()

            try:

                with conn.cursor(
                    cursor_factory=RealDictCursor
                ) as cur:

                    cur.execute("""
                        SELECT id
                        FROM web_users
                        WHERE LOWER(username) = %s
                        LIMIT 1
                    """, (
                        username,
                    ))

                    if cur.fetchone():

                        error = (
                            "That username is already taken."
                        )

                    else:

                        cur.execute("""
                            SELECT id
                            FROM web_users
                            WHERE LOWER(email) = %s
                            LIMIT 1
                        """, (
                            email,
                        ))

                        if cur.fetchone():

                            error = (
                                "That email is already registered."
                            )

                        else:

                            password_hash = (
                                generate_password_hash(
                                    password
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
                                    (
                                        %s,
                                        %s,
                                        %s
                                    )
                                RETURNING id
                            """, (
                                username,
                                email,
                                password_hash,
                            ))

                            user_id = (
                                cur.fetchone()["id"]
                            )

                            conn.commit()

                            create_chat(
                                user_id,
                                "New chat"
                            )

                            session.clear()

                            session["user_id"] = user_id

                            get_csrf_token()

                            log_event(
                                user_id,
                                "registration"
                            )

                            return redirect(
                                url_for("home")
                            )

            except psycopg2.errors.UniqueViolation:

                conn.rollback()

                error = (
                    "That username or email "
                    "is already registered."
                )

            except Exception:

                conn.rollback()

                logging.exception(
                    "Registration failed."
                )

                error = (
                    "Account creation failed. "
                    "Please try again."
                )

            finally:
                conn.close()

    return render_template(
        "register.html",
        error=error,
        csrf_token=get_csrf_token(),
    )


# ============================================================
# LOGIN
# ============================================================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    error = None

    if request.method == "POST":

        require_csrf()

        identifier = (
            request.form.get(
                "username",
                ""
            )
            .strip()
            .lower()
        )

        password = request.form.get(
            "password",
            ""
        )

        if not identifier or not password:

            error = (
                "Please enter your username/email "
                "and password."
            )

        else:

            conn = get_db()

            try:

                with conn.cursor(
                    cursor_factory=RealDictCursor
                ) as cur:

                    cur.execute("""
                        SELECT *
                        FROM web_users
                        WHERE LOWER(username) = %s
                           OR LOWER(email) = %s
                        LIMIT 1
                    """, (
                        identifier,
                        identifier,
                    ))

                    user = cur.fetchone()

                    if not user:

                        error = "Account not found."

                    elif not check_password_hash(
                        user["password_hash"],
                        password
                    ):

                        error = "Incorrect password."

                    else:

                        cur.execute("""
                            UPDATE web_users
                            SET last_seen = NOW()
                            WHERE id = %s
                        """, (
                            user["id"],
                        ))

                        conn.commit()

                        session.clear()

                        session["user_id"] = (
                            user["id"]
                        )

                        get_csrf_token()

                        log_event(
                            user["id"],
                            "login"
                        )

                        return redirect(
                            url_for("home")
                        )

            except Exception:

                conn.rollback()

                logging.exception(
                    "Login failed."
                )

                error = (
                    "Something went wrong "
                    "while signing in."
                )

            finally:
                conn.close()

    return render_template(
        "login.html",
        error=error,
        csrf_token=get_csrf_token(),
    )


# ============================================================
# LOGOUT
# ============================================================

@app.route(
    "/logout",
    methods=["POST"]
)
def logout():

    user_id = session.get("user_id")

    if not user_id:
        return jsonify({
            "ok": True
        })

    if not csrf_valid():

        return jsonify({
            "ok": False,
            "error": "Invalid security token."
        }), 400

    log_event(
        user_id,
        "logout"
    )

    session.clear()

    return jsonify({
        "ok": True
    })


# ============================================================
# CHAT API
# ============================================================

@app.route(
    "/api/chats",
    methods=["GET"]
)
@api_login_required
def get_chats():

    user_id = session["user_id"]

    conn = get_db()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                SELECT
                    id,
                    title,
                    created_at,
                    updated_at
                FROM web_chats
                WHERE user_id = %s
                ORDER BY updated_at DESC
            """, (
                user_id,
            ))

            chats = cur.fetchall()

        return jsonify({
            "ok": True,
            "chats": chats,
        })

    finally:
        conn.close()


@app.route(
    "/api/chats",
    methods=["POST"]
)
@api_login_required
def new_chat():

    require_csrf()

    user_id = session["user_id"]

    chat = create_chat(
        user_id,
        "New chat"
    )

    return jsonify({
        "ok": True,
        "chat": chat,
    })


@app.route(
    "/api/chats/<int:chat_id>/messages"
)
@api_login_required
def get_messages(chat_id):

    user_id = session["user_id"]

    if not chat_belongs_to_user(
        user_id,
        chat_id
    ):

        return jsonify({
            "ok": False,
            "error": "Chat not found."
        }), 404

    conn = get_db()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                SELECT
                    id,
                    role,
                    content,
                    created_at
                FROM web_messages
                WHERE chat_id = %s
                AND user_id = %s
                ORDER BY created_at ASC
            """, (
                chat_id,
                user_id,
            ))

            messages = cur.fetchall()

        return jsonify({
            "ok": True,
            "messages": messages,
        })

    finally:
        conn.close()


# ============================================================
# DELETE CHAT
# ============================================================

@app.route(
    "/api/chats/<int:chat_id>",
    methods=["DELETE"]
)
@api_login_required
def delete_chat(chat_id):

    require_csrf()

    user_id = session["user_id"]

    if not chat_belongs_to_user(
        user_id,
        chat_id
    ):

        return jsonify({
            "ok": False,
            "error": "Chat not found."
        }), 404

    conn = get_db()

    try:

        with conn.cursor() as cur:

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

        return jsonify({
            "ok": True
        })

    except Exception:

        conn.rollback()

        logging.exception(
            "Chat deletion failed."
        )

        return jsonify({
            "ok": False,
            "error": "Could not delete chat."
        }), 500

    finally:
        conn.close()


# ============================================================
# TEXT CHAT
# ============================================================

@app.route(
    "/api/chat",
    methods=["POST"]
)
@api_login_required
def chat():

    require_csrf()

    user_id = session["user_id"]

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    message = str(
        data.get(
            "message",
            ""
        )
    ).strip()

    requested_chat_id = data.get(
        "chat_id"
    )

    if not message:

        return jsonify({
            "ok": False,
            "error": "Please enter a message."
        }), 400

    if len(message) > 12000:

        return jsonify({
            "ok": False,
            "error": "Message is too long."
        }), 400

    chat_id = get_or_create_chat(
        user_id,
        requested_chat_id
    )

    conn = get_db()

    try:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT COUNT(*)
                FROM web_messages
                WHERE chat_id = %s
                AND user_id = %s
            """, (
                chat_id,
                user_id,
            ))

            count = cur.fetchone()[0]

            if count == 0:

                cur.execute("""
                    UPDATE web_chats
                    SET
                        title = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    AND user_id = %s
                """, (
                    make_title(message),
                    chat_id,
                    user_id,
                ))

            cur.execute("""
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
            """, (
                chat_id,
                user_id,
                message,
            ))

        conn.commit()

    finally:
        conn.close()

    try:

        answer = ask_ai(
            user_id,
            chat_id,
            message
        )

    except Exception:

        logging.exception(
            "Groq generation failed."
        )

        return jsonify({
            "ok": False,
            "error": (
                "AskOra couldn't generate "
                "a response right now."
            )
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
                    (
                        %s,
                        %s,
                        'assistant',
                        %s
                    )
            """, (
                chat_id,
                user_id,
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

    log_event(
        user_id,
        "chat"
    )

    return jsonify({
        "ok": True,
        "chat_id": chat_id,
        "answer": answer,
    })


# ============================================================
# VOICE
# ============================================================

@app.route(
    "/api/voice",
    methods=["POST"]
)
@api_login_required
def voice():

    require_csrf()

    user_id = session["user_id"]

    audio = request.files.get(
        "audio"
    )

    if not audio:

        return jsonify({
            "ok": False,
            "error": "No audio was received."
        }), 400

    audio_bytes = audio.read()

    if len(audio_bytes) > MAX_AUDIO_BYTES:

        return jsonify({
            "ok": False,
            "error": "Audio file is too large."
        }), 400

    if not audio_bytes:

        return jsonify({
            "ok": False,
            "error": "The recording was empty."
        }), 400

    chat_id = get_or_create_chat(
        user_id,
        request.form.get("chat_id")
    )

    try:

        transcription = (
            groq_client
            .audio
            .transcriptions
            .create(
                file=(
                    audio.filename
                    or "recording.webm",
                    BytesIO(audio_bytes),
                ),
                model=VOICE_MODEL,
                response_format="text",
            )
        )

        if hasattr(
            transcription,
            "text"
        ):
            text = transcription.text.strip()
        else:
            text = str(
                transcription
            ).strip()

        if not text:

            return jsonify({
                "ok": False,
                "error": (
                    "I couldn't understand "
                    "the recording."
                )
            }), 400

        conn = get_db()

        try:

            with conn.cursor() as cur:

                cur.execute("""
                    SELECT COUNT(*)
                    FROM web_messages
                    WHERE chat_id = %s
                    AND user_id = %s
                """, (
                    chat_id,
                    user_id,
                ))

                count = cur.fetchone()[0]

                if count == 0:

                    cur.execute("""
                        UPDATE web_chats
                        SET
                            title = %s,
                            updated_at = NOW()
                        WHERE id = %s
                        AND user_id = %s
                    """, (
                        make_title(text),
                        chat_id,
                        user_id,
                    ))

                cur.execute("""
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
                """, (
                    chat_id,
                    user_id,
                    text,
                ))

            conn.commit()

        finally:
            conn.close()

        answer = ask_ai(
            user_id,
            chat_id,
            text
        )

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
                        (
                            %s,
                            %s,
                            'assistant',
                            %s
                        )
                """, (
                    chat_id,
                    user_id,
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

        log_event(
            user_id,
            "voice"
        )

        return jsonify({
            "ok": True,
            "chat_id": chat_id,
            "transcription": text,
            "answer": answer,
        })

    except Exception:

        logging.exception(
            "Voice processing failed."
        )

        return jsonify({
            "ok": False,
            "error": (
                "Voice processing failed. "
                "Please try again."
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
        user=current_user(),
        csrf_token=get_csrf_token(),
    )


@app.route("/api/admin/stats")
@api_login_required
def admin_stats():

    user = current_user()

    if (
        not user
        or user["username"].lower()
        != ADMIN_USERNAME
    ):

        return jsonify({
            "ok": False,
            "error": "Unauthorized."
        }), 403

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
                WHERE first_seen::date = CURRENT_DATE
            """)

            users_today = (
                cur.fetchone()["count"]
            )

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM web_users
                WHERE last_seen::date = CURRENT_DATE
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
            """)

            total_messages = (
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
                    id,
                    username,
                    email,
                    first_seen,
                    last_seen
                FROM web_users
                ORDER BY first_seen DESC
                LIMIT 100
            """)

            users = cur.fetchall()

        return jsonify({
            "ok": True,
            "stats": {
                "total_users": total_users,
                "users_today": users_today,
                "active_today": active_today,
                "total_chats": total_chats,
                "total_messages": total_messages,
                "events": events,
                "users": users,
            },
        })

    finally:
        conn.close()


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():
    return "OK", 200


# ============================================================
# ERRORS
# ============================================================

@app.errorhandler(400)
def bad_request(error):

    if request.path.startswith("/api/"):

        return jsonify({
            "ok": False,
            "error": str(
                error.description
            ),
        }), 400

    return str(
        error.description
    ), 400


@app.errorhandler(403)
def forbidden(error):

    if request.path.startswith("/api/"):

        return jsonify({
            "ok": False,
            "error": "You are not authorized."
        }), 403

    return (
        "You are not authorized.",
        403
    )


# ============================================================
# STARTUP
# ============================================================

init_database()


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
    )
