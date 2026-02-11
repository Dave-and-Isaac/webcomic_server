import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path("data") / "app.db"


def ensure_db_dir() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    ensure_db_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def db() -> sqlite3.Connection:
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS comic (
              id INTEGER PRIMARY KEY,
              slug TEXT NOT NULL UNIQUE,
              title TEXT NOT NULL,
              path TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chapter (
              id INTEGER PRIMARY KEY,
              comic_id INTEGER NOT NULL,
              slug TEXT NOT NULL,
              title TEXT NOT NULL,
              path TEXT NOT NULL,
              sort_index INTEGER NOT NULL DEFAULT 0,
              page_count INTEGER NOT NULL DEFAULT 0,
              UNIQUE(comic_id, slug),
              FOREIGN KEY (comic_id) REFERENCES comic(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS progress (
              id INTEGER PRIMARY KEY,
              comic_id INTEGER NOT NULL,
              chapter_id INTEGER NOT NULL,
              page_index INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL DEFAULT (datetime('now')),
              UNIQUE(comic_id, chapter_id),
              FOREIGN KEY (comic_id) REFERENCES comic(id) ON DELETE CASCADE,
              FOREIGN KEY (chapter_id) REFERENCES chapter(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY,
              username TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL,
              is_admin INTEGER NOT NULL DEFAULT 0,
              must_change_password INTEGER NOT NULL DEFAULT 1,
              theme TEXT NOT NULL DEFAULT 'system',
              keyboard_enabled INTEGER NOT NULL DEFAULT 1,
              default_view TEXT NOT NULL DEFAULT 'read',
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS sessions (
              id INTEGER PRIMARY KEY,
              user_id INTEGER NOT NULL,
              token TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              last_seen TEXT NOT NULL DEFAULT (datetime('now')),
              FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )
        ensure_user_theme_column(conn)
        ensure_user_keyboard_column(conn)
        ensure_user_default_view_column(conn)
        ensure_user_avatar_column(conn)
        ensure_chapter_page_count_column(conn)


def ensure_user_theme_column(conn: sqlite3.Connection) -> None:
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "theme" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN theme TEXT NOT NULL DEFAULT 'system'")


def ensure_user_theme_mode_column(conn: sqlite3.Connection) -> None:
    # Legacy no-op: theme_mode removed, but keep for backward compatibility.
    pass


def ensure_user_keyboard_column(conn: sqlite3.Connection) -> None:
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "keyboard_enabled" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN keyboard_enabled INTEGER NOT NULL DEFAULT 1")


def ensure_user_default_view_column(conn: sqlite3.Connection) -> None:
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "default_view" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN default_view TEXT NOT NULL DEFAULT 'read'")


def ensure_user_avatar_column(conn: sqlite3.Connection) -> None:
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "avatar" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN avatar TEXT")


def ensure_chapter_page_count_column(conn: sqlite3.Connection) -> None:
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(chapter)").fetchall()]
    if "page_count" not in cols:
        conn.execute("ALTER TABLE chapter ADD COLUMN page_count INTEGER NOT NULL DEFAULT 0")


def get_setting(key: str) -> str | None:
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,),
        ).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value)
            VALUES(?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )


def delete_setting(key: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))


def upsert_progress(comic_id: int, year_id: int, page_index: int) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO progress(comic_id, chapter_id, page_index, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(comic_id, chapter_id)
            DO UPDATE SET
              page_index=excluded.page_index,
              updated_at=datetime('now');
            """,
            (comic_id, year_id, page_index),
        )


def get_progress_page_index(comic_id: int, year_id: int) -> int | None:
    with db() as conn:
        row = conn.execute(
            "SELECT page_index FROM progress WHERE comic_id=? AND chapter_id=?",
            (comic_id, year_id),
        ).fetchone()
        return int(row["page_index"]) if row else None


def get_last_read_for_comic(comic_id: int) -> dict | None:
    """
    Returns the most recently updated progress row for a comic, including year slug and page_index.
    """
    with db() as conn:
        row = conn.execute(
            """
            SELECT
              p.page_index,
              p.updated_at,
              c.slug AS year_slug,
              c.title AS year_title
            FROM progress p
            JOIN chapter c ON c.id = p.chapter_id
            WHERE p.comic_id = ?
            ORDER BY p.updated_at DESC
            LIMIT 1
            """,
            (comic_id,),
        ).fetchone()

        if not row:
            return None

        return {
            "year_slug": row["year_slug"],
            "year_title": row["year_title"],
            "page_index": int(row["page_index"]),  # 0-based
            "updated_at": row["updated_at"],
        }


def get_last_read_all_comics() -> dict[int, dict]:
    """
    Returns {comic_id: last_read_dict} for all comics that have any progress.
    """
    with db() as conn:
        rows = conn.execute(
            """
            SELECT
              p.comic_id,
              p.page_index,
              p.updated_at,
              c.slug AS year_slug,
              c.title AS year_title
            FROM progress p
            JOIN chapter c ON c.id = p.chapter_id
            JOIN (
              SELECT comic_id, MAX(updated_at) AS max_updated
              FROM progress
              GROUP BY comic_id
            ) latest
              ON latest.comic_id = p.comic_id
             AND latest.max_updated = p.updated_at
            """
        ).fetchall()

        out: dict[int, dict] = {}
        for r in rows:
            out[int(r["comic_id"])] = {
                "year_slug": r["year_slug"],
                "year_title": r["year_title"],
                "page_index": int(r["page_index"]),
                "updated_at": r["updated_at"],
            }
        return out


def delete_progress_for_year(comic_id: int, year_id: int) -> None:
    with db() as conn:
        conn.execute(
            "DELETE FROM progress WHERE comic_id=? AND chapter_id=?",
            (comic_id, year_id),
        )


def delete_progress_for_comic(comic_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM progress WHERE comic_id=?", (comic_id,))


def delete_all_progress() -> None:
    with db() as conn:
        conn.execute("DELETE FROM progress")
