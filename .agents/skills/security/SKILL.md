---
name: security
description: Secrets, database access, untrusted tabular data and real-time system security rules.
---

# Security Skill

Treat uploaded/source data as untrusted.

Protect:
- database credentials
- service-role credentials
- OIDC configuration
- AI provider keys
- artifacts
- user identity

Rules:
- Least privilege.
- Service-role secrets stay server-side.
- Never log secrets/tokens.
- Consider prompt injection from cell contents.
- Keep raw sensitive data out of LLM prompts when unnecessary.
- Validate file paths and uploaded data.
- Review RLS policies before exposing data to user sessions.
- Do not confuse authentication with authorization.
