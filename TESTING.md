# Testing Guide

Nexus has four test layers. The first two run anywhere; the E2E layer needs a
browser and a running stack.

| Layer | Tooling | Location | Runs in CI? |
|-------|---------|----------|-------------|
| Backend unit | pytest | `tests/unit/` | ✅ |
| Backend integration / API | pytest + FastAPI `TestClient` | `tests/integration/` | ✅ |
| Frontend unit / component | Vitest + React Testing Library | `frontend/src/**/*.test.{ts,tsx}` | ✅ |
| End-to-end | Playwright (system Chrome) | `frontend/e2e/*.spec.ts` | ⚠️ needs browser + stack |

---

## Backend (pytest)

The workspace packages (`nexus_common`, `nexus_server`, `nexus_steps`,
`nexus_agent`, `nexus_cli`) are made importable by `tests/conftest.py`, which
injects each `packages/*/src` directory onto `sys.path`. This is deliberate: the
editable installs in `.venv` are unreliable and the environment is offline, so we
don't depend on `pip install -e` working.

`conftest.py` also sets hermetic `JWT_SECRET` and `CREDENTIAL_ENCRYPTION_KEY`
values and points the database at in-memory SQLite — **no real DB, secrets, or
network are touched**.

### Run

```bash
# Everything
.venv/bin/python -m pytest

# A layer
.venv/bin/python -m pytest tests/unit
.venv/bin/python -m pytest tests/integration

# A single file, verbose
.venv/bin/python -m pytest tests/unit/test_parser.py -v

# With coverage
.venv/bin/python -m pytest --cov=packages --cov-report=term-missing
```

### Key fixtures (`tests/conftest.py`)

- `db` — per-test `AsyncSession` on in-memory SQLite (StaticPool).
- `app` — `create_app()` wired to the test DB with `get_db` overridden and the
  production lifespan replaced by a no-op (no on-disk DB, no default admin, no
  background runner work).
- `client` — unauthenticated `TestClient`.
- `admin_client` / `auth_client` — `TestClient` pre-authenticated as an admin /
  regular user.
- `admin_user`, `regular_user`, `admin_token`, `user_token`.
- `auth_service`, `encryptor`, `credential_manager`.
- `sample_pool`, `sample_node`, `step_context`.

pytest is configured (`pyproject.toml`) with `asyncio_mode = "auto"`, so
`async def test_*` functions need no decorator.

---

## Frontend (Vitest)

```bash
cd frontend
npm test                 # run once
npm run test:watch       # watch mode
npm run test:coverage    # with v8 coverage
npx vitest run src/lib/utils.test.ts   # a single file
```

Config: `frontend/vitest.config.ts` (jsdom env, globals, `src/test/setup.ts`).
`setup.ts` installs jest-dom matchers, RTL auto-cleanup, an in-memory
`localStorage` that resets per test, and stubs for `matchMedia`,
`ResizeObserver`, and `scrollIntoView`.

Shared helpers live in `frontend/src/test/test-utils.tsx`:
`renderWithRouter`, `mockFetch`, `jsonResponse`, and fixture factories
(`makeUser`, `makeNode`, `makeJob`, …).

---

## End-to-end (Playwright)

The E2E suite drives **the locally-installed Google Chrome** via
`channel: "chrome"` (see `frontend/playwright.config.ts`) rather than a
Playwright-managed Chromium, because the Chromium CDN download is firewalled in
this environment.

> **Note:** E2E cannot run inside the Claude Code sandbox on this machine — macOS
> Seatbelt blocks Chrome from binding its `ProcessSingleton` socket under
> `$TMPDIR` (`bind() … Operation not permitted`). Run it in a normal terminal.

### Run (in a normal terminal, against a running stack)

```bash
# 1. Bring up the dev stack (UI :3000 proxying API :8000)
./dev.sh up

# 2. Run E2E (defaults to admin/admin dev creds; override via env)
cd frontend
NEXUS_E2E_USER=admin NEXUS_E2E_PASSWORD=admin npm run test:e2e

# Point at a different deployment
PLAYWRIGHT_BASE_URL=https://nexus.example.com npm run test:e2e
```

Specs live in `frontend/e2e/`; shared login helpers in `frontend/e2e/fixtures.ts`.
Credentials are read from `NEXUS_E2E_USER` / `NEXUS_E2E_PASSWORD` (never
hardcoded). The config reuses an already-running server; uncomment the
`webServer` block in `playwright.config.ts` to have Playwright manage it.

---

## Conventions

- **Never modify application source to make a test pass.** If a test surfaces a
  real bug, mark it `xfail` (pytest) / `.fails`/`.skip` with a reason
  (Vitest) and file the bug — don't mask it.
- Test real behavior. Mock only true external boundaries (network, object
  stores, SSH, unsafe subprocesses) — never the system under test.
- Cover positive, negative, and edge-case paths. Use descriptive test names.
