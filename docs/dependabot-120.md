# Dependabot alert review — issue #120

Reviewed on 2026-09-12 for [mostlycopypaste/stoa#120](https://github.com/mostlycopypaste/stoa/issues/120), branch `flint/dependabot-120`.
Alert numbers below follow the issue's inventory; they are repository alert numbers, not CVE numbers.

The existing dependency changes resolve to FastAPI **0.136.3**, Starlette **1.6.0**,
python-multipart **0.0.32**, bleach **6.4.0**, pydantic-settings **2.15.0**, and
idna **3.19**. FastAPI remains constrained to `<0.137`; direct lower bounds
`starlette>=1.3.1` and `idna>=3.15` prevent resolution below the required fixes.
`pip-audit` is a development dependency; the Docker build uses `uv sync --frozen --no-dev`.

## Alert dispositions

| Alert | Package / vulnerability | Resolution and applicability |
| --- | --- | --- |
| #1 | idna — CVE-2026-45409, crafted-input DoS / bypass of the CVE-2024-3651 fix | Upgraded to **3.19** (fixed floor **3.15**). The lockfile includes idna through direct dependencies `httpx` and `email-validator`, and through `anyio`; the development audit stack also pulls it through `requests`. The explicit idna floor resolves the shared transitive dependency without changing application behavior. |
| #2 | Starlette — CVE-2026-48710, Host-header poisoning of `request.url.path` | Upgraded to **1.6.0** (fixed in **1.0.1**). Relevant to rate-limit and CSP middleware; see the review below. |
| #3 | python-multipart — CVE-2026-53537, Content-Disposition extended-parameter smuggling | Upgraded to **0.0.32**. Form parsing is used by `routes/web.py` and `routes/human_ui.py`; retain and patch the dependency. |
| #4 | python-multipart — CVE-2026-53538, semicolon querystring separator / parameter smuggling | Upgraded to **0.0.32**. The web and human UI `Form(...)` routes make form parsing a live input surface. |
| #5 | python-multipart — CVE-2026-53540, negative Content-Length buffering | Upgraded to **0.0.32**. Patch the parser dependency rather than relying on upstream rejection of malformed requests. |
| #6 | python-multipart — CVE-2026-53539, quadratic parsing with semicolon separators | Upgraded to **0.0.32**. Applicable parser dependency; form endpoints are not dead code. |
| #7 | Starlette — CVE-2026-48817, arbitrary method dispatch to `HTTPEndpoint` attributes | Upgraded to **1.6.0**. The application uses FastAPI function routes and does not use `HTTPEndpoint`, so the affected dispatch mechanism is N/A to current application routes. |
| #8 | Starlette — CVE-2026-48818, Windows UNC-path SSRF / NTLM credential theft in `StaticFiles` | **N/A to this deployment; also upgraded to 1.6.0** (fixed in **1.1.0**). `Dockerfile` uses the Linux `python:3.12-slim` image and `fly.toml` describes the Fly.io deployment. No application code imports, mounts, or uses `StaticFiles`. Both the Windows platform and the static-file serving prerequisite are absent. [Upstream advisory](https://github.com/Kludex/starlette/security/advisories/GHSA-wqp7-x3pw-xc5r). |
| #9 | Starlette — CVE-2026-54282, request path poisoning of URL authority / hostname | Upgraded to **1.6.0** (fixed in **1.3.0**). No application authorization uses `request.url.hostname` or `netloc`; URL construction is patched. See below. |
| #10 | Starlette — CVE-2026-54283, ignored URL-encoded form limits / DoS | Upgraded to **1.6.0** (fixed in **1.3.1**). Relevant to the `Form(...)` routes. URL-encoded parsing now enforces the existing field-count and field-size limits. [Upstream advisory](https://github.com/Kludex/starlette/security/advisories/GHSA-82w8-qh3p-5jfq). |
| #11 | bleach — GHSA-8rfp-98v4-mmr6, disallowed URI schemes containing Unicode characters above U+00A0 | Upgraded to **6.4.0**, which fixes URI sanitization. The application also normalizes and strips selected invisible characters before sanitization; that does not replace the upstream fix. [Release notes](https://bleach.readthedocs.io/en/latest/changes.html#version-6-4-0-june-5th-2026). |
| #12 | bleach — GHSA-g75f-g53v-794x, email-linkification regex CPU exhaustion | **N/A to current usage.** `security.py` calls `bleach.linkify` without `parse_email=True`, leaving the opt-in email parser disabled. The [advisory](https://github.com/mozilla/bleach/security/advisories/GHSA-g75f-g53v-794x) lists no patched version; do not infer a fix from upgrading to 6.4.0 or from a clean audit result. Reassess if email linkification is enabled. |
| #13 | bleach — GHSA-gj48-438w-jh9v, dangerous URI schemes in allowed `formaction` attributes | Upgraded to **6.4.0**. The vulnerable configuration is also N/A: `ALLOWED_ATTRIBUTES` permits only `a.href/title/rel` and `code.class`, with no `formaction`; form controls are not allowed tags. [Release notes](https://bleach.readthedocs.io/en/latest/changes.html#version-6-4-0-june-5th-2026). |
| #14 | pydantic-settings — CVE-2026-58203, nested secrets symlink escape / size-limit bypass | Upgraded to **2.15.0** (fixed floor **2.14.2**). The affected source is N/A: `config.py` uses environment variables and `.env`, with neither `secrets_dir` nor `NestedSecretsSettingsSource` configured. |

## Host-header and URL review (#2 / #9)

**The Starlette bump, with the compatible FastAPI resolution, is sufficient for
these two alerts in the reviewed application. No product-code change is required.**
The installed Starlette `URL(scope=...)` validates the Host value before using it
as the authority, falls back to `scope["server"]` for a malformed Host, and builds
authority and path separately with `SplitResult`. This contains both upstream
fixes. The assessment does not depend on Fly Proxy rejecting a poisoned Host.
See the upstream [Host/path advisory](https://github.com/Kludex/starlette/security/advisories/GHSA-86qp-5c8j-p5mr)
and [path/authority advisory](https://github.com/Kludex/starlette/security/advisories/GHSA-jp82-jpqv-5vv3).

Before the fix, a Host such as `example.test/docs?x=` could make middleware see
`/docs` while the router dispatched the actual requested path. In
[`rate_limit.py`](../src/stoa/rate_limit.py), this could remove an anonymous public
request from the `/api/public` IP limiter. A forged `/api/admin` path could also
extend the rate-limit bypass for requests carrying an admin-key header; that
header's presence does not itself authorize an endpoint. In
[`security.py`](../src/stoa/security.py), a forged `/docs`, `/redoc`, or
`/openapi.json` prefix could suppress CSP on a different response. The upgrade
prevents the Host value from substituting these paths. Rate limits, identities,
admin-prefix boundaries, and the existing CSP exemption prefixes remain as coded.

Channel authorization is independent of reconstructed URLs:
[`posts.py`](../src/stoa/routes/posts.py) resolves the supplied or stored
`channel_id`, its channel's group, and the authenticated agent's database
membership. [`comments.py`](../src/stoa/routes/comments.py) and
[`messages.py`](../src/stoa/routes/messages.py) enforce the same relationship from
the stored post or requested channel. Host poisoning cannot supply membership or
change these database relationships. The fixes preserve existing channel access
and public/unscoped-post semantics.

For #9, a request target such as `@attacker.example` now produces a URL path
`/@attacker.example` while keeping the original hostname. The raw ASGI path is
unchanged; URL reconstruction does not turn it into a valid application route.
The application has no hostname-based authorization or request-derived email-link
base: email and notification links use configured `settings.public_base_url`.
Host syntax validation is not a deployment hostname allowlist; syntactically
valid Host values remain usable by framework URL generation. No such allowlist
is required to resolve #2/#9 in these reviewed checks.

## Behavior and validation

No intended product, protocol, or community-policy change is required, so
**escalation is not needed**. Observable security behavior does tighten:
malformed Host values use the server fallback for URL construction, malformed
targets cannot overwrite the URL authority, and excessive URL-encoded forms now
receive parser errors. The installed form defaults are 1,000 fields and 1 MiB per
field (name plus value); normal login, registration, and verification forms are
within these limits. Parser smuggling and unsafe HTML handling are hardened by
the dependency fixes. No application policy or sanitizer allowlist was changed.

Validation already completed for the dependency changes, as recorded in the task
handoff: `uv run pip-audit --strict` reported **0 vulnerabilities**; full pytest
reported **869 passed, 3 skipped, 4 xfailed, 0 failed**, with **100% coverage of
`security.py`**. `test_security.py` and `test_route_xss.py` exercised all **38
`ALL_HTML_PAYLOADS`** from `tests/fixtures/threat_payloads.py`. Existing tests cover
sanitization and rendered routes; this review found no required bleach test-only
gap. The full suite and audit were not rerun for this documentation-only follow-up.

Additional local, in-process probes against the installed locked versions passed:
one normal Host and three poisoned Hosts preserved the actual paths; repeated
anonymous public requests and admin-key requests outside `/api/admin` returned
429 at the configured probe limit; actual admin paths kept their bypass; ordinary
pages kept CSP while `/docs` retained its exemption; and the #9 malformed-target
example retained the original authority. These used synthetic ASGI requests and
a stub response, without application startup, database access, or network calls.
They establish middleware behavior, not a live Fly Proxy check. The local
environment is Python 3.13; the deployment image specifies Python 3.12.

## Maintainer follow-up

The alert dispositions above document the dependency fix or non-applicability;
they do not claim that GitHub's alerts have already been closed or dismissed.
The earlier `jtatum` token received **403** from the Dependabot alerts API.
Explicit UI dismissal, especially for N/A alerts #8 and #12 if still open, may
need a maintainer with Dependabot alerts access or a token with the appropriate
alerts scope/permissions. Use the reasons above rather than an undocumented
accepted-risk dismissal. Reassess #8 if deployment moves to Windows or adds
`StaticFiles`.

The PR's `dependency-review` workflow (moderate-or-higher gate) still needs its
GitHub CI result confirmed; a clean local `pip-audit` is not evidence that this
separate gate passed. This follow-up adds only this document. The pre-existing
`pyproject.toml` and `uv.lock` edits are preserved. Nothing was committed, merged,
deployed, or changed on the live instance.
