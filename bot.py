import os
import re
import secrets
import hashlib
import smtplib
import logging
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from functools import wraps

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import (
    Flask,
    request,
    redirect,
    url_for,
    session,
    jsonify,
    render_template,
    render_template_string,
)
from werkzeug.security import generate_password_hash, check_password_hash
from groq import Groq


# ============================================================
# CONFIG
# ============================================================

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

SESSION_SECRET = os.environ.get(
    "SESSION_SECRET",
    secrets.token_hex(32)
)

ADMIN_USERNAME = os.environ.get(
    "ADMIN_USERNAME",
    "admin"
).strip().lower()

TEXT_MODEL = "openai/gpt-oss-120b"
VOICE_MODEL = "whisper-large-v3-turbo"

PORT = int(os.environ.get("PORT", "10000"))

# Email settings
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get(
    "SMTP_FROM",
    SMTP_USERNAME
)

PASSWORD_RESET_MINUTES = 30

MAX_AUDIO_SIZE = 15 * 1024 * 1024


# ============================================================
# APP
# ============================================================

app = Flask(__name__)

app.secret_key = SESSION_SECRET

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
)

groq_client = Groq(api_key=GROQ_API_KEY)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


# ============================================================
# DATABASE
# ============================================================

def get_db():
    return psycopg2.connect(
        DATABASE_URL,
        sslmode=os.environ.get("PGSSLMODE", "require")
    )


def init_db():
    conn = get_db()

    try:
        cur = conn.cursor()

        # ----------------------------------------------------
        # WEB USERS
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS web_users (
                id BIGSERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                email TEXT,
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
            ALTER TABLE web_users
            ADD COLUMN IF NOT EXISTS first_seen
            TIMESTAMPTZ NOT NULL DEFAULT NOW()
        """)

        cur.execute("""
            ALTER TABLE web_users
            ADD COLUMN IF NOT EXISTS last_seen
            TIMESTAMPTZ NOT NULL DEFAULT NOW()
        """)

        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_web_users_email_lower
            ON web_users (LOWER(email))
            WHERE email IS NOT NULL
        """)

        # ----------------------------------------------------
        # CHATS
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # MESSAGES
        # ----------------------------------------------------

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
            CREATE INDEX IF NOT EXISTS
            idx_web_messages_chat
            ON web_messages(chat_id, created_at)
        """)

        # ----------------------------------------------------
        # USAGE EVENTS
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS web_usage_events (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT,
                event_type TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # ----------------------------------------------------
        # PASSWORD RESET
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS web_password_resets (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL
                    REFERENCES web_users(id)
                    ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TIMESTAMPTZ NOT NULL,
                used_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_password_reset_token
            ON web_password_resets(token_hash)
        """)

        # ----------------------------------------------------
        # MIGRATE OLD MESSAGES INTO CHATS
        # ----------------------------------------------------

        cur.execute("""
            SELECT id
            FROM web_users
        """)

        user_ids = [row[0] for row in cur.fetchall()]

        for user_id in user_ids:

            cur.execute("""
                SELECT COUNT(*)
                FROM web_chats
                WHERE user_id = %s
            """, (user_id,))

            chat_count = cur.fetchone()[0]

            if chat_count == 0:

                cur.execute("""
                    SELECT COUNT(*)
                    FROM web_messages
                    WHERE user_id = %s
                    AND chat_id IS NULL
                """, (user_id,))

                old_messages = cur.fetchone()[0]

                if old_messages > 0:

                    cur.execute("""
                        INSERT INTO web_chats
                        (user_id, title)
                        VALUES (%s, %s)
                        RETURNING id
                    """, (
                        user_id,
                        "Previous chat"
                    ))

                    chat_id = cur.fetchone()[0]

                    cur.execute("""
                        UPDATE web_messages
                        SET chat_id = %s
                        WHERE user_id = %s
                        AND chat_id IS NULL
                    """, (
                        chat_id,
                        user_id
                    ))

        conn.commit()

        logging.info("Database initialized successfully.")

    except Exception:
        conn.rollback()
        logging.exception("Database initialization failed.")
        raise

    finally:
        cur.close()
        conn.close()


# ============================================================
# HELPERS
# ============================================================

def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)

    return session["csrf_token"]


def check_csrf():
    token = request.headers.get("X-CSRF-Token")

    if not token:
        token = request.form.get("csrf_token")

    return (
        token
        and token == session.get("csrf_token")
    )


def current_user():
    user_id = session.get("user_id")

    if not user_id:
        return None

    conn = get_db()

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("""
            SELECT id, username, email,
                   first_seen, last_seen
            FROM web_users
            WHERE id = %s
        """, (user_id,))

        user = cur.fetchone()

        if user:
            cur.execute("""
                UPDATE web_users
                SET last_seen = NOW()
                WHERE id = %s
            """, (user_id,))

            conn.commit()

        return user

    finally:
        cur.close()
        conn.close()


def is_admin(user):
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
            return redirect(url_for("login"))

        return func(*args, **kwargs)

    return wrapper


def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):

        user = current_user()

        if not user or not is_admin(user):
            return "Unauthorized", 403

        return func(*args, **kwargs)

    return wrapper


def valid_username(username):
    return bool(
        re.fullmatch(
            r"[a-zA-Z0-9_]{3,32}",
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


def clean_title(text):
    text = re.sub(
        r"\s+",
        " ",
        text.strip()
    )

    if not text:
        return "New chat"

    if len(text) > 45:
        return text[:45].rstrip() + "..."

    return text


def hash_reset_token(token):
    return hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()


# ============================================================
# EMAIL
# ============================================================

def send_password_reset_email(
    email,
    username,
    reset_url
):

    if not SMTP_USERNAME or not SMTP_PASSWORD:
        logging.error(
            "SMTP credentials are not configured."
        )
        return False

    message = EmailMessage()

    message["Subject"] = "Reset your AskOra password"
    message["From"] = SMTP_FROM
    message["To"] = email

    message.set_content(
        f"""Hello {username},

We received a request to reset your AskOra password.

Use the link below to create a new password:

{reset_url}

This link expires in {PASSWORD_RESET_MINUTES} minutes.

If you did not request this, you can safely ignore this email.

AskOra
Ask. Get answers.
"""
    )

    try:

        with smtplib.SMTP(
            SMTP_HOST,
            SMTP_PORT
        ) as smtp:

            smtp.starttls()

            smtp.login(
                SMTP_USERNAME,
                SMTP_PASSWORD
            )

            smtp.send_message(message)

        return True

    except Exception:
        logging.exception(
            "Could not send password reset email."
        )
        return False


# ============================================================
# AUTH PAGE UI
# ============================================================

AUTH_STYLE = """
<style>
* {
    box-sizing: border-box;
}

body {
    margin: 0;
    min-height: 100vh;
    background:
        radial-gradient(
            circle at top,
            #182044 0%,
            #0b0f20 42%,
            #070914 100%
        );
    color: #f5f7ff;
    font-family:
        Inter,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;

    display: flex;
    justify-content: center;
    align-items: center;

    padding: 24px;
}

.auth-card {
    width: 100%;
    max-width: 430px;
    background: rgba(16, 21, 43, .88);
    border: 1px solid rgba(255,255,255,.08);
    border-radius: 24px;
    padding: 32px;
    box-shadow: 0 25px 80px rgba(0,0,0,.45);
    backdrop-filter: blur(20px);
}

.logo {
    width: 52px;
    height: 52px;
    border-radius: 16px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 25px;
    font-weight: 800;
    margin-bottom: 18px;
    background: linear-gradient(
        135deg,
        #3b82f6,
        #7c3aed
    );
    box-shadow: 0 12px 35px rgba(59,130,246,.25);
}

h1 {
    margin: 0 0 8px;
    font-size: 28px;
}

.subtitle {
    color: #9ca3af;
    margin-bottom: 26px;
}

label {
    display: block;
    margin: 15px 0 7px;
    font-size: 14px;
    color: #d7dbea;
}

input {
    width: 100%;
    border: 1px solid rgba(255,255,255,.10);
    background: #080b18;
    color: white;
    padding: 14px 15px;
    border-radius: 12px;
    outline: none;
    font-size: 15px;
}

input:focus {
    border-color: #3b82f6;
    box-shadow: 0 0 0 3px rgba(59,130,246,.12);
}

button {
    width: 100%;
    margin-top: 22px;
    border: 0;
    border-radius: 12px;
    padding: 14px;
    color: white;
    font-weight: 700;
    font-size: 15px;
    cursor: pointer;
    background: linear-gradient(
        135deg,
        #3b82f6,
        #7c3aed
    );
}

button:hover {
    opacity: .93;
}

.message {
    border-radius: 12px;
    padding: 12px 14px;
    margin-bottom: 18px;
    font-size: 14px;
}

.error {
    background: rgba(239,68,68,.12);
    border: 1px solid rgba(239,68,68,.25);
    color: #fca5a5;
}

.success {
    background: rgba(34,197,94,.12);
    border: 1px solid rgba(34,197,94,.25);
    color: #86efac;
}

.links {
    margin-top: 22px;
    text-align: center;
    color: #9ca3af;
    font-size: 14px;
}

.links a {
    color: #60a5fa;
    text-decoration: none;
}

.links a:hover {
    text-decoration: underline;
}

.small {
    color: #9ca3af;
    font-size: 13px;
    margin-top: 14px;
    line-height: 1.5;
}
</style>
"""


# ============================================================
# LOGIN
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    error = None
    success = request.args.get("success")

    if request.method == "POST":

        username_or_email = request.form.get(
            "username",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        if not username_or_email or not password:

            error = "Please enter your username/email and password."

        else:

            conn = get_db()

            try:

                cur = conn.cursor(
                    cursor_factory=RealDictCursor
                )

                cur.execute("""
                    SELECT *
                    FROM web_users
                    WHERE LOWER(username) = LOWER(%s)
                       OR LOWER(email) = LOWER(%s)
                    LIMIT 1
                """, (
                    username_or_email,
                    username_or_email
                ))

                user = cur.fetchone()

                if (
                    user
                    and check_password_hash(
                        user["password_hash"],
                        password
                    )
                ):

                    session.clear()

                    session["user_id"] = user["id"]
                    session["csrf_token"] = secrets.token_urlsafe(32)

                    cur.execute("""
                        UPDATE web_users
                        SET last_seen = NOW()
                        WHERE id = %s
                    """, (user["id"],))

                    conn.commit()

                    return redirect(url_for("home"))

                error = "Incorrect username/email or password."

            finally:

                cur.close()
                conn.close()

    return render_template_string(
        AUTH_STYLE + """
        <div class="auth-card">

            <div class="logo">A</div>

            <h1>Welcome back</h1>

            <div class="subtitle">
                Sign in to continue using AskOra.
            </div>

            {% if error %}
                <div class="message error">
                    {{ error }}
                </div>
            {% endif %}

            {% if success %}
                <div class="message success">
                    {{ success }}
                </div>
            {% endif %}

            <form method="POST">

                <label>Username or email</label>

                <input
                    name="username"
                    type="text"
                    autocomplete="username"
                    placeholder="Username or email"
                    required
                >

                <label>Password</label>

                <input
                    name="password"
                    type="password"
                    autocomplete="current-password"
                    placeholder="Password"
                    required
                >

                <button type="submit">
                    Log in
                </button>

            </form>

            <div class="links">
                <a href="/forgot-password">
                    Forgot password?
                </a>
            </div>

            <div class="links">
                Don't have an account?
                <a href="/register">Create one</a>
            </div>

        </div>
        """,
        error=error,
        success=success
    )


# ============================================================
# REGISTER
# ============================================================

@app.route("/register", methods=["GET", "POST"])
def register():

    error = None

    username_value = ""
    email_value = ""

    if request.method == "POST":

        username_value = request.form.get(
            "username",
            ""
        ).strip()

        email_value = request.form.get(
            "email",
            ""
        ).strip().lower()

        password = request.form.get(
            "password",
            ""
        )

        confirm_password = request.form.get(
            "confirm_password",
            ""
        )

        if not valid_username(username_value):

            error = (
                "Username must be 3–32 characters "
                "and can only contain letters, numbers "
                "and underscores."
            )

        elif not valid_email(email_value):

            error = "Please enter a valid email address."

        elif len(password) < 8:

            error = "Password must be at least 8 characters."

        elif password != confirm_password:

            error = "Passwords do not match."

        else:

            conn = get_db()

            try:

                cur = conn.cursor(
                    cursor_factory=RealDictCursor
                )

                cur.execute("""
                    SELECT id
                    FROM web_users
                    WHERE LOWER(username) = LOWER(%s)
                """, (username_value,))

                if cur.fetchone():

                    error = "That username is already in use."

                else:

                    cur.execute("""
                        SELECT id
                        FROM web_users
                        WHERE LOWER(email) = LOWER(%s)
                    """, (email_value,))

                    if cur.fetchone():

                        error = "That email is already registered."

                    else:

                        password_hash = generate_password_hash(
                            password
                        )

                        cur.execute("""
                            INSERT INTO web_users
                            (
                                username,
                                email,
                                password_hash
                            )
                            VALUES (%s, %s, %s)
                            RETURNING id
                        """, (
                            username_value,
                            email_value,
                            password_hash
                        ))

                        user_id = cur.fetchone()["id"]

                        cur.execute("""
                            INSERT INTO web_chats
                            (user_id, title)
                            VALUES (%s, %s)
                        """, (
                            user_id,
                            "New chat"
                        ))

                        conn.commit()

                        return redirect(
                            url_for(
                                "login",
                                success="Account created successfully. You can now log in."
                            )
                        )

            except Exception:

                conn.rollback()

                logging.exception(
                    "Registration error."
                )

                error = (
                    "We couldn't create your account right now. "
                    "Please try again."
                )

            finally:

                cur.close()
                conn.close()

    return render_template_string(
        AUTH_STYLE + """
        <div class="auth-card">

            <div class="logo">A</div>

            <h1>Create your account</h1>

            <div class="subtitle">
                Start using AskOra.
            </div>

            {% if error %}
                <div class="message error">
                    {{ error }}
                </div>
            {% endif %}

            <form method="POST">

                <label>Username</label>

                <input
                    name="username"
                    type="text"
                    value="{{ username_value }}"
                    autocomplete="username"
                    placeholder="Choose a username"
                    required
                >

                <label>Email</label>

                <input
                    name="email"
                    type="email"
                    value="{{ email_value }}"
                    autocomplete="email"
                    placeholder="you@example.com"
                    required
                >

                <label>Password</label>

                <input
                    name="password"
                    type="password"
                    autocomplete="new-password"
                    placeholder="At least 8 characters"
                    required
                >

                <label>Confirm password</label>

                <input
                    name="confirm_password"
                    type="password"
                    autocomplete="new-password"
                    placeholder="Enter your password again"
                    required
                >

                <button type="submit">
                    Create account
                </button>

            </form>

            <div class="links">
                Already have an account?
                <a href="/login">Log in</a>
            </div>

        </div>
        """,
        error=error,
        username_value=username_value,
        email_value=email_value
    )


# ============================================================
# FORGOT PASSWORD
# ============================================================

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():

    message = None

    if request.method == "POST":

        email = request.form.get(
            "email",
            ""
        ).strip().lower()

        if not valid_email(email):

            message = "Please enter a valid email address."

        else:

            conn = get_db()

            try:

                cur = conn.cursor(
                    cursor_factory=RealDictCursor
                )

                cur.execute("""
                    SELECT id, username, email
                    FROM web_users
                    WHERE LOWER(email) = LOWER(%s)
                    LIMIT 1
                """, (email,))

                user = cur.fetchone()

                # Always show the same response.
                # This prevents exposing whether an email exists.
                message = (
                    "If an account exists for that email, "
                    "a password reset link has been sent."
                )

                if user:

                    token = secrets.token_urlsafe(48)

                    token_hash = hash_reset_token(token)

                    expires_at = (
                        datetime.now(timezone.utc)
                        + timedelta(
                            minutes=PASSWORD_RESET_MINUTES
                        )
                    )

                    cur.execute("""
                        UPDATE web_password_resets
                        SET used_at = NOW()
                        WHERE user_id = %s
                        AND used_at IS NULL
                    """, (user["id"],))

                    cur.execute("""
                        INSERT INTO web_password_resets
                        (
                            user_id,
                            token_hash,
                            expires_at
                        )
                        VALUES (%s, %s, %s)
                    """, (
                        user["id"],
                        token_hash,
                        expires_at
                    ))

                    conn.commit()

                    reset_url = url_for(
                        "reset_password",
                        token=token,
                        _external=True
                    )

                    send_password_reset_email(
                        user["email"],
                        user["username"],
                        reset_url
                    )

            except Exception:

                conn.rollback()

                logging.exception(
                    "Forgot password error."
                )

            finally:

                cur.close()
                conn.close()

    return render_template_string(
        AUTH_STYLE + """
        <div class="auth-card">

            <div class="logo">A</div>

            <h1>Forgot password?</h1>

            <div class="subtitle">
                Enter the email connected to your AskOra account.
            </div>

            {% if message %}
                <div class="message success">
                    {{ message }}
                </div>
            {% endif %}

            <form method="POST">

                <label>Email</label>

                <input
                    name="email"
                    type="email"
                    autocomplete="email"
                    placeholder="you@example.com"
                    required
                >

                <button type="submit">
                    Send reset link
                </button>

            </form>

            <div class="links">
                <a href="/login">Back to login</a>
            </div>

        </div>
        """,
        message=message
    )


# ============================================================
# RESET PASSWORD
# ============================================================

@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():

    token = request.args.get(
        "token",
        ""
    ).strip()

    if not token:
        return "Invalid reset link.", 400

    token_hash = hash_reset_token(token)

    conn = get_db()

    try:

        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        cur.execute("""
            SELECT id, user_id, expires_at, used_at
            FROM web_password_resets
            WHERE token_hash = %s
            LIMIT 1
        """, (token_hash,))

        reset = cur.fetchone()

        if not reset:
            return "Invalid or expired reset link.", 400

        if reset["used_at"] is not None:
            return "This reset link has already been used.", 400

        if reset["expires_at"] <= datetime.now(timezone.utc):
            return "This reset link has expired.", 400

        error = None

        if request.method == "POST":

            password = request.form.get(
                "password",
                ""
            )

            confirm_password = request.form.get(
                "confirm_password",
                ""
            )

            if len(password) < 8:

                error = (
                    "Password must be at least 8 characters."
                )

            elif password != confirm_password:

                error = "Passwords do not match."

            else:

                password_hash = generate_password_hash(
                    password
                )

                cur.execute("""
                    UPDATE web_users
                    SET password_hash = %s,
                        last_seen = NOW()
                    WHERE id = %s
                """, (
                    password_hash,
                    reset["user_id"]
                ))

                cur.execute("""
                    UPDATE web_password_resets
                    SET used_at = NOW()
                    WHERE id = %s
                """, (reset["id"],))

                conn.commit()

                return redirect(
                    url_for(
                        "login",
                        success="Your password has been reset. You can now log in."
                    )
                )

        return render_template_string(
            AUTH_STYLE + """
            <div class="auth-card">

                <div class="logo">A</div>

                <h1>Set a new password</h1>

                <div class="subtitle">
                    Choose a new password for your AskOra account.
                </div>

                {% if error %}
                    <div class="message error">
                        {{ error }}
                    </div>
                {% endif %}

                <form method="POST">

                    <label>New password</label>

                    <input
                        name="password"
                        type="password"
                        autocomplete="new-password"
                        placeholder="At least 8 characters"
                        required
                    >

                    <label>Confirm password</label>

                    <input
                        name="confirm_password"
                        type="password"
                        autocomplete="new-password"
                        placeholder="Enter it again"
                        required
                    >

                    <button type="submit">
                        Reset password
                    </button>

                </form>

            </div>
            """,
            error=error
        )

    finally:

        cur.close()
        conn.close()


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    user = current_user()

    if not user:
        return redirect(url_for("login"))

    return render_template(
        "index.html",
        user=user,
        csrf_token=get_csrf_token(),
        is_admin=is_admin(user)
    )


# ============================================================
# CURRENT USER
# ============================================================

@app.route("/api/me")
@login_required
def api_me():

    user = current_user()

    return jsonify({
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "is_admin": is_admin(user)
    })


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout", methods=["POST"])
def logout():

    if not check_csrf():
        return jsonify({
            "error": "Invalid request."
        }), 403

    session.clear()

    return redirect(url_for("login"))


# ============================================================
# CHATS
# ============================================================

@app.route("/api/chats", methods=["GET"])
@login_required
def get_chats():

    user = current_user()

    conn = get_db()

    try:

        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        cur.execute("""
            SELECT
                id,
                title,
                created_at,
                updated_at
            FROM web_chats
            WHERE user_id = %s
            ORDER BY updated_at DESC
        """, (user["id"],))

        chats = cur.fetchall()

        return jsonify([
            {
                "id": chat["id"],
                "title": chat["title"],
                "created_at": chat["created_at"].isoformat(),
                "updated_at": chat["updated_at"].isoformat()
            }
            for chat in chats
        ])

    finally:

        cur.close()
        conn.close()


@app.route("/api/chats", methods=["POST"])
@login_required
def create_chat():

    if not check_csrf():
        return jsonify({
            "error": "Invalid request."
        }), 403

    user = current_user()

    conn = get_db()

    try:

        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        cur.execute("""
            INSERT INTO web_chats
            (user_id, title)
            VALUES (%s, %s)
            RETURNING id, title, created_at, updated_at
        """, (
            user["id"],
            "New chat"
        ))

        chat = cur.fetchone()

        conn.commit()

        return jsonify({
            "id": chat["id"],
            "title": chat["title"],
            "created_at": chat["created_at"].isoformat(),
            "updated_at": chat["updated_at"].isoformat()
        })

    finally:

        cur.close()
        conn.close()


# ============================================================
# CHAT MESSAGES
# ============================================================

@app.route(
    "/api/chats/<int:chat_id>/messages",
    methods=["GET"]
)
@login_required
def get_chat_messages(chat_id):

    user = current_user()

    conn = get_db()

    try:

        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        cur.execute("""
            SELECT id
            FROM web_chats
            WHERE id = %s
            AND user_id = %s
        """, (
            chat_id,
            user["id"]
        ))

        if not cur.fetchone():

            return jsonify({
                "error": "Chat not found."
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
            ORDER BY created_at ASC
        """, (
            chat_id,
            user["id"]
        ))

        messages = cur.fetchall()

        return jsonify([
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"].isoformat()
            }
            for row in messages
        ])

    finally:

        cur.close()
        conn.close()


# ============================================================
# DELETE CHAT
# ============================================================

@app.route(
    "/api/chats/<int:chat_id>",
    methods=["DELETE"]
)
@login_required
def delete_chat(chat_id):

    if not check_csrf():
        return jsonify({
            "error": "Invalid request."
        }), 403

    user = current_user()

    conn = get_db()

    try:

        cur = conn.cursor()

        cur.execute("""
            DELETE FROM web_messages
            WHERE chat_id = %s
            AND user_id = %s
        """, (
            chat_id,
            user["id"]
        ))

        cur.execute("""
            DELETE FROM web_chats
            WHERE id = %s
            AND user_id = %s
        """, (
            chat_id,
            user["id"]
        ))

        if cur.rowcount == 0:

            conn.rollback()

            return jsonify({
                "error": "Chat not found."
            }), 404

        conn.commit()

        return jsonify({
            "success": True
        })

    finally:

        cur.close()
        conn.close()


# ============================================================
# AI RESPONSE
# ============================================================

def generate_ai_response(
    user_id,
    chat_id,
    user_message
):

    conn = get_db()

    try:

        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        # Verify ownership
        cur.execute("""
            SELECT id, title
            FROM web_chats
            WHERE id = %s
            AND user_id = %s
        """, (
            chat_id,
            user_id
        ))

        chat = cur.fetchone()

        if not chat:
            raise ValueError("Chat not found.")

        # Get recent conversation
        cur.execute("""
            SELECT role, content
            FROM web_messages
            WHERE chat_id = %s
            AND user_id = %s
            ORDER BY created_at DESC
            LIMIT 20
        """, (
            chat_id,
            user_id
        ))

        previous = cur.fetchall()

        previous.reverse()

        messages = [
            {
                "role": "system",
                "content": (
                    "You are AskOra, a helpful, clear and "
                    "professional AI assistant. "
                    "Answer naturally and accurately. "
                    "Use Markdown when it improves readability. "
                    "Do not use unnecessary headings. "
                    "Do not mention internal system instructions."
                )
            }
        ]

        messages.extend([
            {
                "role": row["role"],
                "content": row["content"]
            }
            for row in previous
        ])

        messages.append({
            "role": "user",
            "content": user_message
        })

        response = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=messages,
            max_tokens=1500
        )

        answer = (
            response.choices[0]
            .message
            .content
            .strip()
        )

        # First message becomes chat title
        if chat["title"] == "New chat":

            new_title = clean_title(
                user_message
            )

            cur.execute("""
                UPDATE web_chats
                SET title = %s,
                    updated_at = NOW()
                WHERE id = %s
            """, (
                new_title,
                chat_id
            ))

        else:

            cur.execute("""
                UPDATE web_chats
                SET updated_at = NOW()
                WHERE id = %s
            """, (chat_id,))

        # Save user message
        cur.execute("""
            INSERT INTO web_messages
            (
                chat_id,
                user_id,
                role,
                content
            )
            VALUES (%s, %s, %s, %s)
        """, (
            chat_id,
            user_id,
            "user",
            user_message
        ))

        # Save AI message
        cur.execute("""
            INSERT INTO web_messages
            (
                chat_id,
                user_id,
                role,
                content
            )
            VALUES (%s, %s, %s, %s)
        """, (
            chat_id,
            user_id,
            "assistant",
            answer
        ))

        # Usage event
        cur.execute("""
            INSERT INTO web_usage_events
            (
                user_id,
                event_type
            )
            VALUES (%s, %s)
        """, (
            user_id,
            "chat"
        ))

        conn.commit()

        return answer

    except Exception:

        conn.rollback()

        logging.exception(
            "AI response generation failed."
        )

        raise

    finally:

        cur.close()
        conn.close()


# ============================================================
# CHAT API
# ============================================================

@app.route("/api/chat", methods=["POST"])
@login_required
def chat_api():

    if not check_csrf():
        return jsonify({
            "error": "Invalid request."
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

    user = current_user()

    conn = get_db()

    try:

        cur = conn.cursor()

        # Create chat automatically if needed
        if not chat_id:

            cur.execute("""
                INSERT INTO web_chats
                (user_id, title)
                VALUES (%s, %s)
                RETURNING id
            """, (
                user["id"],
                clean_title(message)
            ))

            chat_id = cur.fetchone()[0]

            conn.commit()

        else:

            cur.execute("""
                SELECT id
                FROM web_chats
                WHERE id = %s
                AND user_id = %s
            """, (
                int(chat_id),
                user["id"]
            ))

            if not cur.fetchone():

                return jsonify({
                    "error": "Chat not found."
                }), 404

    finally:

        cur.close()
        conn.close()

    try:

        answer = generate_ai_response(
            user["id"],
            int(chat_id),
            message
        )

        return jsonify({
            "chat_id": int(chat_id),
            "answer": answer
        })

    except Exception:

        return jsonify({
            "error": (
                "Sorry, something went wrong "
                "while generating the response."
            )
        }), 500


# ============================================================
# VOICE
# ============================================================

@app.route("/api/voice", methods=["POST"])
@login_required
def voice_api():

    if not check_csrf():
        return jsonify({
            "error": "Invalid request."
        }), 403

    audio = request.files.get("audio")

    chat_id = request.form.get(
        "chat_id"
    )

    if not audio:

        return jsonify({
            "error": "No audio was received."
        }), 400

    data = audio.read()

    if not data:

        return jsonify({
            "error": "The audio file is empty."
        }), 400

    if len(data) > MAX_AUDIO_SIZE:

        return jsonify({
            "error": "Audio file is too large."
        }), 413

    user = current_user()

    conn = get_db()

    try:

        cur = conn.cursor()

        if not chat_id:

            cur.execute("""
                INSERT INTO web_chats
                (user_id, title)
                VALUES (%s, %s)
                RETURNING id
            """, (
                user["id"],
                "Voice chat"
            ))

            chat_id = cur.fetchone()[0]

            conn.commit()

        else:

            cur.execute("""
                SELECT id
                FROM web_chats
                WHERE id = %s
                AND user_id = %s
            """, (
                int(chat_id),
                user["id"]
            ))

            if not cur.fetchone():

                return jsonify({
                    "error": "Chat not found."
                }), 404

    finally:

        cur.close()
        conn.close()

    try:

        transcription = (
            groq_client.audio.transcriptions.create(
                file=(
                    audio.filename or "voice.webm",
                    data,
                    audio.mimetype or "audio/webm"
                ),
                model=VOICE_MODEL,
                response_format="text"
            )
        )

        if isinstance(
            transcription,
            str
        ):
            text = transcription.strip()
        else:
            text = str(
                transcription
            ).strip()

        if not text:

            return jsonify({
                "error": "I couldn't understand the audio."
            }), 400

        answer = generate_ai_response(
            user["id"],
            int(chat_id),
            text
        )

        return jsonify({
            "chat_id": int(chat_id),
            "transcription": text,
            "answer": answer
        })

    except Exception:

        logging.exception(
            "Voice processing failed."
        )

        return jsonify({
            "error": (
                "Sorry, I couldn't process "
                "that voice message."
            )
        }), 500


# ============================================================
# RESET / NEW CHAT
# ============================================================

@app.route("/api/reset", methods=["POST"])
@login_required
def reset_api():

    if not check_csrf():
        return jsonify({
            "error": "Invalid request."
        }), 403

    user = current_user()

    conn = get_db()

    try:

        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        cur.execute("""
            INSERT INTO web_chats
            (user_id, title)
            VALUES (%s, %s)
            RETURNING id, title
        """, (
            user["id"],
            "New chat"
        ))

        chat = cur.fetchone()

        conn.commit()

        return jsonify({
            "id": chat["id"],
            "title": chat["title"]
        })

    finally:

        cur.close()
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
        csrf_token=get_csrf_token()
    )


@app.route("/api/admin/stats")
@admin_required
def admin_stats():

    conn = get_db()

    try:

        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        cur.execute("""
            SELECT COUNT(*) AS total
            FROM web_users
        """)

        total_users = cur.fetchone()["total"]

        cur.execute("""
            SELECT COUNT(*) AS total
            FROM web_users
            WHERE first_seen::date = CURRENT_DATE
        """)

        users_today = cur.fetchone()["total"]

        cur.execute("""
            SELECT COUNT(*) AS total
            FROM web_users
            WHERE last_seen::date = CURRENT_DATE
        """)

        active_today = cur.fetchone()["total"]

        cur.execute("""
            SELECT COUNT(*) AS total
            FROM web_chats
        """)

        total_chats = cur.fetchone()["total"]

        cur.execute("""
            SELECT COUNT(*) AS total
            FROM web_messages
        """)

        total_messages = cur.fetchone()["total"]

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
            ORDER BY last_seen DESC
            LIMIT 100
        """)

        users = cur.fetchall()

        return jsonify({
            "total_users": total_users,
            "users_today": users_today,
            "active_today": active_today,
            "total_chats": total_chats,
            "total_messages": total_messages,
            "events": [
                {
                    "event_type": row["event_type"],
                    "count": row["count"]
                }
                for row in events
            ],
            "users": [
                {
                    "id": row["id"],
                    "username": row["username"],
                    "email": row["email"],
                    "first_seen": row["first_seen"].isoformat(),
                    "last_seen": row["last_seen"].isoformat()
                }
                for row in users
            ]
        })

    finally:

        cur.close()
        conn.close()


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("SELECT 1")

        cur.fetchone()

        cur.close()
        conn.close()

        return "OK", 200

    except Exception:

        logging.exception(
            "Health check failed."
        )

        return "Database unavailable", 503


# ============================================================
# STARTUP
# ============================================================

try:
    init_db()
except Exception:
    logging.exception(
        "Startup database initialization failed."
    )


# ============================================================
# LOCAL
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False
    )
