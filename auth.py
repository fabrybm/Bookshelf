import bcrypt
from functools import wraps
from flask import session, redirect, url_for, flash
from db import db


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


def get_current_user():
    if "user_id" not in session:
        return None
    with db() as conn:
        return conn.execute(
            "SELECT id, username, display_name FROM users WHERE id = ?",
            (session["user_id"],)
        ).fetchone()


def register_user(username: str, password: str, display_name: str):
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username.lower(),)
        ).fetchone()
        if existing:
            return None, "Username already taken"
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, display_name) VALUES (?, ?, ?)",
                (username.lower(), hash_password(password), display_name or username)
            )
            user = conn.execute(
                "SELECT id, username, display_name FROM users WHERE username = ?",
                (username.lower(),)
            ).fetchone()
            return user, None
        except Exception as e:
            return None, str(e)


def login_user(username: str, password: str):
    with db() as conn:
        user = conn.execute(
            "SELECT id, username, password_hash, display_name FROM users WHERE username = ?",
            (username.lower(),)
        ).fetchone()
        if not user or not check_password(password, user["password_hash"]):
            return None, "Invalid username or password"
        return user, None
