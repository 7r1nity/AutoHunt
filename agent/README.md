# AutoHunt Backend MVP

This backend is a secure, single-orchestrator MVP. It includes FastAPI, a Playwright browser worker, SQLite job/event storage, file capability sandbox, persistent `memory.md`, versioned skill files, encrypted private profile storage, an async task broker, and a local MCP server.

## Security model

- The Agent has no shell, arbitrary path, arbitrary URL fetch, browser, or submission tool.
- API authentication uses `AUTOHUNT_API_KEY` and constant-time comparison.
- The Next.js browser client never receives the backend API key; a server route proxies requests.
- Email and phone are rejected from memory and stored only in the encrypted private profile.
- API keys and secret-looking values are rejected from memory and skills.
- MCP can only propose skill changes; an authenticated API action must approve them.
- Audit logs redact email, phone, and common secret patterns.
- DeepSeek keys are encrypted with Fernet, never returned by the API, and never written to audit bodies.
- Uploaded resumes, extracted text, candidate profiles, and structured job preferences are encrypted at rest. Resume-derived roles remain `inferred` until the user confirms them.
- Course reports, project documents, source files, and bounded ZIP archives become candidate evidence; dependency/build directories are ignored and ZIP files are never extracted onto the host filesystem.
- Explicit chat requests such as “把这段实习项目加入简历” create conversation-sourced evidence and rebuild the Markdown resume draft.
- Confirmed search keyword history is encrypted, versioned, and capped at five entries.
- Agent prompts are compiled at runtime from versioned structured preferences; raw resumes are never inserted into long-term public memory.
- Provider URLs and model IDs are server-side allowlists; the browser cannot submit a custom upstream URL.
- Browser navigation is restricted by the BOSS adapter to HTTPS BOSS job-detail URLs.
- CAPTCHA, login and MFA trigger `waiting_for_user`; the worker never attempts to solve or bypass them. Resume re-discovers the active BOSS tab and restores the requested URL instead of trusting a stale page handle.
- Real browser actions are serialized by a cross-process lock. Analysis workers may run concurrently, but browser sessions may not.
- Every browser checkpoint stores redacted login evidence and a private screenshot under `data/private/browser-evidence/`.
- Delivery requires a reviewable preview and a short-lived, one-time authorization. Ambiguous outcomes enter `outcome_indeterminate` and must never be retried automatically.
- Workspace writes are path-confined, size-limited, extension-allowlisted, and atomic.

## Setup

From `agent/`:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Replace the API key and generate a Fernet data key as described in `.env.example`.
`AUTOHUNT_DATA_KEY` must remain stable across restarts or previously saved credentials cannot be decrypted.

Run the API:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --env-file .env
```

### Connect an existing Chrome session (recommended)

From the repository root, start a dedicated Chrome profile with remote debugging:

```powershell
.\scripts\start-chrome-cdp.ps1
```

Log in to BOSS in that visible Chrome window. In AutoHunt choose **现有 Chrome** or **自动连接**. `auto` first tries `http://127.0.0.1:9223` and falls back to the managed Chromium profile. CAPTCHA and MFA are always completed by the user in the visible browser; AutoHunt only re-checks evidence after the user clicks continue.

Do not expose the CDP port outside localhost and do not use your everyday Chrome profile as the remote-debugging profile.

Automated test sources and runners were removed at the project owner's request.
Use `scripts/verify.ps1` from the repository root for syntax, type and build checks;
these checks do not replace behavioral or browser regression tests.

Run the local MCP server over stdio:

```powershell
.\.venv\Scripts\python.exe -m app.mcp.server
```

The MCP server exposes only allowlisted memory, sanitized candidate-profile, preference proposal/rendering, skills, and user-provided JD analysis tools. MCP preference changes remain proposals until an authenticated API action confirms them.

## API summary

- `POST /api/v1/chat`
- `POST /api/v1/browser/search`
- `GET/POST /api/v1/resumes`
- `GET/POST /api/v1/materials`
- `GET /api/v1/candidate-state`
- `PUT /api/v1/preferences`
- `GET/POST /api/v1/browser/tasks/current/...`
- `POST /api/v1/jobs/{id}/delivery-preview`
- `POST /api/v1/deliveries/{preview_id}/authorize`
- `GET /api/v1/jobs`
- `GET /api/v1/automation/stats`
- `GET/PUT /api/v1/llm/config`
- `POST /api/v1/llm/test`
- `GET /api/v1/memory`
- `PUT /api/v1/memory/facts`
- `GET /api/v1/skills/...`
- `GET/POST /api/v1/skill-proposals/...`
- `POST /api/v1/jobs/analyze`
- `POST /api/v1/tasks/job-analysis`
- `GET /api/v1/tasks/{id}`
- `GET/PUT /api/v1/private-profile`
