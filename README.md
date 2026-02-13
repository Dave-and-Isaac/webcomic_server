# StripStash

**Note:** This project was written with AI assistance.

A lightweight, self-hosted webcomic reader focused on folder-based libraries and simple reading flows.

## Features

- Scan a comics folder and browse series
- Resume reading where you left off
- Year-based organization (no chapters)
- Mixed libraries: image folders or archives (`.cbz`, `.zip`, `.cbr`)
- Admin panel for settings and users
- Light/Dark/System themes
- Healthcheck endpoint

## Folder Structure

The app expects series organized like this:

```
comics/
  Series Name/
    2019/
      001.jpg
      002.jpg
    2020/
      001.jpg
```

You can also put archives directly in the series folder:

```
comics/
  Series Name/
    2019.cbz
    2020.zip
    2021.cbr
```

## Local Development

**Note:** Use Python 3.12 for local development (matches the Docker image and avoids PyMuPDF build issues on newer Python versions).

1. Create a virtualenv and install deps:

```
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Run the server:

```
uvicorn app.main:app --reload
```

3. Open: `http://localhost:8000`

## Docker (single container)

Build and run locally:

```
docker build -t webcomic-reader:local .

docker run --rm -p 8000:8000 \
  -v "$PWD/comics:/comics:ro" \
  -v "$PWD/config:/app/config" \
  -v "$PWD/data:/app/data" \
  webcomic-reader:local
```

## Docker Compose (recommended)

Set your GitHub Container Registry owner in a `.env` file:

```
GHCR_OWNER=your-github-username
```

Then start:

```
docker compose pull
docker compose up -d
```

## Configuration

- `COMICS_DIR` (optional): override the comics directory path
- Config folder is required and should be mounted at `/app/config`
- Data folder should be mounted at `/app/data`

The app will auto-create:
- `config/posters/`
- `config/logos/`
- `config/series.json`

## Healthcheck

```
GET /health
```

## Notes

- Admin default login: username `admin`, password `admin`. You’ll be prompted to change it.
- `.cbr` support requires a RAR extractor backend (`unar`/`unrar`). The Docker image includes both.

## License

MIT
