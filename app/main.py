import mimetypes
import os
import time
from pathlib import Path, PurePosixPath

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
)
from .auth import (
    ensure_admin_user,
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
)
from .config import ensure_config, load_series_config, save_series_config, POSTERS_DIR, LOGOS_DIR

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
)

APP_ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(APP_ROOT / "templates"))

COMICS_ENV_VAR = "COMICS_DIR"
COMICS_SETTING_KEY = "comics_dir"
DEFAULT_COMICS_DIR = "comics"
APP_VERSION = os.environ.get("APP_VERSION", "dev")

app = FastAPI(title="Webcomic Reader (MVP)")

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
    context["app_version"] = APP_VERSION
    return TEMPLATES.TemplateResponse(template_name, context)


def _scan_and_record(comics_dir: Path) -> None:
    start = time.perf_counter()
    set_setting("scan_last_started", str(time.time()))
    scan_comics(comics_dir)
    duration_ms = int((time.perf_counter() - start) * 1000)
    set_setting("scan_last_completed", str(time.time()))
    set_setting("scan_duration_ms", str(duration_ms))


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
        return RedirectResponse(url="/", status_code=303)
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
    response = RedirectResponse(url="/", status_code=303)
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



@app.get("/", response_class=HTMLResponse)
def library(request: Request, error: str | None = None, success: str | None = None):
    comics = get_comics()
    last_reads = get_last_read_all_comics()
    series_cfg = load_series_config()

    # attach last_read per comic for template convenience
    comics_rows = []
    for c in comics:
        lr = last_reads.get(c.id)
        meta = series_cfg.get(c.slug, {}) if isinstance(series_cfg, dict) else {}
        display_title = meta.get("title") or c.title
        poster = meta.get("poster")
        poster_url = f"/config/posters/{poster}" if poster else None
        years = get_years_for_comic(c.id)
        year_count = len(years)
        total_images = 0
        for year in years:
            total_images += len(get_year_images(year.path))
        progress_pct = None
        if lr:
            year = get_year_by_slugs(c.id, lr["year_slug"])
            if year:
                year_pages = max(1, len(get_year_images(year.path)))
                progress_pct = int(((lr["page_index"] + 1) / year_pages) * 100)
        comics_rows.append(
            {
                "comic": c,
                "last_read": lr,
                "poster_url": poster_url,
                "display_title": display_title,
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
    _scan_and_record(active_dir)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/settings/comics-dir/reset")
def reset_comics_dir(request: Request):
    user = request.state.user
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    delete_setting(COMICS_SETTING_KEY)
    active_dir, _source = _get_active_comics_dir()
    _scan_and_record(active_dir)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/rescan")
def rescan():
    comics_dir, _source = _get_active_comics_dir()
    _scan_and_record(comics_dir)
    return RedirectResponse(url="/", status_code=303)


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
        for year in years:
            images_count += len(get_year_images(year.path))
    users = list_users()
    scan_last_started = get_setting("scan_last_started")
    scan_last_completed = get_setting("scan_last_completed")
    scan_duration_ms = get_setting("scan_duration_ms")

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
    meta = series_cfg.get(comic.slug, {}) if isinstance(series_cfg, dict) else {}
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

    if remove_poster:
        meta.pop("poster", None)
    if remove_logo:
        meta.pop("logo", None)

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

    if meta:
        series_cfg[comic.slug] = meta
    else:
        series_cfg.pop(comic.slug, None)

    save_series_config(series_cfg)
    return RedirectResponse(url=f"/admin/series/{comic.slug}?success=Series+updated", status_code=303)


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

    years = get_years_for_comic(comic.id)
    series_cfg = load_series_config()
    meta = series_cfg.get(comic.slug, {}) if isinstance(series_cfg, dict) else {}
    poster = meta.get("poster")
    logo = meta.get("logo")
    poster_url = f"/config/posters/{poster}" if poster else None
    logo_url = f"/config/logos/{logo}" if logo else None
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
    year = get_year_by_slugs(comic.id, year_slug)
    if not year:
        raise HTTPException(status_code=404, detail="Year not found")

    images = get_year_images(year.path)
    items = []
    for idx0, filename in enumerate(images):
        page = idx0 + 1
        items.append(
            {
                "page": page,
                "filename": filename,
                "thumb_url": f"/asset/{comic.slug}/{year.slug}/{filename}",
                "read_url": f"/read/{comic.slug}/{year.slug}/{page}",
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
            "page_count": len(images),
        },
    )


@app.get("/read/{comic_slug}/{year_slug}/{page}", response_class=HTMLResponse)
def reader(request: Request, comic_slug: str, year_slug: str, page: int):
    comic = get_comic_by_slug(comic_slug)
    if not comic:
        raise HTTPException(status_code=404, detail="Comic not found")
    year = get_year_by_slugs(comic.id, year_slug)
    if not year:
        raise HTTPException(status_code=404, detail="Year not found")

    images = get_year_images(year.path)
    if not images:
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
                "prev_url": None,
                "next_url": None,
            },
        )

    page_count = len(images)

    # clamp page to [1..page_count]
    if page < 1:
        return RedirectResponse(url=f"/read/{comic_slug}/{year_slug}/1", status_code=303)
    if page > page_count:
        return RedirectResponse(url=f"/read/{comic_slug}/{year_slug}/{page_count}", status_code=303)

    idx0 = page - 1
    filename = images[idx0]

    image_url = f"/asset/{comic_slug}/{year_slug}/{filename}"

    prev_url = f"/read/{comic_slug}/{year_slug}/{page - 1}" if page > 1 else None
    next_url = f"/read/{comic_slug}/{year_slug}/{page + 1}" if page < page_count else None

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
            "page_filename": filename,
            "prev_url": prev_url,
            "next_url": next_url,
        },
    )


@app.get("/resume/{comic_slug}")
def resume_comic(comic_slug: str):
    comic = get_comic_by_slug(comic_slug)
    if not comic:
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
def restart_comic(comic_slug: str):
    comic = get_comic_by_slug(comic_slug)
    if not comic:
        raise HTTPException(status_code=404, detail="Comic not found")
    delete_progress_for_comic(comic.id)
    return RedirectResponse(url=f"/comic/{comic.slug}", status_code=303)


@app.post("/restart/{comic_slug}/{year_slug}")
def restart_year(comic_slug: str, year_slug: str):
    comic = get_comic_by_slug(comic_slug)
    if not comic:
        raise HTTPException(status_code=404, detail="Comic not found")
    year = get_year_by_slugs(comic.id, year_slug)
    if not year:
        raise HTTPException(status_code=404, detail="Year not found")
    delete_progress_for_year(comic.id, year.id)
    return RedirectResponse(url=f"/read/{comic.slug}/{year.slug}/1", status_code=303)


@app.get("/asset/{comic_slug}/{year_slug}/{filename:path}")
def asset(comic_slug: str, year_slug: str, filename: str):
    comic = get_comic_by_slug(comic_slug)
    if not comic:
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


@app.get("/config/logos/{filename}")
def logo_asset(filename: str):
    file_path = (LOGOS_DIR / filename).resolve()
    if LOGOS_DIR not in file_path.parents:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(file_path))
