# Bugfix Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the audited runtime, security, compatibility, and CI defects and push a fully verified `master` branch.

**Architecture:** Keep the existing FastAPI/SQLAlchemy/scanner structure, but isolate byte-range logic in a small streaming module and make the connection abstraction responsible for efficient bounded reads. Apply all behavior changes through regression-first tests; preserve current routes and database semantics.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, httpx, Paramiko, APScheduler, pytest/pytest-asyncio/respx, Ruff, mypy.

## Global Constraints

- Work directly on `master`; push only after every verification gate passes.
- Do not make real TMDB, Douban, ASSRT, OpenSubtitles, SubDL, SSH, or WebDAV calls in automated tests.
- Never hold a SQLite write transaction across an external `await`.
- Preserve the single-process synchronous scanner claim invariant.
- Overall coverage must be at least 75%; `app/parsers/filename_parser.py` and `app/scanner.py` must each be at least 85%.
- Do not weaken Ruff, mypy, or coverage configuration.
- Preserve user data and the current globally unique path schema.
- Produce only a short Chinese changelog, not a detailed repair report.

## File Map

**Create**

- `.gitattributes` — enforce LF for source and documentation files.
- `app/streaming.py` — byte-range parsing and bounded connection streaming.
- `tests/test_connection.py` — local, SFTP, and WebDAV connection behavior.
- `tests/test_streaming.py` — byte-range parser and chunk generator tests.
- `tests/test_subtitle.py` — provider fallback, extension, destination, and cleanup tests.
- `docs/changes/2026-08-09-bugfixes.md` — short Chinese changelog.

**Modify**

- `.gitignore` — ignore generated `coverage.json`.
- `requirements.txt` — install HTTPX SOCKS support.
- `app/config.py` — strict subtitle-setting validation.
- `app/database.py` — defensive library-column migrations.
- `app/connection.py` — non-blocking local I/O, efficient SFTP/WebDAV range reads, reliable WebDAV resource typing.
- `app/scanner.py` — shared synchronous background claim, subtitle path correctness, safe NFO metadata narrowing.
- `app/scrapers/subtitle.py` — root-relative writes and extension preservation.
- `app/main.py` — resource lifecycle, transactional settings, library mutation guards, remote validation, safe streaming, escaped player HTML.
- `app/templates/items.html` — safe DOM construction for TMDB candidates.
- `app/templates/preview.html` — inert data attributes instead of dynamic JavaScript string arguments.
- `tests/test_config.py`, `tests/test_database.py`, `tests/test_scanner.py`, `tests/test_web.py`, `tests/test_nfo_writer.py`, `tests/test_tmdb.py` — regression and CI coverage.

---

### Task 1: Normalize the Workspace Without Losing Content

**Files:**
- Create: `.gitattributes`
- Modify: `.gitignore`

**Interfaces:**
- Produces: a clean LF worktree and ignored generated coverage report.

- [ ] **Step 1: Prove the pre-existing tracked changes are EOL-only**

Run:

```bash
git diff --ignore-space-at-eol --quiet
test $? -eq 0
git diff --numstat
```

Expected: semantic diff exits zero while regular diff shows whole-file CRLF changes.

- [ ] **Step 2: Add deterministic text and artifact rules**

Create `.gitattributes`:

```gitattributes
* text=auto
*.py text eol=lf
*.html text eol=lf
*.md text eol=lf
*.toml text eol=lf
*.txt text eol=lf
*.yaml text eol=lf
*.yml text eol=lf
```

Append to `.gitignore`:

```gitignore
coverage.json
```

- [ ] **Step 3: Normalize only files proven to have no semantic changes**

Run a Python script over paths reported by `git diff --name-only`, replacing `\r\n` with `\n`, then run:

```bash
git diff --ignore-space-at-eol --quiet
git status --short
```

Expected: only `.gitattributes` and `.gitignore` remain changed; `coverage.json` is hidden by ignore rules.

- [ ] **Step 4: Commit workspace hygiene**

```bash
git add .gitattributes .gitignore
git commit -m "chore: normalize repository text files"
```

---

### Task 2: Make Background Scanner Ownership Atomic

**Files:**
- Modify: `app/scanner.py:514-710`
- Test: `tests/test_scanner.py`

**Interfaces:**
- Produces: `_start_background(factory: Callable[[], Coroutine[Any, Any, T]]) -> asyncio.Task[T]`.
- Preserves: `start_full_background`, `start_rescrape_failed_background`, `start_rescrape_item_background`, `start_refresh_subtitles_background`, and `start_rescan_library_background` public signatures.

- [ ] **Step 1: Add a failing no-yield race regression test**

Add a test equivalent to:

```python
@pytest.mark.asyncio
async def test_background_rescrape_claims_before_task_is_scheduled(tmp_path: Path) -> None:
    h = _setup(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(*args: object, **kwargs: object) -> MediaItem:
        entered.set()
        await release.wait()
        return MediaItem(id=1, library_id=1, media_type="movie", folder_path="/x", status="matched")

    h.runner._rescrape_item_impl = blocked  # type: ignore[method-assign]
    first = h.runner.start_rescrape_item_background(1)
    assert h.runner.is_running is True
    with pytest.raises(ScanBusyError):
        h.runner.start_rescrape_item_background(2)
    assert h.runner._current_task is first
    release.set()
    await first
    assert h.runner.is_running is False
```

- [ ] **Step 2: Run the race test and verify failure**

```bash
pytest tests/test_scanner.py::test_background_rescrape_claims_before_task_is_scheduled -v
```

Expected: FAIL because the second background rescrape is accepted before either task runs.

- [ ] **Step 3: Generalize the claimed background starter**

Use a `TypeVar` and coroutine factory so `_claim()` runs before coroutine creation. The done callback must clear state only when its task is still `_current_task`; all five background entry points use this helper.

- [ ] **Step 4: Add cancellation and rejected-coroutine regressions**

Cover: `stop()` cancels the claimed rescrape task; a rejected second start emits no un-awaited coroutine warning; completion clears `_current_task` once.

- [ ] **Step 5: Run focused scanner concurrency tests**

```bash
pytest tests/test_scanner.py -k "background or concurrent or stop" -v
```

Expected: PASS.

- [ ] **Step 6: Commit concurrency fix**

```bash
git add app/scanner.py tests/test_scanner.py
git commit -m "fix(scanner): claim background rescrapes synchronously"
```

---

### Task 3: Save Subtitles Beside Their Video

**Files:**
- Modify: `app/scrapers/subtitle.py:156-204`
- Modify: `app/scanner.py:1572-1609`
- Create: `tests/test_subtitle.py`
- Test: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `Connection.root` as the write-path base.
- Produces: `_subtitle_extension(filename: str) -> str` and root-relative connection writes.

- [ ] **Step 1: Add failing folder-destination and extension tests**

```python
@pytest.mark.asyncio
async def test_save_connection_writes_beside_nested_video(tmp_path: Path) -> None:
    root = tmp_path / "movies"
    folder = root / "Film (2020)"
    folder.mkdir(parents=True)
    downloader = SubtitleDownloader("")
    downloader._subdl = AsyncMock()
    downloader._subdl.download.return_value = b"[Script Info]"
    result = SubtitleResult("subdl", "chi", "release.ass", "https://example/sub")

    returned = await downloader._save(
        result, folder, "video.mkv", LocalConnection(str(root)),
    )

    assert returned == folder / "video.zh.ass"
    assert returned.read_bytes() == b"[Script Info]"
    assert not (root / "video.zh.ass").exists()
```

Also cover `Film/Disc/video.mkv`, loose root files, unknown extensions defaulting to `.srt`, and fake remote connections receiving `Film/video.zh.ass`.

- [ ] **Step 2: Run the destination test and verify failure**

```bash
pytest tests/test_subtitle.py::test_save_connection_writes_beside_nested_video -v
```

Expected: FAIL because the file is currently written at the library root and named `.srt`.

- [ ] **Step 3: Implement root-relative path calculation**

Normalize `folder`, destination, and `connection.root` as POSIX-like paths, require the destination to be under the connection root, and pass that relative path to `connection.write_bytes`. Select `.srt`, `.ass`, `.ssa`, or `.vtt` from `result.filename`; default to `.srt`.

- [ ] **Step 4: Cover provider fallback and cleanup paths**

Add mocked tests for ASSRT success, ASSRT failure to OpenSubtitles, OpenSubtitles failure to SubDL, no result, and `aclose()` resetting lazy clients.

- [ ] **Step 5: Run subtitle and scanner subtitle tests**

```bash
pytest tests/test_subtitle.py tests/test_subtitle_assrt.py tests/test_scanner.py -k subtitle -v
```

Expected: PASS with no network calls.

- [ ] **Step 6: Commit subtitle fix**

```bash
git add app/scrapers/subtitle.py app/scanner.py tests/test_subtitle.py tests/test_scanner.py
git commit -m "fix(subtitles): write files next to media videos"
```

---

### Task 4: Harden the Connection Abstraction

**Files:**
- Modify: `app/connection.py`
- Create: `tests/test_connection.py`

**Interfaces:**
- Produces efficient overrides:
  - `SshConnection.read_range(path: str, offset: int, size: int) -> bytes`
  - `SshConnection.file_size(path: str) -> int`
  - `WebdavConnection.read_range(path: str, offset: int, size: int) -> bytes`
  - `WebdavConnection.file_size(path: str) -> int`
- Produces `_parse_propfind_entries(xml_text: str, request_url: str) -> list[tuple[str, bool]]`, where the boolean means collection/directory.

- [ ] **Step 1: Write local connection behavior tests**

Cover sorted directory listing, file/directory checks, bounded reads, size, atomic writes, mkdir, exists, video detection, and path resolution. Patch `asyncio.to_thread` with a spy to prove filesystem operations are offloaded.

- [ ] **Step 2: Run local tests and verify the offload test fails**

```bash
pytest tests/test_connection.py -k local -v
```

Expected: at least one FAIL because current local methods execute synchronously.

- [ ] **Step 3: Offload local filesystem operations**

Wrap directory enumeration, stat/read/range/write/mkdir/exists in small synchronous callables passed to `asyncio.to_thread`; retain atomic temp-file replacement and cleanup.

- [ ] **Step 4: Write SFTP bounded-read tests with an in-memory fake**

The fake SFTP file records `seek(offset)` and `read(size)`. Assert `read_range("Film/video.mkv", 5, 3) == b"567"` and `file_size` uses `stat().st_size`, not `read_bytes`.

- [ ] **Step 5: Implement SFTP range and size methods**

Use the existing `_ensure_connected()` and perform seek/read/stat inside `asyncio.to_thread`.

- [ ] **Step 6: Write WebDAV PROPFIND tests**

Use `httpx.MockTransport` with a standard multistatus body containing the requested collection, `Film%20One/`, and `video.mkv`. Assert:

```python
assert request.headers["Depth"] == "1"
assert await conn.list_dir("") == ["Film One", "video.mkv"]
assert await conn.is_dir("Film One") is True
assert await conn.is_file("video.mkv") is True
```

Also cover malformed XML, absolute href URLs, Depth 0 cache misses, `https://nas.example:8443` hosts, and 404.

- [ ] **Step 7: Run WebDAV tests and verify current heuristics fail**

```bash
pytest tests/test_connection.py -k webdav -v
```

Expected: FAIL because self hrefs are included, names remain encoded, and HEAD 200 classifies both kinds incorrectly.

- [ ] **Step 8: Implement WebDAV resource parsing and bounded reads**

Use PROPFIND `resourcetype/collection`, cache kinds learned by `list_dir`, URL-decode only path components, and quote outbound path components. For range reads, send a Range request with streamed response handling, consume at most the required bytes, and close the upstream response in `finally`. `file_size` uses `Content-Length` or WebDAV `getcontentlength`.

- [ ] **Step 9: Run the complete connection suite**

```bash
pytest tests/test_connection.py -v
```

Expected: PASS.

- [ ] **Step 10: Commit connection hardening**

```bash
git add app/connection.py tests/test_connection.py
git commit -m "fix(connections): support reliable bounded remote IO"
```

---

### Task 5: Parse and Stream HTTP Byte Ranges Safely

**Files:**
- Create: `app/streaming.py`
- Create: `tests/test_streaming.py`
- Modify: `app/main.py:1098-1203`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces:
  - `ByteRange(start: int, end: int)` dataclass.
  - `RangeNotSatisfiable(ValueError)`.
  - `parse_byte_range(header: str | None, file_size: int) -> ByteRange | None`.
  - `iter_connection_bytes(conn: Connection, path: str, start: int, end: int, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]`.

- [ ] **Step 1: Add byte-range parser tests**

Parameterized expectations:

```python
@pytest.mark.parametrize(("header", "size", "expected"), [
    (None, 10, None),
    ("bytes=0-3", 10, ByteRange(0, 3)),
    ("bytes=7-", 10, ByteRange(7, 9)),
    ("bytes=-4", 10, ByteRange(6, 9)),
])
def test_parse_byte_range(header: str | None, size: int, expected: ByteRange | None) -> None:
    assert parse_byte_range(header, size) == expected
```

Reject `bytes=999-1000`, `bytes=7-3`, `bytes=0-1,4-5`, `garbage`, `bytes=-0`, and every range on an empty file.

- [ ] **Step 2: Run parser tests and verify missing implementation failure**

```bash
pytest tests/test_streaming.py -v
```

Expected: collection error/import failure until `app.streaming` is created.

- [ ] **Step 3: Implement the strict single-range parser and bounded iterator**

The iterator must never request more than `chunk_size`, stop if a connection returns empty bytes, and not own/close the connection.

- [ ] **Step 4: Add stream-route regression tests**

Create a 10-byte local MP4 item. Assert valid closed/open/suffix ranges return 206 and exact content; invalid ranges return 416 plus `Content-Range: bytes */10`; full GET returns 200. Add a fake connection test proving a large range is read in bounded calls.

- [ ] **Step 5: Replace route-local regex and eager range reads**

Use `parse_byte_range`; wrap `iter_connection_bytes` in a route generator whose `finally` closes the connection. Set exact 200/206 headers and return a direct 416 response for parser errors.

- [ ] **Step 6: Run streaming tests**

```bash
pytest tests/test_streaming.py tests/test_web.py -k "stream or play" -v
```

Expected: PASS; invalid ranges no longer return 500 or silently stream the full file.

- [ ] **Step 7: Commit streaming fix**

```bash
git add app/streaming.py app/main.py tests/test_streaming.py tests/test_web.py
git commit -m "fix(stream): validate and bound byte range responses"
```

---

### Task 6: Remove Metadata-Driven XSS Paths

**Files:**
- Modify: `app/main.py:1439-1600`
- Modify: `app/templates/items.html:114-181`
- Modify: `app/templates/preview.html:35-185`
- Test: `tests/test_web.py`

**Interfaces:**
- Preserves all existing page routes and user actions.
- Changes `openDetail` to consume a poster-card element and read inert `data-*` values.

- [ ] **Step 1: Add failing malicious-metadata tests**

Assert `_play_page_html` does not contain raw `</h2><script>` or raw `<img onerror=...>` values and contains their escaped representation. Insert a matched item containing quotes, backslashes, HTML, and a newline; assert `/preview` renders only `onclick="openDetail(this)"` and no executable payload.

- [ ] **Step 2: Add a template safety assertion for TMDB results**

Read `items.html` and assert candidate fields are assigned with `textContent`, no candidate/error field is concatenated into `innerHTML`, and poster URLs are assigned through an image element property.

- [ ] **Step 3: Run security-focused tests and verify failure**

```bash
pytest tests/test_web.py -k "xss or malicious or template_safety" -v
```

Expected: FAIL on raw player HTML and unsafe template construction.

- [ ] **Step 4: Escape generated HTML and replace unsafe JavaScript construction**

Use `html.escape(..., quote=True)` for player title and filename. In `preview.html`, store values in autoescaped `data-*` attributes and call `openDetail(this)`. In `items.html`, build candidate rows with `document.createElement` and `textContent`; use no metadata-derived `innerHTML`.

- [ ] **Step 5: Run page and security tests**

```bash
pytest tests/test_web.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit security fix**

```bash
git add app/main.py app/templates/items.html app/templates/preview.html tests/test_web.py
git commit -m "fix(web): prevent metadata driven script injection"
```

---

### Task 7: Make Configuration and Resource Swaps Transactional

**Files:**
- Modify: `requirements.txt`
- Modify: `app/config.py`
- Modify: `app/main.py:75-176, 785-970`
- Test: `tests/test_config.py`
- Test: `tests/test_tmdb.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Preserves `create_app(...)` and `save_config(...)` signatures.
- Adds strict validation for `subtitle_enabled`, `opensubtitles_api_key`, `subtitle_languages`, `opensubtitles_user_agent`, and `assrt_token`.

- [ ] **Step 1: Add strict YAML type tests**

Parameterized invalid values include string `"false"` for `subtitle_enabled`, list API keys/tokens, and empty/non-string language/user-agent values. Each must raise `ConfigError` instead of being coerced.

- [ ] **Step 2: Run config tests and verify failure**

```bash
pytest tests/test_config.py -k subtitle_type -v
```

Expected: FAIL because `bool("false")` currently becomes `True`.

- [ ] **Step 3: Implement strict config validation**

Validate booleans as booleans, secrets as strings, and language/user-agent as non-empty strings. Keep unknown keys preserved.

- [ ] **Step 4: Install and verify SOCKS support**

Change the dependency to:

```text
httpx[socks]>=0.27,<1
```

Refresh the isolated Python 3.12 dependencies and add a constructor test showing `TmdbScraper("key", proxy="socks5://127.0.0.1:1080")` no longer raises `ImportError`; close it afterward.

- [ ] **Step 5: Add settings rollback tests**

Monkeypatch candidate `TmdbScraper` construction to raise after startup. POST settings and assert: response redirects with an error, config bytes are unchanged, scheduler pause state is unchanged, and app state still references the old scraper. Add a runner-reconfigure failure case with the same assertions and candidate close checks.

- [ ] **Step 6: Add hot-reload shutdown ownership test**

Track fake initial and replacement scraper instances. After a successful settings POST and TestClient shutdown, assert the initial scraper was closed during replacement and the active replacement was closed during lifespan cleanup.

- [ ] **Step 7: Refactor lifespan and settings commit order**

Wrap the full acquisition period in `try/finally`; track partially acquired resources. Build candidates before writing config, commit synchronously as save → scheduler state → runner swap, update `app.state`, and close rejected/old resources outside the commit path. Lifespan cleanup reads current active resources from state and avoids double-close by identity.

- [ ] **Step 8: Run config, scraper, scheduler, and settings tests**

```bash
pytest tests/test_config.py tests/test_tmdb.py tests/test_scheduler.py tests/test_web.py -k "config or proxy or settings or lifespan or scheduler" -v
```

Expected: PASS.

- [ ] **Step 9: Commit transactional settings fix**

```bash
git add requirements.txt app/config.py app/main.py tests/test_config.py tests/test_tmdb.py tests/test_web.py
git commit -m "fix(config): make proxy and scraper reloads transactional"
```

---

### Task 8: Protect Library Mutations and Legacy Databases

**Files:**
- Modify: `app/database.py:185-215`
- Modify: `app/main.py:370-486`
- Test: `tests/test_database.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Preserves database model and routes.
- Migration guarantees `library.connection_type` and `library.connection_config_encrypted` exist.

- [ ] **Step 1: Add a legacy-schema migration test**

Create SQLite tables with a `library` table containing only `id/name/path/media_type`; call `init_db`; assert both remote columns exist, existing rows read as `connection_type == "local"`, and `connection_config_encrypted is None`.

- [ ] **Step 2: Run migration test and verify failure**

```bash
pytest tests/test_database.py::test_migrate_legacy_library_connection_columns -v
```

Expected: FAIL with `no such column: library.connection_type`.

- [ ] **Step 3: Add idempotent column migration**

Inspect `PRAGMA table_info(library)` and issue only missing `ALTER TABLE` statements, using `NOT NULL DEFAULT 'local'` for connection type.

- [ ] **Step 4: Add scan-time mutation tests**

Set `app.state.runner._running = True`; POST library add and delete; assert both redirect with the busy message and database row counts are unchanged.

- [ ] **Step 5: Add remote credential validation tests**

Assert empty SSH/WebDAV host, non-numeric port, port 0, and port 65536 are rejected. Assert valid SSH 22 and WebDAV 443 are persisted. Reject manually posted `connection_type=smb` because no implementation exists.

- [ ] **Step 6: Implement route guards and validation**

Check `runner.is_running` before opening mutation sessions. Accept only `local`, `ssh`, and `webdav`; require a host for fresh remote credentials and enforce ports 1–65535.

- [ ] **Step 7: Run database and library-route tests**

```bash
pytest tests/test_database.py tests/test_web.py -k "library or migration or remote" -v
```

Expected: PASS.

- [ ] **Step 8: Commit mutation/migration fix**

```bash
git add app/database.py app/main.py tests/test_database.py tests/test_web.py
git commit -m "fix(database): protect library mutations and upgrades"
```

---

### Task 9: Restore Scanner and Overall Coverage Gates

**Files:**
- Modify: `tests/test_scanner.py`
- Modify: `tests/test_web.py`
- Modify: `tests/test_connection.py`
- Modify: `tests/test_subtitle.py`
- Modify production files only when a newly tested branch reveals a real defect.

**Interfaces:**
- Produces no new public API; verifies existing scanner state-machine behavior.

- [ ] **Step 1: Measure current post-fix coverage**

```bash
pytest --cov=app --cov-report=term-missing --cov-report=json:/tmp/tmm-coverage.json
python scripts/check_coverage.py /tmp/tmm-coverage.json app/parsers/filename_parser.py=85 app/scanner.py=85
```

Expected before additions: scanner remains below 85%.

- [ ] **Step 2: Cover per-library rescan branches**

Add tests for successful rediscovery/scrape, existing NFO metadata, inaccessible connection, missing library, item scrape failure, and background busy rejection. Assert `ScrapeLog.total/matched/failed/detail` and final item states.

- [ ] **Step 3: Cover subtitle-refresh branches**

Add tests for disabled downloader, no eligible items, Chinese-title skip, successful download, no-video result, downloader exception isolation, and cancellation. Assert connection closure and final log counts.

- [ ] **Step 4: Cover scanner helper/error branches**

Add focused tests for malformed NFO `genres`, numeric conversion failures, ignored-path serialization, relative-path errors, connection credential conversion failures, deep video discovery errors, and `_persist_result`/state transitions.

- [ ] **Step 5: Re-run scanner coverage after each group**

```bash
pytest tests/test_scanner.py --cov=app.scanner --cov-report=term-missing
```

Expected final scanner coverage: at least 85% without exclusions.

- [ ] **Step 6: Close the overall coverage gap with behavior tests**

Use the missing-line report to add concrete connection, subtitle, and web lifecycle cases until overall coverage reaches at least 75%. Do not add no-op line-execution tests; every assertion must verify an externally visible result, state transition, request, write path, or cleanup call.

- [ ] **Step 7: Commit coverage tests**

```bash
git add tests
git commit -m "test: restore scanner and application coverage gates"
```

---

### Task 10: Resolve Static Analysis Findings

**Files:**
- Modify: `app/main.py`
- Modify: `app/scanner.py`
- Modify: `app/connection.py`
- Modify: `app/scrapers/assrt.py`
- Modify: `tests/test_nfo_writer.py`
- Modify other touched files only for findings emitted by the commands.

**Interfaces:**
- No behavior changes beyond explicit logging/type narrowing.

- [ ] **Step 1: Run Ruff and capture exact remaining findings**

```bash
ruff check app tests
```

- [ ] **Step 2: Fix findings without blanket ignores**

Log best-effort exceptions with safe context, remove stale `noqa`, format the long NFO import, use byte literals where appropriate, and retain narrowly justified suppressions only where blocking operations have already been moved to worker threads.

- [ ] **Step 3: Run mypy and capture exact remaining findings**

```bash
mypy app
```

- [ ] **Step 4: Narrow dynamic values explicitly**

Use `isinstance` for NFO genre iterables, safe integer conversion helpers for decrypted config/file-info values, distinct variable names for optional video paths and connection configs, and precise annotations for route-local dictionaries.

- [ ] **Step 5: Verify both static gates**

```bash
ruff check app tests
mypy app
```

Expected: both exit zero.

- [ ] **Step 6: Commit static-analysis fixes**

```bash
git add app tests
git commit -m "chore: satisfy lint and type checking gates"
```

---

### Task 11: Final Verification, Changelog, Commit, and Push

**Files:**
- Create: `docs/changes/2026-08-09-bugfixes.md`

**Interfaces:**
- Produces the requested short Chinese changelog and pushed `origin/master`.

- [ ] **Step 1: Run the complete Python 3.12 test and coverage gate**

```bash
COVERAGE_FILE=/tmp/tmm-final.coverage pytest \
  --cov=app \
  --cov-report=term-missing \
  --cov-report=json:/tmp/tmm-final-coverage.json \
  --cov-fail-under=75
python scripts/check_coverage.py /tmp/tmm-final-coverage.json \
  app/parsers/filename_parser.py=85 app/scanner.py=85
```

Expected: all tests pass, overall ≥75%, parser ≥85%, scanner ≥85%.

- [ ] **Step 2: Run final non-test gates**

```bash
ruff check app tests
mypy app
python -m compileall -q app
git diff --check
git status --short
```

Expected: all commands exit zero; only the changelog is uncommitted.

- [ ] **Step 3: Write the short Chinese changelog**

Include compact sections: `修复内容`, `测试结果`, and `提交信息`. List only verified facts and the implementation commit(s); do not include the detailed investigation narrative.

- [ ] **Step 4: Commit the changelog**

```bash
git add docs/changes/2026-08-09-bugfixes.md
git commit -m "docs: record verified bug fixes"
```

- [ ] **Step 5: Verify the exact commits to push**

```bash
git status --short
git log --oneline origin/master..master
git diff --stat origin/master..master
git diff --check origin/master..master
```

Expected: clean worktree and only reviewed bugfix/design/plan/changelog commits ahead of `origin/master`.

- [ ] **Step 6: Push directly to master**

```bash
git push origin master
```

Expected: successful fast-forward update of `origin/master`.

- [ ] **Step 7: Confirm remote state**

```bash
git fetch origin
git rev-parse HEAD
git rev-parse origin/master
git status --short
```

Expected: local and remote hashes match and the worktree is clean.
