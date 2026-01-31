import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    import rarfile  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    rarfile = None

from .db import db

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ARCHIVE_EXTS = {".cbz", ".cbr", ".zip"}


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


def is_image_name(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_EXTS


def is_archive_file(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in ARCHIVE_EXTS


def list_images_in_dir(dir_path: Path) -> list[Path]:
    imgs = [p for p in dir_path.iterdir() if is_image_file(p)]
    # Sort in a predictable way (supports 001.jpg, 1.jpg, etc.)
    return sorted(imgs, key=lambda p: p.name.lower())


def list_images_in_archive(archive_path: Path) -> list[str]:
    suffix = archive_path.suffix.lower()
    try:
        if suffix in {".cbz", ".zip"}:
            with zipfile.ZipFile(archive_path) as zf:
                names = [
                    info.filename
                    for info in zf.infolist()
                    if not info.is_dir() and is_image_name(info.filename)
                ]
        elif suffix == ".cbr" and rarfile is not None:
            with rarfile.RarFile(archive_path) as rf:
                names = [
                    info.filename
                    for info in rf.infolist()
                    if not info.is_dir() and is_image_name(info.filename)
                ]
        else:
            names = []
    except Exception:
        names = []
    return sorted(names, key=lambda n: n.lower())


def read_archive_image(archive_path: Path, filename: str) -> bytes | None:
    suffix = archive_path.suffix.lower()
    try:
        if suffix in {".cbz", ".zip"}:
            with zipfile.ZipFile(archive_path) as zf:
                return zf.read(filename)
        if suffix == ".cbr" and rarfile is not None:
            with rarfile.RarFile(archive_path) as rf:
                return rf.read(filename)
    except Exception:
        return None
    return None


def detect_year_entries(comic_dir: Path) -> list[Path]:
    """Collect year folders that contain images and archive files in the series root."""
    year_entries: list[Path] = []
    for child in comic_dir.iterdir():
        if child.is_dir():
            if any(is_image_file(p) for p in child.iterdir()):
                year_entries.append(child)
        elif is_archive_file(child):
            year_entries.append(child)
    return sorted(year_entries, key=lambda p: p.name.lower())


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

    desired_comic_slugs = []
    for comic_dir in comic_dirs:
        desired_comic_slugs.append(slugify(comic_dir.name))

    with db() as conn:
        if desired_comic_slugs:
            placeholders = ",".join(["?"] * len(desired_comic_slugs))
            conn.execute(
                f"DELETE FROM comic WHERE slug NOT IN ({placeholders})",
                (*desired_comic_slugs,),
            )
        else:
            conn.execute("DELETE FROM comic")

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

            year_entries = detect_year_entries(comic_dir)
            years_to_upsert: list[tuple[str, str, str, int]] = []
            for idx, year_entry in enumerate(year_entries):
                if year_entry.is_dir():
                    title = year_entry.name
                    slug = slugify(title)
                else:
                    title = year_entry.stem
                    slug = slugify(year_entry.name)
                years_to_upsert.append((slug, title, str(year_entry), idx))

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
    if not p.exists():
        return []
    if p.is_dir():
        return [img.name for img in list_images_in_dir(p)]
    if is_archive_file(p):
        return list_images_in_archive(p)
    return []
