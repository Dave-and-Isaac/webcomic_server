import mimetypes
import re
import os
import time
import logging
from pathlib import Path, PurePosixPath
from urllib.parse import quote_plus

from fastapi import FastAPI, Request, HTTPException, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from .db import (
    init_db,
    upsert_progress,
    get_progress_page_index,
    get_last_read_all_comics,
    get_last_read_for_comic,
    get_setting,
    set_setting,
    delete_setting,
    delete_progress_for_year,
    delete_progress_for_comic,
    delete_all_progress,
)
from .auth import (
    ensure_admin_user,
    get_user_by_id,
    get_user_by_session,
    get_user_by_username,
    create_session,
    delete_session,
    create_user,
    list_users,
    update_password,
    verify_password,
    update_theme,
    update_reader_prefs,
    update_username,
    update_avatar,
    update_user_adult_access,
)
from .config import ensure_config, load_series_config, save_series_config, POSTERS_DIR, LOGOS_DIR, AVATARS_DIR

from .library import (
    scan_comics,
    get_comics,
    get_comic_by_slug,
    get_years_for_comic,
    get_year_by_slugs,
    get_year_images,
    is_archive_file,
    is_image_name,
    read_archive_image,
    is_pdf_file,
    get_pdf_page_count,
    render_pdf_page,
)

APP_ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(APP_ROOT / "templates"))
PDF_CACHE_DIR = Path("data") / "pdf_cache"

COMICS_ENV_VAR = "COMICS_DIR"
COMICS_SETTING_KEY = "comics_dir"
DEFAULT_COMICS_DIR = "comics"
_version_path = APP_ROOT.parent / "VERSION"
if _version_path.exists():
    APP_VERSION = _version_path.read_text(encoding="utf-8").strip() or "dev"
else:
    APP_VERSION = "dev"

app = FastAPI(title="StripStash")
logger = logging.getLogger(__name__)

app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.on_event("startup")
def _startup():
    init_db()
    ensure_admin_user()
    ensure_config()
    comics_dir, _source = _get_active_comics_dir()
    _scan_and_record(comics_dir)


def _get_active_comics_dir() -> tuple[Path, str]:
    saved = get_setting(COMICS_SETTING_KEY)
    if saved:
        return (Path(os.path.expanduser(saved)).resolve(), "saved")
    env = os.environ.get(COMICS_ENV_VAR)
    if env:
        return (Path(os.path.expanduser(env)).resolve(), "env")
    return (Path(DEFAULT_COMICS_DIR).resolve(), "default")


def _get_current_user(request: Request):
    token = request.cookies.get("session")
    return get_user_by_session(token) if token else None


def _render(request: Request, template_name: str, context: dict):
    context = dict(context)
    user = getattr(request.state, "user", None)
    context["current_user"] = user
    context["must_change_password"] = bool(user and user.must_change_password)
    context["theme"] = (user.theme if user and user.theme else "system")
    context["keyboard_enabled"] = bool(user and user.keyboard_enabled)
    context["default_view"] = (user.default_view if user and user.default_view else "read")
    context["active_path"] = request.url.path
    if user and user.avatar:
        context["avatar_url"] = f"/config/avatars/{user.avatar}"
    else:
        context["avatar_url"] = None
    context["app_version"] = APP_VERSION
    return TEMPLATES.TemplateResponse(template_name, context)


def _scan_and_record(comics_dir: Path) -> tuple[bool, str | None]:
    start = time.perf_counter()
    set_setting("scan_last_started", str(time.time()))
    try:
        scan_comics(comics_dir)
    except Exception as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        set_setting("scan_duration_ms", str(duration_ms))
        set_setting("scan_last_error", str(exc))
        logger.exception("Scan failed for comics_dir=%s", comics_dir)
        return (False, str(exc))
    duration_ms = int((time.perf_counter() - start) * 1000)
    set_setting("scan_last_completed", str(time.time()))
    set_setting("scan_duration_ms", str(duration_ms))
    delete_setting("scan_last_error")
    return (True, None)


def _series_meta(series_cfg: dict, comic_slug: str) -> dict:
    if not isinstance(series_cfg, dict):
        return {}
    meta = series_cfg.get(comic_slug, {})
    return meta if isinstance(meta, dict) else {}


def _is_adult_series(series_cfg: dict, comic_slug: str) -> bool:
    return bool(_series_meta(series_cfg, comic_slug).get("adult", False))


def _user_can_view_comic(user, series_cfg: dict, comic_slug: str) -> bool:
    if user is None:
        return False
    if bool(user.allow_adult_content):
        return True
    return not _is_adult_series(series_cfg, comic_slug)


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path == "/login":
        return await call_next(request)

    user = _get_current_user(request)
    request.state.user = user
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None):
    if _get_current_user(request):
        return RedirectResponse(url="/home", status_code=303)
    return _render(
        request,
        "login.html",
        {"request": request, "error": error},
    )


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = get_user_by_username(username.strip())
    if not user or not verify_password(password, user.password_hash):
        return RedirectResponse(url="/login?error=Invalid+credentials", status_code=303)
    token = create_session(user.id)
    response = RedirectResponse(url="/home", status_code=303)
    response.set_cookie("session", token, httponly=True, samesite="lax")
    return response


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get("session")
    delete_session(token)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session")
    return response


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, error: str | None = None, success: str | None = None):
    return _render(
        request,
        "settings.html",
        {"request": request, "error": error, "success": success},
    )


@app.post("/settings/theme")
def update_theme_setting(request: Request, theme: str = Form(...)):
    user = request.state.user
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    theme = theme.strip().lower()
    if theme not in {"system", "dark", "light"}:
        return RedirectResponse(url="/settings?error=Invalid+theme", status_code=303)
    update_theme(user.id, theme)
    return RedirectResponse(url="/settings?success=Theme+updated", status_code=303)


@app.post("/settings/reader")
def update_reader_settings(
    request: Request,
    keyboard_enabled: str | None = Form(None),
    default_view: str = Form("read"),
):
    user = request.state.user
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    default_view = default_view.strip().lower()
    if default_view not in {"read", "browse"}:
        return RedirectResponse(url="/settings?error=Invalid+default+view", status_code=303)
    update_reader_prefs(user.id, bool(keyboard_enabled), default_view)
    return RedirectResponse(url="/settings?success=Reader+settings+updated", status_code=303)


@app.post("/settings/reset-progress")
def reset_all_progress(request: Request):
    user = request.state.user
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    delete_all_progress()
    return RedirectResponse(url="/settings?success=Progress+reset", status_code=303)


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, error: str | None = None, success: str | None = None):
    return _render(
        request,
        "profile.html",
        {"request": request, "error": error, "success": success},
    )


@app.post("/profile/username")
def profile_update_username(request: Request, username: str = Form(...)):
    user = request.state.user
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    cleaned = username.strip()
    if not cleaned:
        return RedirectResponse(url="/profile?error=Username+required", status_code=303)
    existing = get_user_by_username(cleaned)
    if existing and existing.id != user.id:
        return RedirectResponse(url="/profile?error=Username+already+taken", status_code=303)
    update_username(user.id, cleaned)
    return RedirectResponse(url="/profile?success=Username+updated", status_code=303)


@app.post("/profile/password")
def profile_update_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    user = request.state.user
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not verify_password(current_password, user.password_hash):
        return RedirectResponse(url="/profile?error=Current+password+incorrect", status_code=303)
    if not new_password or new_password != confirm_password:
        return RedirectResponse(url="/profile?error=Passwords+do+not+match", status_code=303)
    update_password(user.id, new_password)
    return RedirectResponse(url="/profile?success=Password+updated", status_code=303)


@app.post("/profile/avatar")
def profile_update_avatar(request: Request, avatar: UploadFile = File(...)):
    user = request.state.user
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not avatar or not avatar.filename:
        return RedirectResponse(url="/profile?error=No+file+selected", status_code=303)
    ext = Path(avatar.filename).suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
        return RedirectResponse(url="/profile?error=Unsupported+file+type", status_code=303)
    filename = f"user-{user.id}-{int(time.time())}{ext}"
    file_path = (AVATARS_DIR / filename).resolve()
    if AVATARS_DIR not in file_path.parents:
        return RedirectResponse(url="/profile?error=Invalid+path", status_code=303)
    data = avatar.file.read()
    file_path.write_bytes(data)
    if user.avatar:
        old_path = (AVATARS_DIR / user.avatar).resolve()
        if AVATARS_DIR in old_path.parents and old_path.exists():
            old_path.unlink()
    update_avatar(user.id, filename)
    return RedirectResponse(url="/profile?success=Avatar+updated", status_code=303)


@app.post("/profile/avatar/remove")
def profile_remove_avatar(request: Request):
    user = request.state.user
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if user.avatar:
        old_path = (AVATARS_DIR / user.avatar).resolve()
        if AVATARS_DIR in old_path.parents and old_path.exists():
            old_path.unlink()
    update_avatar(user.id, None)
    return RedirectResponse(url="/profile?success=Avatar+removed", status_code=303)



@app.get("/series", response_class=HTMLResponse)
def library(request: Request, error: str | None = None, success: str | None = None):
    user = request.state.user
    comics = get_comics()
    last_reads = get_last_read_all_comics()
    series_cfg = load_series_config()

    # attach last_read per comic for template convenience
    comics_rows = []
    for c in comics:
        if not _user_can_view_comic(user, series_cfg, c.slug):
            continue
        lr = last_reads.get(c.id)
        meta = _series_meta(series_cfg, c.slug)
        display_title = meta.get("title") or c.title
        poster = meta.get("poster")
        poster_version = meta.get("poster_updated") if isinstance(meta, dict) else None
        poster_url = f"/config/posters/{poster}?v={poster_version}" if poster else None
        years = get_years_for_comic(c.id)
        year_count = len(years)
        total_images = sum(year.page_count for year in years)
        progress_pct = None
        if lr:
            years_by_slug = {year.slug: year for year in years}
            year = years_by_slug.get(lr["year_slug"])
            if year:
                year_pages = max(1, year.page_count)
                progress_pct = int(((lr["page_index"] + 1) / year_pages) * 100)
        comics_rows.append(
            {
                "comic": c,
                "last_read": lr,
                "poster_url": poster_url,
                "display_title": display_title,
                "is_adult": bool(meta.get("adult", False)),
                "year_count": year_count,
                "total_images": total_images,
                "progress_pct": progress_pct,
            }
        )

    return _render(
        request,
        "library.html",
        {
            "request": request,
            "comics": comics_rows,
            "error": error,
            "success": success,
            "app_version": APP_VERSION,
        },
    )


@app.get("/", response_class=HTMLResponse)
@app.get("/home", response_class=HTMLResponse)
def home(request: Request):
    user = request.state.user
    comics = get_comics()
    last_reads = get_last_read_all_comics()
    series_cfg = load_series_config()

    continue_items = []
    for c in comics:
        if not _user_can_view_comic(user, series_cfg, c.slug):
            continue
        lr = last_reads.get(c.id)
        if not lr:
            continue
        year = get_year_by_slugs(c.id, lr["year_slug"])
        if not year:
            continue
        meta = _series_meta(series_cfg, c.slug)
        display_title = meta.get("title") or c.title
        poster = meta.get("poster")
        poster_version = meta.get("poster_updated") if isinstance(meta, dict) else None
        poster_url = f"/config/posters/{poster}?v={poster_version}" if poster else None
        images = get_year_images(year.path)
        page_index = lr["page_index"]
        page_num = page_index + 1
        preview_url = None
        year_path = Path(year.path)
        if is_pdf_file(year_path):
            preview_url = f"/pdf-page/{c.slug}/{year.slug}/{page_num}?dpi=140"
        else:
            if images:
                filename = images[min(page_index, len(images) - 1)]
                preview_url = f"/asset/{c.slug}/{year.slug}/{filename}"
        continue_items.append(
            {
                "comic": c,
                "display_title": display_title,
                "poster_url": poster_url,
                "year_title": year.title,
                "page_num": page_num,
                "preview_url": preview_url,
                "resume_url": f"/read/{c.slug}/{year.slug}/{page_num}",
                "updated_at": lr.get("updated_at"),
            }
        )

    continue_items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)

    return _render(
        request,
        "home.html",
        {
            "request": request,
            "continue_items": continue_items[:8],
        },
    )


@app.post("/settings/comics-dir")
def update_comics_dir(request: Request, comics_dir: str = Form(...)):
    user = request.state.user
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    cleaned = comics_dir.strip()
    if cleaned:
        set_setting(COMICS_SETTING_KEY, cleaned)
    else:
        delete_setting(COMICS_SETTING_KEY)
    active_dir, _source = _get_active_comics_dir()
    ok, err = _scan_and_record(active_dir)
    if ok:
        return RedirectResponse(url="/admin?success=Comics+directory+updated", status_code=303)
    return RedirectResponse(url=f"/admin?error={quote_plus(f'Rescan failed: {err}')}", status_code=303)


@app.post("/settings/comics-dir/reset")
def reset_comics_dir(request: Request):
    user = request.state.user
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    delete_setting(COMICS_SETTING_KEY)
    active_dir, _source = _get_active_comics_dir()
    ok, err = _scan_and_record(active_dir)
    if ok:
        return RedirectResponse(url="/admin?success=Using+default+or+env+comics+directory", status_code=303)
    return RedirectResponse(url=f"/admin?error={quote_plus(f'Rescan failed: {err}')}", status_code=303)


@app.post("/rescan")
def rescan():
    comics_dir, _source = _get_active_comics_dir()
    ok, err = _scan_and_record(comics_dir)
    if ok:
        return RedirectResponse(url="/series?success=Rescan+completed", status_code=303)
    return RedirectResponse(url=f"/series?error={quote_plus(f'Rescan failed: {err}')}", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_panel(
    request: Request,
    section: str = "overview",
    error: str | None = None,
    success: str | None = None,
):
    user = request.state.user
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")

    comics_dir, comics_dir_source = _get_active_comics_dir()
    env_comics_dir = os.environ.get(COMICS_ENV_VAR)
    series_cfg = load_series_config()
    comics = get_comics()
    years_count = 0
    images_count = 0
    for comic in comics:
        years = get_years_for_comic(comic.id)
        years_count += len(years)
        images_count += sum(year.page_count for year in years)
    users = list_users()
    scan_last_started = get_setting("scan_last_started")
    scan_last_completed = get_setting("scan_last_completed")
    scan_duration_ms = get_setting("scan_duration_ms")
    scan_last_error = get_setting("scan_last_error")

    return _render(
        request,
        "admin.html",
        {
            "request": request,
            "comics_dir": str(comics_dir),
            "comics_dir_source": comics_dir_source,
            "env_comics_dir": env_comics_dir,
            "users": users,
            "section": section,
            "stats": {
                "series": len(comics),
                "years": years_count,
                "images": images_count,
                "users": len(users),
            },
            "scan": {
                "last_started": scan_last_started,
                "last_completed": scan_last_completed,
                "duration_ms": scan_duration_ms,
                "last_error": scan_last_error,
            },
            "series_cfg": series_cfg,
            "error": error,
            "success": success,
        },
    )


@app.post("/admin/users")
def admin_create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    is_admin: str | None = Form(None),
):
    user = request.state.user
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    uname = username.strip()
    if not uname:
        return RedirectResponse(url="/admin?error=Username+required", status_code=303)
    if not password:
        return RedirectResponse(url="/admin?error=Password+required", status_code=303)
    if get_user_by_username(uname):
        return RedirectResponse(url="/admin?error=Username+already+exists", status_code=303)
    create_user(uname, password, bool(is_admin))
    return RedirectResponse(url="/admin?success=User+created", status_code=303)


@app.get("/admin/series/{comic_slug}", response_class=HTMLResponse)
def admin_series_edit(request: Request, comic_slug: str, error: str | None = None, success: str | None = None):
    user = request.state.user
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    comic = get_comic_by_slug(comic_slug)
    if not comic:
        raise HTTPException(status_code=404, detail="Comic not found")
    series_cfg = load_series_config()
    meta = _series_meta(series_cfg, comic.slug)
    return _render(
        request,
        "admin_series.html",
        {
            "request": request,
            "comic": comic,
            "meta": meta,
            "error": error,
            "success": success,
        },
    )


@app.post("/admin/series/{comic_slug}")
def admin_series_update(
    request: Request,
    comic_slug: str,
    title: str = Form(""),
    year_range: str = Form(""),
    summary: str = Form(""),
    website: str = Form(""),
    adult_content: str | None = Form(None),
    remove_poster: str | None = Form(None),
    remove_logo: str | None = Form(None),
    poster: UploadFile | None = File(None),
    logo: UploadFile | None = File(None),
):
    user = request.state.user
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    comic = get_comic_by_slug(comic_slug)
    if not comic:
        raise HTTPException(status_code=404, detail="Comic not found")

    series_cfg = load_series_config()
    if not isinstance(series_cfg, dict):
        series_cfg = {}
    meta = series_cfg.get(comic.slug, {})

    cleaned_title = title.strip()
    cleaned_year_range = year_range.strip()
    cleaned_summary = summary.strip()
    cleaned_website = website.strip()

    if cleaned_title:
        meta["title"] = cleaned_title
    else:
        meta.pop("title", None)

    if cleaned_year_range:
        meta["year_range"] = cleaned_year_range
    else:
        meta.pop("year_range", None)

    if cleaned_summary:
        meta["summary"] = cleaned_summary
    else:
        meta.pop("summary", None)

    if cleaned_website:
        meta["website"] = cleaned_website
    else:
        meta.pop("website", None)

    if adult_content:
        meta["adult"] = True
    else:
        meta.pop("adult", None)

    if remove_poster:
        meta.pop("poster", None)
        meta.pop("poster_updated", None)
    if remove_logo:
        meta.pop("logo", None)
        meta.pop("logo_updated", None)

    if poster and poster.filename:
        name = poster.filename.lower()
        ext = os.path.splitext(name)[1]
        if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
            return RedirectResponse(url=f"/admin/series/{comic.slug}?error=Invalid+poster+type", status_code=303)
        safe_name = f"{comic.slug}{ext}"
        dest = (POSTERS_DIR / safe_name).resolve()
        if POSTERS_DIR not in dest.parents:
            raise HTTPException(status_code=400, detail="Invalid path")
        with dest.open("wb") as f:
            f.write(poster.file.read())
        meta["poster"] = safe_name
        meta["poster_updated"] = int(time.time())

    if logo and logo.filename:
        name = logo.filename.lower()
        ext = os.path.splitext(name)[1]
        if ext not in {".jpg", ".jpeg", ".png", ".webp", ".svg"}:
            return RedirectResponse(url=f"/admin/series/{comic.slug}?error=Invalid+logo+type", status_code=303)
        safe_name = f"{comic.slug}{ext}"
        dest = (LOGOS_DIR / safe_name).resolve()
        if LOGOS_DIR not in dest.parents:
            raise HTTPException(status_code=400, detail="Invalid path")
        with dest.open("wb") as f:
            f.write(logo.file.read())
        meta["logo"] = safe_name
        meta["logo_updated"] = int(time.time())

    if meta:
        series_cfg[comic.slug] = meta
    else:
        series_cfg.pop(comic.slug, None)

    save_series_config(series_cfg)
    return RedirectResponse(url=f"/admin/series/{comic.slug}?success=Series+updated", status_code=303)


@app.post("/admin/users/{user_id}/adult-access")
def admin_update_user_adult_access(request: Request, user_id: int, allow_adult_content: str | None = Form(None)):
    user = request.state.user
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    target = get_user_by_id(user_id)
    if not target:
        return RedirectResponse(url="/admin?section=users&error=User+not+found", status_code=303)
    update_user_adult_access(user_id, bool(allow_adult_content))
    return RedirectResponse(url="/admin?section=users&success=User+restrictions+updated", status_code=303)


@app.post("/admin/change-password")
def admin_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    user = request.state.user
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not verify_password(current_password, user.password_hash):
        target = "/admin" if user.is_admin else "/"
        return RedirectResponse(url=f"{target}?error=Current+password+incorrect", status_code=303)
    if not new_password or new_password != confirm_password:
        target = "/admin" if user.is_admin else "/"
        return RedirectResponse(url=f"{target}?error=Password+confirmation+does+not+match", status_code=303)
    update_password(user.id, new_password)
    target = "/admin" if user.is_admin else "/"
    return RedirectResponse(url=f"{target}?success=Password+updated", status_code=303)


@app.get("/comic/{comic_slug}", response_class=HTMLResponse)
def comic_page(request: Request, comic_slug: str, order: str = "asc"):
    comic = get_comic_by_slug(comic_slug)
    if not comic:
        raise HTTPException(status_code=404, detail="Comic not found")

    series_cfg = load_series_config()
    if not _user_can_view_comic(request.state.user, series_cfg, comic.slug):
        raise HTTPException(status_code=404, detail="Comic not found")

    years = get_years_for_comic(comic.id)
    meta = _series_meta(series_cfg, comic.slug)
    poster = meta.get("poster")
    logo = meta.get("logo")
    poster_version = meta.get("poster_updated") if isinstance(meta, dict) else None
    logo_version = meta.get("logo_updated") if isinstance(meta, dict) else None
    poster_url = f"/config/posters/{poster}?v={poster_version}" if poster else None
    logo_url = f"/config/logos/{logo}?v={logo_version}" if logo else None
    order = "desc" if order == "desc" else "asc"
    if order == "desc":
        years = list(reversed(years))

    # For each year, load progress (best-effort)
    year_rows = []
    for year in years:
        idx = get_progress_page_index(comic.id, year.id)
        year_rows.append({"year": year, "progress_page": (
            idx + 1) if idx is not None else None})  # 1-based display

    return _render(
        request,
        "chapters.html",
        {
            "request": request,
            "comic": comic,
            "years": year_rows,
            "order": order,
            "toggle_order": "desc" if order == "asc" else "asc",
            "meta": meta,
            "poster_url": poster_url,
            "logo_url": logo_url,
        },
    )


@app.get("/read/{comic_slug}/{year_slug}", response_class=HTMLResponse)
def read_resume(request: Request, comic_slug: str, year_slug: str):
    comic = get_comic_by_slug(comic_slug)
    if not comic:
        raise HTTPException(status_code=404, detail="Comic not found")
    series_cfg = load_series_config()
    if not _user_can_view_comic(request.state.user, series_cfg, comic.slug):
        raise HTTPException(status_code=404, detail="Comic not found")
    year = get_year_by_slugs(comic.id, year_slug)
    if not year:
        raise HTTPException(status_code=404, detail="Year not found")

    idx0 = get_progress_page_index(comic.id, year.id)
    page1 = (idx0 + 1) if idx0 is not None else 1
    return RedirectResponse(url=f"/read/{comic_slug}/{year_slug}/{page1}", status_code=303)


@app.get("/browse/{comic_slug}/{year_slug}", response_class=HTMLResponse)
def browse_year(request: Request, comic_slug: str, year_slug: str):
    comic = get_comic_by_slug(comic_slug)
    if not comic:
        raise HTTPException(status_code=404, detail="Comic not found")
    series_cfg = load_series_config()
    if not _user_can_view_comic(request.state.user, series_cfg, comic.slug):
        raise HTTPException(status_code=404, detail="Comic not found")
    year = get_year_by_slugs(comic.id, year_slug)
    if not year:
        raise HTTPException(status_code=404, detail="Year not found")

    year_path = Path(year.path)
    is_pdf = is_pdf_file(year_path)
    images = get_year_images(year.path) if not is_pdf else []
    pdf_page_count = get_pdf_page_count(year_path) if is_pdf else 0
    pdf_page_count_unknown = is_pdf and pdf_page_count == 0
    page_count = pdf_page_count if is_pdf else len(images)
    if is_pdf and page_count == 0:
        page_count = 1
    items = []
    pages = range(1, page_count + 1) if is_pdf else range(1, len(images) + 1)
    for page in pages:
        filename = f"Page {page}" if is_pdf else images[page - 1]
        items.append(
            {
                "page": page,
                "filename": filename,
                "thumb_url": (
                    f"/pdf-page/{comic.slug}/{year.slug}/{page}?dpi=120"
                    if is_pdf
                    else f"/asset/{comic.slug}/{year.slug}/{filename}"
                ),
                "read_url": f"/read/{comic.slug}/{year.slug}/{page}",
                "is_pdf": is_pdf,
            }
        )

    return _render(
        request,
        "year_grid.html",
        {
            "request": request,
            "comic": comic,
            "year": year,
            "items": items,
            "page_count": page_count,
            "is_pdf": is_pdf,
            "pdf_page_count_unknown": pdf_page_count_unknown,
        },
    )


@app.get("/read/{comic_slug}/{year_slug}/{page}", response_class=HTMLResponse)
def reader(request: Request, comic_slug: str, year_slug: str, page: int):
    comic = get_comic_by_slug(comic_slug)
    if not comic:
        raise HTTPException(status_code=404, detail="Comic not found")
    series_cfg = load_series_config()
    if not _user_can_view_comic(request.state.user, series_cfg, comic.slug):
        raise HTTPException(status_code=404, detail="Comic not found")
    year = get_year_by_slugs(comic.id, year_slug)
    if not year:
        raise HTTPException(status_code=404, detail="Year not found")

    year_path = Path(year.path)
    is_pdf = is_pdf_file(year_path)
    images = get_year_images(year.path) if not is_pdf else []
    pdf_page_count = get_pdf_page_count(year_path) if is_pdf else 0
    pdf_page_count_unknown = is_pdf and pdf_page_count == 0
    page_count = pdf_page_count if is_pdf else len(images)
    if is_pdf and page_count == 0:
        page_count = max(1, page)
    if not is_pdf and not images:
        return _render(
            request,
            "reader.html",
            {
                "request": request,
                "comic": comic,
                "year": year,
                "page": 1,
                "page_count": 0,
                "image_url": None,
                "page_filename": None,
                "is_pdf": is_pdf,
                "pdf_url": None,
                "view_mode": "single",
                "second_page": None,
                "pdf_page_count_unknown": pdf_page_count_unknown,
                "prev_url": None,
                "next_url": None,
            },
        )

    # clamp page to [1..page_count] when known
    if page < 1:
        return RedirectResponse(url=f"/read/{comic_slug}/{year_slug}/1", status_code=303)
    if not pdf_page_count_unknown and page > page_count:
        return RedirectResponse(url=f"/read/{comic_slug}/{year_slug}/{page_count}", status_code=303)

    idx0 = page - 1
    filename = f"Page {page}" if is_pdf else images[idx0]
    page_date = None
    page_title = None
    if not is_pdf and filename:
        base_name = Path(filename).name
        m = re.match(r"^(\d{8})\s*[-–—]\s*(.+)\.(?:[A-Za-z0-9]+)$", base_name)
        if m:
            raw = m.group(1)
            page_date = f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
            page_title = m.group(2).strip()
    view_mode = request.query_params.get("view", "single")
    if view_mode not in {"single", "spread"}:
        view_mode = "single"

    image_url = None
    second_image_url = None
    pdf_url = None
    second_page = None
    if is_pdf:
        image_url = f"/pdf-page/{comic_slug}/{year_slug}/{page}?dpi=250"
        if view_mode == "spread" and (pdf_page_count_unknown or page + 1 <= page_count):
            second_page = page + 1
            second_image_url = f"/pdf-page/{comic_slug}/{year_slug}/{second_page}?dpi=250"
    else:
        image_url = f"/asset/{comic_slug}/{year_slug}/{filename}"

    view_qs = f"?view={view_mode}" if is_pdf else ""
    first_url = f"/read/{comic_slug}/{year_slug}/1{view_qs}" if page > 1 else None
    last_url = (
        f"/read/{comic_slug}/{year_slug}/{page_count}{view_qs}"
        if (pdf_page_count_unknown or page < page_count)
        else None
    )
    prev_url = f"/read/{comic_slug}/{year_slug}/{page - 1}{view_qs}" if page > 1 else None
    next_url = (
        f"/read/{comic_slug}/{year_slug}/{page + 1}{view_qs}"
        if (pdf_page_count_unknown or page < page_count)
        else None
    )

    # Save progress server-side on page load (simple MVP)
    upsert_progress(comic.id, year.id, idx0)

    return _render(
        request,
        "reader.html",
        {
            "request": request,
            "comic": comic,
            "year": year,
            "page": page,
            "page_count": page_count,
            "image_url": image_url,
            "second_image_url": second_image_url,
            "page_filename": (f"Page {page}" if is_pdf else filename),
            "page_date": page_date,
            "page_title": page_title,
            "is_pdf": is_pdf,
            "pdf_url": pdf_url,
            "view_mode": view_mode,
            "second_page": second_page,
            "pdf_page_count_unknown": pdf_page_count_unknown,
            "first_url": first_url,
            "last_url": last_url,
            "prev_url": prev_url,
            "next_url": next_url,
        },
    )


@app.get("/resume/{comic_slug}")
def resume_comic(request: Request, comic_slug: str):
    comic = get_comic_by_slug(comic_slug)
    if not comic:
        raise HTTPException(status_code=404, detail="Comic not found")
    series_cfg = load_series_config()
    if not _user_can_view_comic(request.state.user, series_cfg, comic.slug):
        raise HTTPException(status_code=404, detail="Comic not found")

    lr = get_last_read_for_comic(comic.id)
    if not lr:
        return RedirectResponse(url=f"/comic/{comic.slug}", status_code=303)

    page1 = lr["page_index"] + 1
    return RedirectResponse(
        url=f"/read/{comic.slug}/{lr['year_slug']}/{page1}",
        status_code=303,
    )


@app.post("/restart/{comic_slug}")
def restart_comic(request: Request, comic_slug: str):
    comic = get_comic_by_slug(comic_slug)
    if not comic:
        raise HTTPException(status_code=404, detail="Comic not found")
    series_cfg = load_series_config()
    if not _user_can_view_comic(request.state.user, series_cfg, comic.slug):
        raise HTTPException(status_code=404, detail="Comic not found")
    delete_progress_for_comic(comic.id)
    return RedirectResponse(url=f"/comic/{comic.slug}", status_code=303)


@app.post("/restart/{comic_slug}/{year_slug}")
def restart_year(request: Request, comic_slug: str, year_slug: str):
    comic = get_comic_by_slug(comic_slug)
    if not comic:
        raise HTTPException(status_code=404, detail="Comic not found")
    series_cfg = load_series_config()
    if not _user_can_view_comic(request.state.user, series_cfg, comic.slug):
        raise HTTPException(status_code=404, detail="Comic not found")
    year = get_year_by_slugs(comic.id, year_slug)
    if not year:
        raise HTTPException(status_code=404, detail="Year not found")
    delete_progress_for_year(comic.id, year.id)
    return RedirectResponse(url=f"/read/{comic.slug}/{year.slug}/1", status_code=303)


@app.get("/asset/{comic_slug}/{year_slug}/{filename:path}")
def asset(request: Request, comic_slug: str, year_slug: str, filename: str):
    comic = get_comic_by_slug(comic_slug)
    if not comic:
        raise HTTPException(status_code=404, detail="Comic not found")
    series_cfg = load_series_config()
    if not _user_can_view_comic(request.state.user, series_cfg, comic.slug):
        raise HTTPException(status_code=404, detail="Comic not found")
    year = get_year_by_slugs(comic.id, year_slug)
    if not year:
        raise HTTPException(status_code=404, detail="Year not found")

    year_path = Path(year.path).resolve()

    if year_path.is_dir():
        file_path = (year_path / filename).resolve()

        # Safety: ensure file is within year_dir
        if year_path not in file_path.parents:
            raise HTTPException(status_code=400, detail="Invalid path")

        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        return FileResponse(str(file_path))

    if is_archive_file(year_path):
        if ".." in PurePosixPath(filename).parts:
            raise HTTPException(status_code=400, detail="Invalid path")
        if not is_image_name(filename):
            raise HTTPException(status_code=404, detail="File not found")
        data = read_archive_image(year_path, filename)
        if data is None:
            raise HTTPException(status_code=404, detail="File not found")
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return Response(content=data, media_type=media_type)

    raise HTTPException(status_code=404, detail="File not found")


@app.get("/config/posters/{filename}")
def poster_asset(filename: str):
    file_path = (POSTERS_DIR / filename).resolve()
    if POSTERS_DIR not in file_path.parents:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(file_path))


@app.get("/pdf/{comic_slug}/{year_slug}")
def pdf_asset(request: Request, comic_slug: str, year_slug: str):
    comic = get_comic_by_slug(comic_slug)
    if not comic:
        raise HTTPException(status_code=404, detail="Comic not found")
    series_cfg = load_series_config()
    if not _user_can_view_comic(request.state.user, series_cfg, comic.slug):
        raise HTTPException(status_code=404, detail="Comic not found")
    year = get_year_by_slugs(comic.id, year_slug)
    if not year:
        raise HTTPException(status_code=404, detail="Year not found")

    file_path = Path(year.path).resolve()
    if not is_pdf_file(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(file_path), media_type="application/pdf")


@app.get("/pdf-page/{comic_slug}/{year_slug}/{page}")
def pdf_page_asset(request: Request, comic_slug: str, year_slug: str, page: int, dpi: int = 250):
    comic = get_comic_by_slug(comic_slug)
    if not comic:
        raise HTTPException(status_code=404, detail="Comic not found")
    series_cfg = load_series_config()
    if not _user_can_view_comic(request.state.user, series_cfg, comic.slug):
        raise HTTPException(status_code=404, detail="Comic not found")
    year = get_year_by_slugs(comic.id, year_slug)
    if not year:
        raise HTTPException(status_code=404, detail="Year not found")

    file_path = Path(year.path).resolve()
    if not is_pdf_file(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    safe_dpi = max(72, min(400, dpi))
    cache_dir = (PDF_CACHE_DIR / comic.slug / year.slug).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"p{page}-d{safe_dpi}.png"

    if not cache_file.exists():
        data = render_pdf_page(file_path, page, safe_dpi)
        if data is None:
            raise HTTPException(status_code=404, detail="Page not found")
        cache_file.write_bytes(data)

    return FileResponse(str(cache_file), media_type="image/png")


@app.get("/config/logos/{filename}")
def logo_asset(filename: str):
    file_path = (LOGOS_DIR / filename).resolve()
    if LOGOS_DIR not in file_path.parents:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(file_path))


@app.get("/config/avatars/{filename}")
def avatar_asset(filename: str):
    file_path = (AVATARS_DIR / filename).resolve()
    if AVATARS_DIR not in file_path.parents:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(file_path))
