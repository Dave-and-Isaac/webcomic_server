import hashlib
import os
import secrets
from dataclasses import dataclass
from typing import Iterable

from .db import db

PBKDF2_ALG = "sha256"
PBKDF2_ITERS = 120_000


@dataclass(frozen=True)
class User:
    id: int
    username: str
    password_hash: str
    is_admin: bool
    must_change_password: bool
    theme: str
    keyboard_enabled: bool
    default_view: str
    avatar: str | None


def _hash_password_raw(password: str, salt: bytes) -> str:
    dk = hashlib.pbkdf2_hmac(PBKDF2_ALG, password.encode("utf-8"), salt, PBKDF2_ITERS)
    return dk.hex()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = _hash_password_raw(password, salt)
    return f"pbkdf2_{PBKDF2_ALG}${PBKDF2_ITERS}${salt.hex()}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt_hex, digest = stored.split("$", 3)
    except ValueError:
        return False
    if not scheme.startswith("pbkdf2_"):
        return False
    try:
        iters_i = int(iters)
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac(PBKDF2_ALG, password.encode("utf-8"), salt, iters_i).hex()
    return secrets.compare_digest(calc, digest)


def ensure_admin_user() -> None:
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE username=?",
            ("admin",),
        ).fetchone()
        if row:
            return
        conn.execute(
            """
            INSERT INTO users(username, password_hash, is_admin, must_change_password)
            VALUES (?, ?, 1, 1)
            """,
            ("admin", hash_password("admin")),
        )


def get_user_by_username(username: str) -> User | None:
    with db() as conn:
        row = conn.execute(
            """
            SELECT id, username, password_hash, is_admin, must_change_password, theme, keyboard_enabled, default_view, avatar
            FROM users
            WHERE username=?
            """,
            (username,),
        ).fetchone()
        if not row:
            return None
        return User(
            int(row["id"]),
            row["username"],
            row["password_hash"],
            bool(row["is_admin"]),
            bool(row["must_change_password"]),
            row["theme"],
            bool(row["keyboard_enabled"]),
            row["default_view"],
            row["avatar"],
        )


def get_user_by_id(user_id: int) -> User | None:
    with db() as conn:
        row = conn.execute(
            """
            SELECT id, username, password_hash, is_admin, must_change_password, theme, keyboard_enabled, default_view, avatar
            FROM users
            WHERE id=?
            """,
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return User(
            int(row["id"]),
            row["username"],
            row["password_hash"],
            bool(row["is_admin"]),
            bool(row["must_change_password"]),
            row["theme"],
            bool(row["keyboard_enabled"]),
            row["default_view"],
            row["avatar"],
        )


def create_user(username: str, password: str, is_admin: bool) -> User:
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO users(username, password_hash, is_admin, must_change_password, theme, keyboard_enabled, default_view, avatar)
            VALUES (?, ?, ?, 1, 'system', 1, 'read', NULL)
            """,
            (username, hash_password(password), 1 if is_admin else 0),
        )
        user_id = int(cur.lastrowid)
    return get_user_by_id(user_id)


def list_users() -> Iterable[User]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, username, password_hash, is_admin, must_change_password, theme, keyboard_enabled, default_view, avatar
            FROM users
            ORDER BY username COLLATE NOCASE
            """
        ).fetchall()
        return [
            User(
                int(r["id"]),
                r["username"],
                r["password_hash"],
                bool(r["is_admin"]),
                bool(r["must_change_password"]),
                r["theme"],
                bool(r["keyboard_enabled"]),
                r["default_view"],
                r["avatar"],
            )
            for r in rows
        ]


def update_password(user_id: int, new_password: str) -> None:
    with db() as conn:
        conn.execute(
            """
            UPDATE users
            SET password_hash=?, must_change_password=0
            WHERE id=?
            """,
            (hash_password(new_password), user_id),
        )


def update_theme(user_id: int, theme: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE users SET theme=? WHERE id=?",
            (theme, user_id),
        )


def update_reader_prefs(user_id: int, keyboard_enabled: bool, default_view: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE users SET keyboard_enabled=?, default_view=? WHERE id=?",
            (1 if keyboard_enabled else 0, default_view, user_id),
        )


def update_username(user_id: int, username: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE users SET username=? WHERE id=?",
            (username, user_id),
        )


def update_avatar(user_id: int, avatar: str | None) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE users SET avatar=? WHERE id=?",
            (avatar, user_id),
        )


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO sessions(user_id, token, created_at, last_seen)
            VALUES (?, ?, datetime('now'), datetime('now'))
            """,
            (user_id, token),
        )
    return token


def get_user_by_session(token: str) -> User | None:
    if not token:
        return None
    with db() as conn:
        row = conn.execute(
            """
            SELECT u.id, u.username, u.password_hash, u.is_admin, u.must_change_password, u.theme, keyboard_enabled, default_view, avatar
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token=?
            """,
            (token,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE sessions SET last_seen=datetime('now') WHERE token=?",
            (token,),
        )
        return User(
            int(row["id"]),
            row["username"],
            row["password_hash"],
            bool(row["is_admin"]),
            bool(row["must_change_password"]),
            row["theme"],
            bool(row["keyboard_enabled"]),
            row["default_view"],
            row["avatar"],
        )


def delete_session(token: str) -> None:
    if not token:
        return
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
