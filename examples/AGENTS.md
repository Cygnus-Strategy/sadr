# AGENTS.md

This repo builds the slicer command-line tool and its Cloudflare Worker.

## Sadr

### Build and test
- `uv sync --locked && uv run slicer build examples/demo.md`
- No test suite. Also run `uv run python scripts/smoke.py`.

### Toolchain
- uv for the root project and `worker/`. CI pins uv with the exact `version:` input of setup-uv.
- worker/ resolves packages from the Pyodide index, which has no upload dates. Exempt worker/ from exclude-newer.

### Related repos
- Builds against Cygnus-Strategy/slicer-themes at its default branch, checked out as ../slicer-themes.

### Deploys outside CI
- Cloudflare Workers Builds deploys `worker/` with its own uv. It resolves again from worker/pyproject.toml, so that file pins exact versions.

### Don't touch
- `migrations/` is generated. Do not edit it.

### Holds
- Hold: Python at 3.14 (newest 3.15). Cloudflare Python Workers do not support 3.15 yet. Added 2026-10-19T03:17Z, revisit 2026-11-16T03:17Z.
- Maintainer test: the Worker's cold start after the Pyodide 0.29 upgrade needs a check in the Cloudflare dashboard. Added 2026-10-20T14:02Z, revisit 2026-11-17T14:02Z.

### Security notes
- `scripts/` runs only on a developer's machine. It never handles untrusted input.

### Settings
- Auto-merge facts PRs, and upkeep PRs with minor updates. Enhancement PRs need review.
- Stay on the Node LTS before the newest one.
- Leave `workers-py` alone. Cloudflare pins it.
- Request review from the CODEOWNERS for each changed path.
- Build enhancement issues, at most two per run.
- Run at most once a day.
