# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.6.x   | ✅ Published releases |
| 0.5.x   | ✅        |
| 0.4.x   | ✅        |
| < 0.4   | ❌        |

This policy applies to published releases. Check
[PyPI](https://pypi.org/project/moderato/) for available distributions; a dated
changelog and package version do not establish publication or security support
for an unreleased build. Existing 0.5.x and 0.4.x support is retained.

Version 0.6.x targets CPython 3.10–3.14 and removes Python 3.9;
published older releases retain their original Python metadata.
See the [Python support/deprecation policy](README.md#python-support-policy).
Library compatibility does not extend CPython's security lifecycle: Python 3.10
is retained for migration compatibility after its October 2026 upstream EOL.
Use an upstream security-maintained interpreter for production. This does not
change the published security-support status above.

## Reporting a vulnerability

Please do **not** report security vulnerabilities through public GitHub issues.

Two private options:

- Open a [private security advisory](https://github.com/Arjun-Aravind/moderato/security/advisories/new) on this repository (requires "Private vulnerability reporting" to be enabled in the repository settings), or
- Email **arjunaravind748@gmail.com** with `[security] moderato` in the subject.

Include:

- A description of the vulnerability and its impact
- Steps to reproduce or a proof of concept
- The version(s) of Moderato affected

You can expect an initial response within 7 days. Once a fix is released, the advisory will be published with credit (unless you prefer to remain anonymous).

## Security-relevant design notes

Moderato is infrastructure security tooling, so a few design decisions are worth knowing about when reporting:

- Keys are built from user-supplied identifiers. Identifier components are normalized and hashed when they exceed 100 characters to keep keys bounded; the limiter never passes raw, unbounded user input into Redis key slots.
- The default decorator scope is derived from the HTTP method and route path. Client-IP extraction from proxy headers (`X-Forwarded-For` and friends) is controlled by `trust_proxy_headers`: when enabled, these headers take precedence over the direct client address; leave it `False` unless the application is behind a reverse proxy you control that overwrites these headers.
- Rate-limit checks and updates run through atomic Lua scripts that never execute user-supplied strings. Fixed-window and sliding-window accounting use integer arithmetic. Since 0.5.0, public token policies use bounded exact integer refill arithmetic with millisecond time resolution; independent custom backend refill rates use floating-point math with explicit numeric bounds. Administrative `reset()` deletes keys directly with `SCAN`/`DEL` and does not interleave with checks atomically.
- Costs passed to `check()` are validated as strictly positive integers before any Redis call is made.

If you believe a rate limit can be bypassed or key state can leak across tenants or routes, that is a security bug — please report it privately.
