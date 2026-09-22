# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.3.x   | ✅        |
| < 0.3   | ❌        |

## Reporting a vulnerability

Please do **not** report security vulnerabilities through public GitHub issues.

Instead, open a [private security advisory](https://github.com/Arjun-Aravind/moderato/security/advisories/new) on this repository. Include:

- A description of the vulnerability and its impact
- Steps to reproduce or a proof of concept
- The version(s) of Moderato affected

You can expect an initial response within 7 days. Once a fix is released, the advisory will be published with credit (unless you prefer to remain anonymous).

## Security-relevant design notes

Moderato is infrastructure security tooling, so a few design decisions are worth knowing about when reporting:

- Keys are built from user-supplied identifiers. Identifier components are normalized and hashed when they exceed 100 characters to keep keys bounded; the limiter never passes raw, unbounded user input into Redis key slots.
- The default decorator scope is derived from the HTTP method and route path. Proxy header spoofing (`X-Forwarded-For` and friends) is handled by an explicit client-IP extraction policy — see the README's security section for how to configure trusted proxies.
- The Lua scripts are the sole writers of rate-limit state. They are atomic, use integer-only arithmetic, and never execute user-supplied strings.
- Costs passed to `check()` are validated as strictly positive integers before any Redis call is made.

If you believe a rate limit can be bypassed or key state can leak across tenants or routes, that is a security bug — please report it privately.
