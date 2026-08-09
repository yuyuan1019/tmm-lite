# Bugfix and Hardening Design

## Goal

Fix the reproducible functional, security, compatibility, and CI defects found during the audit while preserving the current application architecture and public routes. Work is committed directly to `master` and pushed to `origin/master` only after all verification gates pass.

## Scope

### Scanner concurrency

- Route every background operation through the same synchronous claim mechanism.
- Claim before returning the HTTP response or scheduling competing work.
- Ensure a stale task callback cannot release another task's ownership.
- Add regression coverage for two background rescrapes started without yielding.

### Subtitle placement and format

- Compute subtitle destinations relative to the connection/library root so folder-based items are written beside their video rather than at the library root.
- Keep the returned path consistent with the physical path.
- Preserve a supported subtitle extension when provider metadata identifies one; do not label ASS/SSA/VTT payloads as SRT.

### Connection and remote streaming correctness

- Move blocking local filesystem work out of the event loop.
- Implement efficient SFTP range reads and size lookup.
- Implement WebDAV range reads and size lookup without repeatedly buffering the complete remote file.
- Use WebDAV PROPFIND resource type information for file/directory classification.
- Send explicit Depth headers, skip the collection's own response, decode href components, and support an explicit HTTP/HTTPS scheme in WebDAV hosts.

### HTTP streaming

- Parse one RFC-style byte range: closed, open-ended, or suffix.
- Return 416 with `Content-Range: bytes */<size>` for malformed, multiple, reversed, empty-file, or unsatisfiable ranges.
- Stream both full and partial responses in bounded chunks and always close the connection.

### Web security

- HTML-escape dynamic values in the generated player page.
- Remove unsafe TMDB result `innerHTML` construction and dynamic inline JavaScript string arguments.
- Use DOM text assignment or inert data attributes for external and metadata-derived values.

### Configuration and resource lifecycle

- Validate subtitle-related YAML values with strict types.
- Install HTTPX SOCKS support because SOCKS proxy URLs are accepted by configuration.
- Construct candidate scraper resources before committing settings and close all rejected candidates.
- Roll back configuration and scheduler enabled/paused state together on pre-commit failure.
- During application shutdown, close the currently active hot-reloaded resources, while also cleaning partially acquired startup resources.

### Database and mutation safety

- Add defensive migrations for missing remote-library columns while retaining the current schema contract.
- Reject library add/delete operations while scanning.
- Validate remote host and port before persisting a library.

### CI restoration

- Resolve all Ruff and mypy failures without weakening their configuration.
- Add tests until overall coverage is at least 75%, `app/parsers/filename_parser.py` is at least 85%, and `app/scanner.py` is at least 85%.
- Keep all external HTTP calls mocked.

## Exclusions

- No redesign of remote-library identity or the existing globally unique path model.
- No multi-worker locking, authentication system, frontend build chain, or unrelated refactoring.
- No detailed Chinese repair report. A short Chinese changelog will list fixes, verification results, and commit information.

## Verification

Run on Python 3.12:

1. Focused regression tests after each fix.
2. `ruff check app tests`
3. `mypy app`
4. `pytest --cov=app --cov-report=term-missing --cov-report=json --cov-fail-under=75`
5. `python scripts/check_coverage.py coverage.json app/parsers/filename_parser.py=85 app/scanner.py=85`
6. `git diff --check`

Only after all gates pass: create the implementation commit on `master`, push `master` to `origin`, and record the resulting commit in the short changelog/final response.
