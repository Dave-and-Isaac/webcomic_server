import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .db import db

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


@dataclass(frozen=True)
class Comic:
    id: int
    slug: str
    title: str
    path: str


@dataclass(frozen=True)
class Year:
    id: int
    comic_id: int
    slug: str
    title: str
    path: str
    sort_index: int


def slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s.strip("-") or "untitled"


def is_image_file(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in IMAGE_EXTS


def list_images_in_dir(dir_path: Path) -> list[Path]:
    imgs = [p for p in dir_path.iterdir() if is_image_file(p)]
    # Sort in a predictable way (supports 001.jpg, 1.jpg, etc.)
    return sorted(imgs, key=lambda p: p.name.lower())


def detect_year_dirs(comic_dir: Path) -> list[Path]:
    """If comic_dir contains subdirectories that contain images, treat those as years."""
    year_dirs = []
    for child in sorted([p for p in comic_dir.iterdir() if p.is_dir()], key=lambda p: p.name.lower()):
        if any(is_image_file(p) for p in child.iterdir()):
            year_dirs.append(child)
    return year_dirs


def scan_comics(comics_root: Path) -> None:
    """
    Sync filesystem -> DB.
    Minimal rules:
      - Each direct subfolder of comics_root is a comic.
      - Each year is a subfolder under a series folder containing images.
    """
    comics_root = comics_root.resolve()
    comics_root.mkdir(parents=True, exist_ok=True)

    comic_dirs = [p for p in comics_root.iterdir() if p.is_dir()]
    comic_dirs.sort(key=lambda p: p.name.lower())

    with db() as conn:
        for comic_dir in comic_dirs:
            comic_title = comic_dir.name
            comic_slug = slugify(comic_title)

            # Upsert comic
            row = conn.execute(
                "SELECT id FROM comic WHERE slug=?", (comic_slug,)).fetchone()
            if row:
                comic_id = int(row["id"])
                conn.execute(
                    "UPDATE comic SET title=?, path=? WHERE id=?",
                    (comic_title, str(comic_dir), comic_id),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO comic(slug, title, path) VALUES(?,?,?)",
                    (comic_slug, comic_title, str(comic_dir)),
                )
                comic_id = int(cur.lastrowid)

            year_dirs = detect_year_dirs(comic_dir)

            years_to_upsert: list[tuple[str, str, str, int]] = []
            for idx, year_dir in enumerate(year_dirs):
                title = year_dir.name
                slug = slugify(title)
                years_to_upsert.append((slug, title, str(year_dir), idx))

            # Delete years that no longer exist on disk
            desired_slugs = [slug for (slug, _title, _path, _idx) in years_to_upsert]

            if desired_slugs:
                placeholders = ",".join(["?"] * len(desired_slugs))
                conn.execute(
                    f"""
                    DELETE FROM chapter
                    WHERE comic_id = ?
                      AND slug NOT IN ({placeholders})
                    """,
                    (comic_id, *desired_slugs),
                )
            else:
                conn.execute(
                    "DELETE FROM chapter WHERE comic_id = ?",
                    (comic_id,),
                )

            # Upsert years, keep it simple: insert if missing, update if exists.
            for slug, title, path, sort_idx in years_to_upsert:
                row = conn.execute(
                    "SELECT id FROM chapter WHERE comic_id=? AND slug=?",
                    (comic_id, slug),
                ).fetchone()
                if row:
                    year_id = int(row["id"])
                    conn.execute(
                        "UPDATE chapter SET title=?, path=?, sort_index=? WHERE id=?",
                        (title, path, sort_idx, year_id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO chapter(comic_id, slug, title, path, sort_index) VALUES(?,?,?,?,?)",
                        (comic_id, slug, title, path, sort_idx),
                    )


def get_comics() -> list[Comic]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, slug, title, path FROM comic ORDER BY title COLLATE NOCASE").fetchall()
        return [Comic(int(r["id"]), r["slug"], r["title"], r["path"]) for r in rows]


def get_comic_by_slug(slug: str) -> Comic | None:
    with db() as conn:
        r = conn.execute(
            "SELECT id, slug, title, path FROM comic WHERE slug=?", (slug,)).fetchone()
        if not r:
            return None
        return Comic(int(r["id"]), r["slug"], r["title"], r["path"])


def get_years_for_comic(comic_id: int) -> list[Year]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, comic_id, slug, title, path, sort_index
            FROM chapter
            WHERE comic_id=?
            ORDER BY sort_index ASC, title COLLATE NOCASE ASC
            """,
            (comic_id,),
        ).fetchall()
        return [Year(int(r["id"]), int(r["comic_id"]), r["slug"], r["title"], r["path"], int(r["sort_index"])) for r in rows]


def get_year_by_slugs(comic_id: int, year_slug: str) -> Year | None:
    with db() as conn:
        r = conn.execute(
            """
            SELECT id, comic_id, slug, title, path, sort_index
            FROM chapter
            WHERE comic_id=? AND slug=?
            """,
            (comic_id, year_slug),
        ).fetchone()
        if not r:
            return None
        return Year(
            int(r["id"]),
            int(r["comic_id"]),
            r["slug"],
            r["title"],
            r["path"],
            int(r["sort_index"]),
        )


def get_year_images(year_path: str) -> list[str]:
    p = Path(year_path)
    if not p.exists() or not p.is_dir():
        return []
    return [img.name for img in list_images_in_dir(p)]
