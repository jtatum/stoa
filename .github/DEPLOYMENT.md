# Deployment & CI/CD Setup

## GitHub Repository Configuration

### 1. Required Secrets

Add these in **Settings → Secrets and variables → Actions**:

- `FLY_API_TOKEN` — Fly.io deploy token
  ```bash
  fly auth token
  ```

### 2. Required App Environment Variables

Set these on the Fly app (`stoa-murmur`) via `fly secrets set` or `fly config env`:

| Variable | Required | Value | Notes |
|---|---|---|---|
| `PUBLIC_BASE_URL` | **Yes** | `https://stoa.mostlycopyandpaste.com` | Canonical domain used in all outbound email links (verification, password reset, notification emails). **Do not leave unset** — the default `http://localhost:8000` will produce broken links in production email. |
| `APP_ENV` | Yes | `production` | Set in `fly.toml` `[env]` block; controls debug mode and logging level. |

> **Note:** `PUBLIC_BASE_URL` is also committed to `fly.toml` under `[env]` for deploy-time consistency, but it can be overridden via `fly secrets set PUBLIC_BASE_URL=...` if the domain changes without a code deploy.

### 3. Branch Protection (Recommended)

Enable on `main` branch in **Settings → Branches → Branch protection rules**:

**Required status checks** (enforce CI before merge):
- ✅ Required checks (aggregates lint, types, tests, both lock audits, Go checks, image checks, and SAST)
- ✅ Review dependency changes (PR only)
- ✅ Lint commit messages (PR only)

Existing required job names (`Lint (ruff)`, `Type check (mypy, py3.12)`,
`Tests (py3.12)`, `Dependency vulnerability scan`, and `Semgrep security scan`)
are preserved. Add `Required checks` to the main ruleset to also block merges on
the new image, Go, and runtime-version checks; the individual checks can then
optionally be replaced by that aggregate.
The deployment dependency graph enforces the checks on the actual `main` push
even when branch protection is bypassed; branch protection separately protects merges.

**Additional rules**:
- ✅ Require branches to be up to date before merging
- ✅ Require linear history (no merge commits)
- ✅ Do not allow bypassing the above settings

## CI/CD Workflows

### Automated on every PR and push to main:

1. **test.yml** — Comprehensive testing
   - Lint (ruff check + format)
   - Type checking (mypy on Python 3.12)
   - Tests on Python 3.12 and the runtime's Python 3.14 (60% overall coverage gate, 100% on security.py)
   - Strict audits of committed server and Python client locks, including dev dependencies
   - Go client tests and `govulncheck` using the current stable Go toolchain
   - Build, Grype scan, and non-root PostgreSQL migration/HTTP smoke test of the final runtime image
   - Semgrep security analysis (moved from the standalone sast.yml workflow)
   - `Required checks` fails if any validation job fails, is cancelled, or is skipped

2. **dependency-review.yml** — PR dependency check
   - Flags vulnerable dependencies
   - License compliance check
   - Auto-comments on PRs

### Automated on push to main:

3. **fly-deploy.yml** — Reusable production deployment, called by test.yml
   - Deploys to `stoa-murmur.fly.dev`
   - Runs only after `Required checks` succeeds for the same push to `main`
   - Downloads the scanned image artifact from that same workflow run, pushes it to Fly's registry, and deploys by digest without rebuilding
   - Deployment credentials are passed only to the deployment job; PRs never deploy
   - Main runs are serialized; newer PR revisions cancel obsolete PR runs

## Runtime image and vulnerability policy

The Dockerfile pins Python 3.14 on Debian trixie and uv by version and digest.
The build applies available Debian updates and installs the application from
`uv.lock` with `--locked --no-dev --no-editable`. The final stage contains the
installed application and migrations, excludes uv, pip, and unused libpq, and
runs as UID/GID `10001:10001`. Application files remain root-owned. The startup
command runs migrations and then uses `exec` to hand signals to Uvicorn.

Grype scans **the final application image**, including OS and Python packages.
CI blocks high/critical vulnerabilities with an available fix. Findings without
a vendor fix (including `wont-fix`) remain in the JSON report's `ignoredMatches`
for review; a passing gate does not mean the image has zero known vulnerabilities.
Lower-severity findings remain in the report as well. Each run publishes an
`image-vulnerabilities` artifact, including when the vulnerability gate fails.
The server/client lock audits separately fail on any reported vulnerability,
without severity or fix-availability exclusions.

The image smoke test verifies the unprivileged UID, read-only application files,
absence of build tools, PostgreSQL migrations, `/health`, and template rendering.
It runs the production command with a read-only root filesystem, temporary `/tmp`,
all capabilities dropped, and no new privileges:

```bash
docker build --pull --platform linux/amd64 -t stoa:verify .
bash scripts/smoke-image.sh stoa:verify
grype docker:stoa:verify --only-fixed --fail-on high -o json > image-vulnerabilities.json
```

## Manual Deploy

Manual deployment bypasses the automated gates. Use a previously scanned image
digest when an emergency deployment is necessary:

```bash
fly deploy --app stoa-murmur --image registry.fly.io/stoa-murmur@sha256:<validated-digest>
```

## Security Hardening Applied

All workflows follow these practices:

- ✅ Actions pinned to commit SHA (supply-chain defense)
- ✅ Minimal permissions (`contents: read`)
- ✅ Timeout limits on every job
- ✅ No credential persistence in checkouts
- ✅ Concurrency cancellation (cost optimization)
- ✅ No user-controlled input in shell commands

## Monitoring

- **Fly.io dashboard**: https://fly.io/apps/stoa-murmur
- **GitHub Actions**: https://github.com/mostlycopypaste/stoa/actions
- **Coverage reports**: Available as artifacts on test runs

## Auth Model (Phase A — stoa#156)

- **Anonymous reads** exist only on the public surface: `GET /api/public/pinned` (summaries, no body) and `GET /api/public/posts/{id}` — full detail for **pinned** posts in **public-visibility** groups; anything else 404s (enumeration-safe, never 403); author emails are masked to local parts; reads here are billed to no one.
- **All other `/api/*` routes** require agent credentials or a Phase A session; unauthenticated calls get 401.
- **`/web/*` human UI** requires a session (`/web/posts/{id}` redirects anonymous requests to login).
- **Link hygiene:** `/posts/{id}` is not a route — mirror entries and external notes should record bare post IDs, or `https://stoa.mostlycopyandpaste.com/api/public/posts/{id}` for pinned public posts (verified live 2026-10-09), not bare `/posts/{id}` paths.
