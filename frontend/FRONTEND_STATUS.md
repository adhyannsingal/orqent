# Orqent frontend - status

**Branch:** `frontend` at `0e93ae5`
**Stack:** React 19, TypeScript, Vite, Tailwind v4, React Flow, TanStack Query,
Zustand, React Router, Lucide, sonner  
**Run:** `npm run dev` from `frontend/` with the backend on `:8000`

## Backend Coverage Matrix

| Backend capability | Frontend surface | Status | Demo-critical |
|---|---|---:|---:|
| auth | Landing -> login/register, restore, logout | ✅ usable | yes |
| workflows | List/create/open workflow | ✅ usable | yes |
| drafts | Builder save/reload with revision | ✅ usable | yes |
| validation | Validate action + issue panel | ✅ usable | yes |
| publish | Publish action + one-time webhook result | ✅ usable | yes |
| node types | Palette from `/node-types` | ✅ usable | yes |
| manual trigger | Builder node + Run action | ✅ usable | yes |
| webhook trigger | Publish URL dialog + copy | ✅ usable | yes |
| schedule trigger | Cron field, presets, UTC note | ✅ usable | yes |
| condition | Config form + true/false handles | ✅ usable | yes |
| merge | Catalogue node + named inputs | ✅ usable | no |
| AI agent | Instructions/model/temp/RAG/tools | ✅ usable | yes |
| RAG | Retrieval toggle + top_k | ✅ usable | yes |
| tools | Calculator allow-list toggle | ✅ usable | yes |
| documents | Ingestion/replacement form | ✅ usable | yes |
| runs | Run list/detail/polling | ✅ usable | yes |
| events | Timeline panel | ✅ usable | yes |
| resume | Resume button for waiting run | ✅ usable | no |

## P0

All P0 items are usable: login/register, workflow list/create/open,
add/connect/delete nodes, edit config, AI Agent config, RAG top_k, calculator
tool selection, save draft, validate, publish, ingest knowledge, start run,
inspect run, see final outputs, and see run/node errors.

## P1

Verified usable against the backend: webhook publish/token/delivery, schedule
cron validation/publish, condition branching, skipped node execution, run event
timeline, and suspended run resume.

## Final Polish

- Added a public landing page at `/` with Ø Orqent branding, a compact product
  hero, repo link, and direct sign-in/register paths.
- Added light/dark theme support with a persisted non-secret `orqent.theme`
  preference and theme-aware toast/canvas styling.
- Removed visible raw user/org/run/document identifiers from shell, run list,
  run detail, and knowledge success copy where they were not useful.
- Added register-only password policy UX and backend request validation:
  8+ characters, at least one letter, one number, and one special character.
- Kept existing builder/runtime architecture intact; no backend architecture
  changes were made.

## Security Review

- One client env var: `VITE_API_BASE_URL`.
- No `GEMINI_API_KEY`, `DATABASE_URL`, `PRIVATE_KEY`, `SECRET_KEY`, MySQL URL,
  `api_key`, or provider credential in frontend source or production assets.
  The only secret-scan match is warning text in `frontend/.env.example`.
- Browser calls only Orqent API routes; no Gemini, Chroma, or MySQL client.
- Access token stays in memory; refresh token is the only persisted credential.
- Concurrent 401 responses share one refresh request.
- Logout clears local credentials; terminal 401 clears auth globally.
- No app-source `console.*`.
- No `dangerouslySetInnerHTML`; AI output, document text, and errors render as
  escaped React text.
- Webhook token is kept only in publish dialog state, copied on demand, and not
  stored in localStorage/sessionStorage.
- UI does not author tenant ids; document ingestion and RAG config send no
  organization fields.

## Demo Result

Live backend, 2026-08-20:

Register/login -> ingest policy text -> create `trigger.manual -> ai.agent ->
core.log` -> enable retrieval top_k 3 + calculator -> save -> validate ->
publish -> run -> `COMPLETED`.

Agent output included the retrieved allowance fact (`40 GBP`) and calculated
three-day total (`120 GBP`). Downstream log node succeeded.

Provider-independent P1 checks previously passed:

- Webhook workflow published with one-time token and delivered via `/hooks/{token}`.
- Schedule workflow published with `0 9 * * *` UTC cron.
- Condition workflow completed with false branch `SKIPPED`.
- Wait workflow suspended with a resume token and completed after resume.

## Verification

- TypeScript: `npm run build` passed (`tsc -b`).
- Lint: `npm run lint` exits 0 with 6 advisory React warnings.
- Backend auth schema lint: `python -m ruff check src/app/schemas/auth.py tests/unit/test_auth_endpoints.py` passed.
- Backend auth pytest: blocked in this shell because backend dependencies are
  not installed (`ModuleNotFoundError: structlog`).
- Production build: passed.
- `git diff --check`: clean.
- Browser smoke: Playwright Chromium screenshots verified landing/register at
  desktop and mobile sizes.
- Bundle:
  - initial JS `index-B08EzEOw.js`: 256.36 kB / 78.66 kB gzip
  - React Flow/node chunk `OrqentNode-BXkY5RAi.js`: 181.51 kB / 58.99 kB gzip
  - builder route chunk: 24.01 kB / 7.83 kB gzip
  - run detail route chunk: 10.23 kB / 3.32 kB gzip
  - landing route chunk: 9.08 kB / 2.90 kB gzip
  - CSS: 50.94 kB / 10.00 kB gzip
- Vite dev server served the app at `http://127.0.0.1:5173/`.

## Deferred

- No document list/delete UI; backend exposes ingestion/replacement only.
- No version history UI, analytics, settings, team management, streaming, or
  collaborative editing.
- No committed Playwright suite; browser smoke used the Playwright CLI and
  cached Chromium without changing project dependencies.

## Known Limitations

- The builder allows drawing type-invalid edges and relies on backend validation
  to report the exact issue. This keeps the backend authoritative.
- `core.log` accepts `Text`; trigger outputs are `Json`, so trigger-to-log graphs
  correctly validate as incompatible. Use `ai.agent`, `core.constant`, or
  `core.noop` depending on the intended data flow.
- Vite warns that `vite.config.ts` uses `__dirname`, which future native config
  loading will not support. Current build/dev still pass.
