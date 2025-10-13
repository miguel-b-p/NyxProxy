# Security Policy

NyxProxy is a CLI that orchestrates third-party binaries (Xray-core, proxychains) and consumes
network resources provided by end users. This document explains how to report vulnerabilities
responsibly and outlines the security posture expected from contributors.

> **Important:** NyxProxy **does not guarantee privacy or anonymity**. It merely helps you manage
> proxies and relies on third-party infrastructure. Evaluate the trustworthiness of every proxy
> you load and apply additional protections as needed.

## Supported Versions

| Version | Supported             |
|---------|-----------------------|
| 1.x     | ✅ Full support       |

Patch releases are issued from the latest `1.x` branch as needed. Older releases will not receive
security fixes—upgrade to the most recent version instead.

## Reporting a Vulnerability

- Email `miguelpinotty@gmail.com` with the subject line `NyxProxy Security`.
- Include a clear description, reproduction steps, and any logs needed to validate the issue.
- Do **not** create a public GitHub issue for sensitive reports.
- Avoid sharing real proxy credentials or personal access tokens; sanitize examples whenever
  possible.

You should receive an acknowledgment within **3 business days**. If you do not hear back, feel free
to resend or ping via GitHub discussions.

## Guidelines for Test Cases

When demonstrating an issue:

1. Prefer synthetic URIs or the sample entries in `proxy.txt`.
2. Never include working production proxies or secrets in attachments or gists.
3. Note any required environment variables (for example `FINDIP_TOKEN`) and whether the bug depends
   on third-party services.

## Coordinated Disclosure Process

1. Acknowledge receipt and assess impact.
2. Work on a fix with priority given to high-severity issues.
3. Provide a patched release or mitigation guidance.
4. Credit the reporter (if desired) once the fix is public.

Please allow a reasonable window for remediation before sharing details publicly.

## Hardening Recommendations

- Keep `xray` up to date; the CLI delegates all bridge work to that binary.
- Store secrets in the environment or external secret managers—avoid committing `.env`.
- Run `nyxproxy` inside unprivileged containers or virtual environments when testing untrusted
  proxy lists.
- Review configuration files under `~/.nyxproxy/` and ensure they are writable only by the current
  user.
