# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**TMM-Lite** — a lightweight, self-hosted media metadata scraper for movies and TV shows. It scans local directories, fetches metadata (title, year, rating, overview, genres, poster, backdrop) from TMDB (primary) and Douban (supplementary, Chinese descriptions/ratings), writes Kodi-compatible NFO files and images into media folders, and provides a web management UI. Designed as a tinyMediaManager (TMM) minimal self-built alternative.

**Stack:** Python 3.12 + FastAPI + SQLAlchemy 2.0 (SQLite) + APScheduler (AsyncIOScheduler) + httpx (async) + Jinja2 server-side templates + Docker single-container deployment.

## Documents (Authority Order)

When these conflict, the later document wins:

1. **`tmm-lite-design-spec.md`** — requirements, architecture, data model, routes, NFO spec
2. **`tmm-lite-dev-plan.md`** — module breakdown, dependency graph, per-module test requirements
3. **`tmm-lite-implementation-spec.md`** — **final baseline**: exact DDL, algorithm steps, interface signatures, error behavior, file contents (Dockerfile, docker-compose.yml, pyproject.toml, dependency versions), test fixtures, acceptance checklist

Read the relevant section of the implementation spec before writing any module code.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt          # production
pip install -r requirements-dev.txt      # dev: pytest, respx, ruff, mypy

# Lint & type-check
ruff check app tests
mypy app

# Run tests
pytest                                  # all tests
pytest tests/test_filename_parser.py    # single module
pytest -k "M7"                          # all scanner tests

# Coverage (CI gate: ≥75% overall, M3/M7 ≥85%)
pytest --cov=app --cov-report=term-missing --cov-report=json --cov-fail-under=75
python scripts/check_coverage.py coverage.json app/parsers/filename_parser.py=85 app/scanner.py=85

# Run the app (dev)
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Docker
docker compose build
docker compose up -d
```

Tests use `pytest` + `pytest-asyncio` (auto mode). HTTP mocking via `respx`. Web tests via `fastapi.testclient`. **All unit tests must mock external network** (TMDB/Douban) — no real API calls in automated tests. Integration/E2E tests use `TestClient` with `create_app(data_dir=tmp_path, start_scheduler=False)`.

## Architecture

```
Web Browser ──► FastAPI (Jinja2 SSR, no JS build)
                   │
              ┌────┴────┐
         Scheduler    SQLite (via SQLAlchemy)
         (APScheduler)   Library / MediaItem / ScrapeLog / AppMeta
              │
         ScanRunner (core orchestration)
              │
    ┌─────┬───┴───┬─────┐
  Parser  TMDB   Douban  NFO Writer
```

### Module Dependency Graph

```
M1 config ──┐
M2 database ──┤  (base layer — no mutual deps, can develop in parallel)
             │
M3 filename_parser ──┤
M4 nfo_writer       ──┤
M5 scraper_tmdb     ──┤  (all depend on shared DTO from scrapers/base.py)
M6 scraper_douban   ──┘
             │
M7 scanner (main flow) ── depends on M1–M6
M8 scheduler ── depends on M7
M9 web (routes + templates) ── depends on M1, M2, M7, M8
M10 deploy (Docker) ── depends on all
```

### Key Modules

| Module | File | Responsibility |
|--------|------|---------------|
| M1 Config | `app/config.py` | Read/write `data/config.yaml`, atomic save, API key priority (YAML > env var), cron validation |
| M2 Database | `app/database.py` | SQLAlchemy models, session factory, foreign key PRAGMA, UTC datetime storage |
| M3 Parser | `app/parsers/filename_parser.py` | Pure functions: extract title/year/season/episode from folder/filename, noise word stripping |
| M4 NFO Writer | `app/nfo_writer.py` | Generate Kodi `movie.nfo`/`tvshow.nfo` via lxml (no string concatenation), atomic write |
| M5 TMDB | `app/scrapers/tmdb.py` | Official API: search → detail → ScrapedMeta, 429 retry with backoff, image download |
| M6 Douban | `app/scrapers/douban.py` | Unofficial scraping: suggest API → year validation → detail HTML parse, rate-limited, never throws |
| M7 Scanner | `app/scanner.py` | Core flow: directory walk → incremental DB sync → NFO skip logic → scrape orchestration → state machine |
| M8 Scheduler | `app/scheduler.py` | Cron-triggered full scans via APScheduler, hot-reload on config change |
| M9 Web | `app/main.py` + `templates/` | Jinja2 SSR pages, flash messages via query params, form-based routes, 303 redirects |
| M10 Deploy | `Dockerfile` + `docker-compose.yml` | Single-container, root user (v1), `data/` volume, TZ=Asia/Shanghai, healthcheck at `/healthz` |

### Shared Types

- **`app/scrapers/base.py`** — `ScrapedMeta` dataclass (the single source of truth for scrape results; M4/M5/M7 all import from here)
- **`app/exceptions.py`** — unified exception hierarchy: `TmmError` → `ConfigError`, `TmdbError` → `TmdbAuthError`/`TmdbRateLimitError`, `ScrapeError`, `ScanBusyError`, `ItemNotFoundError`

## Critical Design Rules

### State Machine (MediaItem.status)

The 5 legal statuses: `pending` → `matched` (success) or `failed` (error) or `manual_needed` (unparseable title). `missing` = folder deleted from disk. `manual_needed` and `missing` never enter the auto-scrape queue. `failed` items always retry (even with existing NFO). `matched` items only re-scrape when `overwrite_existing_nfo=true`.

### Single-Process Constraint

The app uses an in-process mutex (`_running` flag set synchronously before any `await`) for concurrency control. **Never** run multiple Uvicorn workers or container replicas — the scheduler and mutex are process-local. Deployment is fixed to single worker, single replica.

### Transaction Discipline

**Never hold a SQLite write transaction across an external HTTP `await`.** Scanner syncs DB changes in short transactions before/after each network call. Session factory uses `expire_on_commit=False`.

### NFO as Completion Marker

NFO is always written **last** (after images download). The presence of a valid NFO marks a completed scrape. If NFO write fails, the item is `failed` and no partial NFO exists. If image download fails (when URL is present), the item is `failed` even if NFO could be written.

### Single Source of Truth

- **Libraries**: database is the sole source of truth after initial seed import from `config.yaml` (one-time, tracked via `AppMeta` key)
- **Settings** (API key, cron, douban toggle, etc.): `config.yaml` is the sole persistent store; web settings page writes directly to it
- **API Key priority**: YAML value > `TMDB_API_KEY` env var > empty string

### Douban Isolation

Douban failures must **never** affect the TMDB main flow. Exceptions are caught at both the Douban module level AND the scanner orchestration level. Douban only supplements `overview` and `rating`; TMDB is the authoritative match source.

## Development Order

Fixed by the dependency graph — follow the milestones:

1. **MS1**: M1 (config) + M2 (database) — foundation
2. **MS2**: M3 (parser) + M4 (NFO writer) — pure functions, can parallel with MS3
3. **MS3**: M5 (TMDB) + M6 (Douban) — scrapers, mock all HTTP
4. **MS4**: M7 (scanner) — **highest risk, reserve most time**
5. **MS5**: M8 (scheduler) + M9 (web) — service layer + E2E tests
6. **MS6**: M10 (deploy) + README + real-environment smoke tests

After `app/scrapers/base.py` (shared DTO) is fixed, M3/M4/M5/M6 can be developed in parallel.

## Testing Conventions

- `tests/` mirrors `app/` structure
- DB tests use `tmp_path` file SQLite; memory SQLite requires `StaticPool`
- TMDB/Douban tests use `respx` mocks exclusively
- JSON fixtures live in `tests/fixtures/`
- Web tests use `TestClient(create_app(data_dir=..., start_scheduler=False))`
- CI gate: overall coverage ≥75%, M3 (`filename_parser.py`) and M7 (`scanner.py`) each ≥85%

## Key Constraints

- **No frontend build chain** — all pages are Jinja2 SSR with a single `static/style.css`
- **No external DB** — SQLite file in `data/` volume
- **Chinese-first** metadata via `language=zh-CN` on all TMDB requests
- **Image CDN requests** must NOT carry `language` parameter
- **Sensitive data**: logs, exceptions, and `error_message` must never contain the TMDB API key
- **v1 scope**: no episode-level scraping, no manual match UI, no multi-user, no notifications, no Basic Auth; root container only
