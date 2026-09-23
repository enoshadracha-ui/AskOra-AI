import os
import re
import secrets
import logging
from functools import wraps
from io import BytesIO
from datetime import datetime, timezone

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
from werkzeug.security import generate_password_hash, check_password_hash
from groq import Groq


# ============================================================
# SETTINGS
# ============================================================

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

PORT = int(os.environ.get("PORT", "10000"))

# Username of the AskOra website administrator.
# Set this directly in Render Environment Variables.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin").lower()

TEXT_MODEL = "openai/gpt-oss-120b"
VOICE_MODEL = "whisper-large-v3-turbo"

app = Flask(__name__)

# IMPORTANT:
# Set SESSION_SECRET directly in Render.
app.secret_key = os.environ.get("SESSION_SECRET", secrets.token_hex(32))

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = True
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # 15 MB


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
# ASKORA AI INSTRUCTIONS
# ============================================================

SYSTEM_INSTRUCTION = """
You are AskOra, a smart, friendly and reliable AI assistant.

Your personality:
- Smart
- Simple
- Friendly
- Fast
- Clear
- Helpful

Answer naturally and accurately.

Keep responses concise:
- Simple questions: 1–3 sentences.
- Normal questions: around 40–100 words.
- Use bullets or Markdown when useful.
- Do not write huge essays unless the user asks for detail.
- If the user asks a follow-up question, use the previous conversation context.
- Remember relevant details from the current conversation.
- Never claim to know something you do not know.
"""


# ============================================================
# DATABASE
# ============================================================

def get_db():
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
        cursor_factory=RealDictCursor,
    )


def init_database():
    conn = get_db()

    try:
        with conn.cursor() as cur:

            # ------------------------------------------------
            # NEW WEB USERS TABLE
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

            # ------------------------------------------------
            # WEB USAGE EVENTS
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS web_usage_events (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL
                        REFERENCES web_users(id)
                        ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            # ------------------------------------------------
            # INDEXES
            # ------------------------------------------------

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_web_messages_user
                ON web_messages(user_id)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_web_messages_created
                ON web_messages(created_at)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_web_events_user
                ON web_usage_events(user_id)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_web_events_created
                ON web_usage_events(created_at)
            """)

            conn.commit()

            logger.info("Database initialized successfully.")

    finally:
        conn.close()


# ============================================================
# CSRF
# ============================================================

def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)

    return session["csrf_token"]


@app.context_processor
def inject_csrf():
    return {
        "csrf_token": get_csrf_token()
    }


def check_csrf():
    token = request.headers.get("X-CSRF-Token")

    if not token:
        token = request.form.get("csrf_token")

    stored = session.get("csrf_token")

    return bool(
        token
        and stored
        and secrets.compare_digest(token, stored)
    )


# ============================================================
# USER HELPERS
# ============================================================

USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]{3,30}$")


def validate_username(username):
    return bool(USERNAME_PATTERN.fullmatch(username))


def get_current_user():
    user_id = session.get("user_id")

    if not user_id:
        return None

    conn = get_db()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, first_seen, last_seen
                FROM web_users
                WHERE id = %s
                """,
                (user_id,),
            )

            return cur.fetchone()

    finally:
        conn.close()


def login_required(func):

    @wraps(func)
    def wrapper(*args, **kwargs):

        if not session.get("user_id"):
            return jsonify({
                "success": False,
                "error": "Please log in."
            }), 401

        return func(*args, **kwargs)

    return wrapper


# ============================================================
# USER DATABASE FUNCTIONS
# ============================================================

def create_user(username, password):

    conn = get_db()

    try:
        with conn.cursor() as cur:

            password_hash = generate_password_hash(password)

            cur.execute(
                """
                INSERT INTO web_users
                (username, password_hash)
                VALUES (%s, %s)
                RETURNING id
                """,
                (username, password_hash),
            )

            user_id = cur.fetchone()["id"]

            conn.commit()

            return user_id

    finally:
        conn.close()


def authenticate_user(username, password):

    conn = get_db()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id, username, password_hash
                FROM web_users
                WHERE username = %s
                """,
                (username,),
            )

            user = cur.fetchone()

            if not user:
                return None

            if not check_password_hash(
                user["password_hash"],
                password,
            ):
                return None

            cur.execute(
                """
                UPDATE web_users
                SET last_seen = NOW()
                WHERE id = %s
                """,
                (user["id"],),
            )

            conn.commit()

            return user

    finally:
        conn.close()


def record_usage(user_id, event_type):

    conn = get_db()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO web_usage_events
                (user_id, event_type)
                VALUES (%s, %s)
                """,
                (user_id, event_type),
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

    finally:
        conn.close()


def save_message(user_id, role, content):

    conn = get_db()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO web_messages
                (user_id, role, content)
                VALUES (%s, %s, %s)
                """,
                (user_id, role, content),
            )

            conn.commit()

    finally:
        conn.close()


def get_history(user_id):

    conn = get_db()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT role, content, created_at
                FROM web_messages
                WHERE user_id = %s
                ORDER BY created_at ASC
                LIMIT 100
                """,
                (user_id,),
            )

            return cur.fetchall()

    finally:
        conn.close()


def clear_history(user_id):

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

            conn.commit()

    finally:
        conn.close()


# ============================================================
# AI
# ============================================================

def build_ai_messages(user_id, current_message):

    history = get_history(user_id)

    messages = [
        {
            "role": "system",
            "content": SYSTEM_INSTRUCTION,
        }
    ]

    for item in history[-30:]:

        content = item["content"]

        # Prevent accidental duplicate current message.
        if (
            item["role"] == "user"
            and content.strip() == current_message.strip()
        ):
            continue

        messages.append({
            "role": item["role"],
            "content": content,
        })

    messages.append({
        "role": "user",
        "content": current_message,
    })

    return messages


def generate_answer(user_id, message):

    messages = build_ai_messages(
        user_id,
        message,
    )

    response = groq_client.chat.completions.create(
        model=TEXT_MODEL,
        messages=messages,
        temperature=0.7,
        max_tokens=1200,
    )

    return response.choices[0].message.content.strip()


def transcribe_audio(audio_bytes, filename):

    response = groq_client.audio.transcriptions.create(
        file=(
            filename,
            BytesIO(audio_bytes),
        ),
        model=VOICE_MODEL,
    )

    return response.text.strip()


# ============================================================
# AUTH PAGES
# ============================================================

@app.route("/register", methods=["GET", "POST"])
def register():

    if session.get("user_id"):
        return redirect(url_for("home"))

    if request.method == "GET":
        return render_template(
            "register.html",
            csrf_token=get_csrf_token(),
        )

    if not check_csrf():
        return jsonify({
            "success": False,
            "error": "Security check failed."
        }), 403

    username = request.form.get(
        "username",
        "",
    ).strip().lower()

    password = request.form.get(
        "password",
        "",
    )

    if not validate_username(username):

        return render_template(
            "register.html",
            error=(
                "Username must be 3–30 characters "
                "using only letters, numbers or underscores."
            ),
            csrf_token=get_csrf_token(),
        )

    if len(password) < 8:

        return render_template(
            "register.html",
            error="Password must be at least 8 characters.",
            csrf_token=get_csrf_token(),
        )

    try:

        user_id = create_user(
            username,
            password,
        )

    except psycopg2.errors.UniqueViolation:

        return render_template(
            "register.html",
            error="That username is already taken.",
            csrf_token=get_csrf_token(),
        )

    session.clear()

    session["user_id"] = user_id
    session["username"] = username

    session["csrf_token"] = secrets.token_urlsafe(32)

    record_usage(
        user_id,
        "signup",
    )

    return redirect(url_for("home"))


@app.route("/login", methods=["GET", "POST"])
def login():

    if session.get("user_id"):
        return redirect(url_for("home"))

    if request.method == "GET":
        return render_template(
            "login.html",
            csrf_token=get_csrf_token(),
        )

    if not check_csrf():
        return jsonify({
            "success": False,
            "error": "Security check failed."
        }), 403

    username = request.form.get(
        "username",
        "",
    ).strip().lower()

    password = request.form.get(
        "password",
        "",
    )

    user = authenticate_user(
        username,
        password,
    )

    if not user:

        return render_template(
            "login.html",
            error="Incorrect username or password.",
            csrf_token=get_csrf_token(),
        )

    session.clear()

    session["user_id"] = user["id"]
    session["username"] = user["username"]

    session["csrf_token"] = secrets.token_urlsafe(32)

    record_usage(
        user["id"],
        "login",
    )

    return redirect(url_for("home"))


@app.route("/logout", methods=["POST"])
def logout():

    if not check_csrf():
        return jsonify({
            "success": False,
            "error": "Security check failed."
        }), 403

    session.clear()

    return redirect(url_for("login"))


# ============================================================
# MAIN WEBSITE
# ============================================================

@app.route("/")
def home():

    if not session.get("user_id"):
        return redirect(url_for("login"))

    return render_template(
        "index.html",
        username=session.get("username"),
        csrf_token=get_csrf_token(),
    )


# ============================================================
# API
# ============================================================

@app.route("/api/me")
@login_required
def api_me():

    user = get_current_user()

    if not user:
        return jsonify({
            "success": False,
            "error": "User not found."
        }), 404

    return jsonify({
        "success": True,
        "username": user["username"],
        "is_admin": (
            user["username"].lower()
            == ADMIN_USERNAME
        ),
    })


@app.route("/api/history")
@login_required
def api_history():

    user_id = session["user_id"]

    history = get_history(user_id)

    return jsonify({
        "success": True,
        "messages": [
            {
                "role": item["role"],
                "content": item["content"],
            }
            for item in history
        ],
    })


@app.route("/api/chat", methods=["POST"])
@login_required
def api_chat():

    if not check_csrf():
        return jsonify({
            "success": False,
            "error": "Security check failed."
        }), 403

    data = request.get_json(
        silent=True
    ) or {}

    message = str(
        data.get("message", "")
    ).strip()

    if not message:

        return jsonify({
            "success": False,
            "error": "Please enter a message."
        }), 400

    if len(message) > 10000:

        return jsonify({
            "success": False,
            "error": "Message is too long."
        }), 400

    user_id = session["user_id"]

    try:

        save_message(
            user_id,
            "user",
            message,
        )

        record_usage(
            user_id,
            "text",
        )

        answer = generate_answer(
            user_id,
            message,
        )

        save_message(
            user_id,
            "assistant",
            answer,
        )

        return jsonify({
            "success": True,
            "answer": answer,
        })

    except Exception as error:

        logger.exception(
            "Chat error: %s",
            error,
        )

        return jsonify({
            "success": False,
            "error": (
                "Something went wrong while "
                "generating the response."
            ),
        }), 500


@app.route("/api/voice", methods=["POST"])
@login_required
def api_voice():

    if not check_csrf():
        return jsonify({
            "success": False,
            "error": "Security check failed."
        }), 403

    if "audio" not in request.files:

        return jsonify({
            "success": False,
            "error": "No audio file received."
        }), 400

    audio = request.files["audio"]

    audio_bytes = audio.read()

    if not audio_bytes:

        return jsonify({
            "success": False,
            "error": "The recording was empty."
        }), 400

    if len(audio_bytes) > 15 * 1024 * 1024:

        return jsonify({
            "success": False,
            "error": "Audio file is too large."
        }), 400

    filename = audio.filename or "recording.webm"

    user_id = session["user_id"]

    try:

        transcript = transcribe_audio(
            audio_bytes,
            filename,
        )

        if not transcript:

            return jsonify({
                "success": False,
                "error": "I couldn't understand the recording."
            }), 400

        save_message(
            user_id,
            "user",
            transcript,
        )

        record_usage(
            user_id,
            "voice",
        )

        answer = generate_answer(
            user_id,
            transcript,
        )

        save_message(
            user_id,
            "assistant",
            answer,
        )

        return jsonify({
            "success": True,
            "transcript": transcript,
            "answer": answer,
        })

    except Exception as error:

        logger.exception(
            "Voice error: %s",
            error,
        )

        return jsonify({
            "success": False,
            "error": (
                "Something went wrong while "
                "processing your voice message."
            ),
        }), 500


@app.route("/api/reset", methods=["POST"])
@login_required
def api_reset():

    if not check_csrf():
        return jsonify({
            "success": False,
            "error": "Security check failed."
        }), 403

    user_id = session["user_id"]

    clear_history(user_id)

    record_usage(
        user_id,
        "reset",
    )

    return jsonify({
        "success": True,
    })


# ============================================================
# ADMIN
# ============================================================

@app.route("/admin")
@login_required
def admin():

    username = session.get(
        "username",
        "",
    ).lower()

    if username != ADMIN_USERNAME:

        return jsonify({
            "success": False,
            "error": "Unauthorized."
        }), 403

    return render_template(
        "admin.html",
        username=username,
        csrf_token=get_csrf_token(),
    )


@app.route("/api/admin/stats")
@login_required
def admin_stats():

    username = session.get(
        "username",
        "",
    ).lower()

    if username != ADMIN_USERNAME:

        return jsonify({
            "success": False,
            "error": "Unauthorized."
        }), 403

    conn = get_db()

    try:

        with conn.cursor() as cur:

            stats = {}

            cur.execute(
                "SELECT COUNT(*) AS count FROM web_users"
            )
            stats["total_users"] = cur.fetchone()["count"]

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM web_users
                WHERE first_seen >= CURRENT_DATE
            """)
            stats["new_users_today"] = cur.fetchone()["count"]

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM web_users
                WHERE first_seen >= CURRENT_DATE - INTERVAL '7 days'
            """)
            stats["new_users_week"] = cur.fetchone()["count"]

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM web_users
                WHERE last_seen >= CURRENT_DATE
            """)
            stats["active_users_today"] = cur.fetchone()["count"]

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM web_messages
            """)
            stats["total_messages"] = cur.fetchone()["count"]

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM web_messages
                WHERE created_at >= CURRENT_DATE
            """)
            stats["messages_today"] = cur.fetchone()["count"]

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM web_usage_events
                WHERE event_type = 'text'
            """)
            stats["text_requests"] = cur.fetchone()["count"]

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM web_usage_events
                WHERE event_type = 'voice'
            """)
            stats["voice_requests"] = cur.fetchone()["count"]

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM web_usage_events
            """)
            stats["total_events"] = cur.fetchone()["count"]

            return jsonify({
                "success": True,
                **stats,
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
        conn.close()

        return jsonify({
            "status": "ok",
            "service": "AskOra",
        })

    except Exception:

        return jsonify({
            "status": "error",
        }), 500


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(413)
def file_too_large(error):

    return jsonify({
        "success": False,
        "error": "File is too large."
    }), 413


@app.errorhandler(404)
def not_found(error):

    if request.path.startswith("/api/"):

        return jsonify({
            "success": False,
            "error": "Not found."
        }), 404

    return redirect(url_for("login"))


# ============================================================
# STARTUP
# ============================================================

if __name__ == "__main__":

    logger.info("Starting AskOra standalone website...")

    init_database()

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
    )
