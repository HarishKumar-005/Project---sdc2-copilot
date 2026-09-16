---
name: supabase
description: Safe local and remote Supabase/PostgreSQL project setup for SCD2 Copilot.
---

# Supabase Skill

Use official Supabase CLI workflows and version-controlled migrations.

Project structure:
supabase/config.toml
supabase/migrations/
supabase/seed.sql

Rules:
- Never commit secrets.
- Never put service-role credentials in frontend/UI code.
- Keep schema changes in migrations.
- Keep repeatable demo data in seed.sql or controlled seed scripts.
- Test locally before pushing remote migrations.
- Prefer PostgreSQL primitives that remain portable.
- Treat Supabase Realtime as a transport option, not business logic.
- Do not couple the SCD2 engine to Supabase-specific APIs.

Required checks:
- local startup/reset
- migration apply
- seed reproducibility
- remote link/push
- RLS/security review
