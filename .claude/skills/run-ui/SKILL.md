---
name: run-ui
description: Launch job-search-app's backend + frontend and drive the React UI with a headless Playwright browser (chromium-cli is not available in this environment) — use when asked to run, test, or screenshot a frontend/UI change, or to confirm a fix works live rather than just by reading the code.
---

# Running and driving job-search-app's UI

This project has no test framework (no vitest/jest, no Playwright already
wired in). To actually observe a frontend change, launch both servers and
drive a real headless browser against them — don't just read the React code
and reason about it.

## 1. Check for already-running dev servers first

The user often already has `start-local.bat` running (backend on `:8000`,
Vite on `:5173`). Check before starting your own:

```bash
curl -sf http://localhost:8000/api/jobs?include_skipped=true >/dev/null && echo "backend already up on 8000"
```

If `:8000` responds, **reuse it** — don't spawn a second backend (SQLite +
single-run guard don't love two writers). If you need your own for isolation,
note the existing one's PID before touching anything.

For the frontend, `npm run dev` (in `frontend/`) is safe to run again even if
`:5173`/`:5174` are taken — Vite auto-increments the port (`5175`, `5176`,
...) and prints which one it picked. **The proxy target is always
`localhost:8000`** regardless of which port Vite lands on (see
`frontend/vite.config.ts`), so this is transparent.

```bash
cd frontend && npm run dev > /tmp/frontend.log 2>&1 &
echo $! > /tmp/frontend.pid
sleep 3 && tail -5 /tmp/frontend.log   # find the port it actually bound
```

Stop only what you started: `taskkill //PID <pid> //T //F` (the `//T` kills
the child esbuild process Vite spawns too). Do not touch a pre-existing
instance on 5173/5174 you didn't launch.

## 2. Get a headless browser — chromium-cli is not installed here

This environment doesn't have `chromium-cli`. Use Playwright directly:

```bash
npx --yes playwright@1.48 install chromium   # one-time, ~140MB, a couple minutes
```

Then make the `playwright` npm module resolvable from wherever your driver
script lives (scratchpad is fine) — e.g. `npm install playwright@1.48
--no-save` in that directory, or point `NODE_PATH` at
`frontend/node_modules` if it's already a dependency there.

## 3. Driver script pattern

Write a small Node script (not a REPL) using `playwright` directly:

```js
const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  try {
    await page.goto("http://localhost:5175/", { waitUntil: "networkidle" });
    await page.waitForSelector(".stats");
    // ...interact, assert, screenshot...
  } finally {
    await browser.close();   // put cleanup in finally — an assertion/timeout
                              // failure mid-script otherwise leaves an orphaned
                              // headless chrome.exe process running.
  }
})().catch((e) => { console.error("SCRIPT FAILED:", e); process.exit(1); });
```

## Gotchas specific to this app

- **`JobState` is sacred (see root `CLAUDE.md`).** Clicking a status button
  (`applied`/`review later`/`pass`/`closed`) PATCHes real, persisted user data
  — not a mock. If you mark a job's status to test something, **revert it
  back to empty status afterward**, either by clicking the same button again
  in the UI (toggles off) or directly:
  ```bash
  curl -sf -X PATCH "http://localhost:8000/api/jobs/$(python -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" '<job-id>')" \
    -H "Content-Type: application/json" -d '{"status":""}'
  ```
  Do this even if a later script step throws — check via `curl .../api/jobs`
  that the target job's `status` is back to `null` before finishing.

- **Passed/Closed and Skipped sections are collapsed by default**
  (`App.tsx`'s `collapsed` state inits `passed: true, skipped: true`). A job
  you just marked "pass" disappears from view in that run — clicking its
  buttons again requires first clicking the section header
  (`.section-header.passed`) to expand it. Don't assume a card stays visible
  or clickable after a status change moved it to a different bucket.

- **Default filter tab is "pending"** (`filter` state inits to `"pending"`,
  which hides anything with a status already set). Click the `all` filter tab
  first if the job you need to interact with might already have a status.

- **Never kill `chrome.exe` broadly to clean up.** This machine runs the
  user's actual browser alongside test runs — there can be 20+ `chrome.exe`
  processes that are just their open tabs. Only target the PID your own
  script's `browser.close()` didn't clean up, and identify it precisely:
  ```powershell
  Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
    Where-Object { $_.CommandLine -like '*ms-playwright*' } |
    Select-Object ProcessId, CommandLine
  ```
  If nothing matches, there's nothing to clean up — don't broaden the filter.

## Representative interaction (known-good as of this writing)

Loading `/`, switching to the `all` filter, finding a Review-section job by
title, clicking its `pass` button, and asserting `.stat.review .num` /
`.stat.passed .num` changed live — this is the exact path used to verify the
stats-bar live-update behavior. `.stats`, `.stat.review .num`,
`.stat.passed .num`, `.card-title`, and `button.btn` (with `hasText` for the
status label) are the selectors that matter; see `frontend/src/App.tsx` and
`frontend/src/components/JobCard.tsx` for their source.
