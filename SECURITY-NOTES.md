# Security Notes — BK JR MCP

**Date:** 2026-08-13 · **Author:** Hermes (audit during BK JR MCP enhancement)

---

## ⚠️ HISTORICAL: secrets committed in `render.yaml` (now removed)

`render.yaml` (deleted in this branch's cleanup) tracked plaintext API keys
in git from 2026-08-07 until 2026-08-13. The exposed keys were:

| Key | First commit | Status |
|---|---|---|
| `OPENPHONE_API_KEY` | `000a669` (2026-08-07) | ✅ Now lives in Coolify env vars only |
| `RETELL_API_KEY` | `000a669` (2026-08-07) | ✅ Now lives in Coolify env vars only |
| `RETELL_WEBHOOK_SECRET` | `000a669` (2026-08-07) | ✅ Now lives in Coolify env vars only |
| `SUPABASE_SERVICE_KEY` | `000a669` (2026-08-07) | ✅ Now lives in Coolify env vars only |
| `SUPABASE_ANON_KEY` | `000a669` (2026-08-07) | ✅ Now lives in Coolify env vars only |
| `SUPABASE_PUBLISHABLE_KEY` | `000a669` (2026-08-07) | ✅ Now lives in Coolify env vars only |
| `TURSO_AUTH_TOKEN` | `000a669` (2026-08-07) | ✅ Now lives in Coolify env vars only |
| `BOLD_TOOL_SECRET` | `000a669` (2026-08-07) | ✅ Now lives in Coolify env vars only |
| `AUTH_JWT_SECRET` | `000a669` (2026-08-07) | ✅ Now lives in Coolify env vars only |
| `BOLD_TEAM_SECRET` | `000a669` (2026-08-07) | ✅ Now lives in Coolify env vars only |
| `BOLD_BUSINESS_AGENT_ID` | `000a669` (2026-08-07) | ✅ Now lives in Coolify env vars only |
| `DEEPGRAM_API_KEY` | `000a669` (2026-08-07) | ✅ Now lives in Coolify env vars only |
| `COMPOSIO_API_KEY` | `000a669` (2026-08-07) | ✅ Now lives in Coolify env vars only |
| `COMPOSIO_USER_ID` | `000a669` (2026-08-07) | ✅ Now lives in Coolify env vars only |
| `COMPOSIO_CONNECTED_ACCOUNT_ID` | `000a669` (2026-08-07) | ✅ Now lives in Coolify env vars only |
| `MCP_AUTH_TOKEN` | `b44e8ee` (2026-08-07) | ✅ Now lives in Coolify env vars only |
| `SMS_AGENT_API_KEY` | `000a669` (2026-08-07) | ✅ Now lives in Coolify env vars only |

**The keys still exist in git history.** `git log -p render.yaml` will
surface every value. They are NOT removed by the file deletion. The only
ways to fully erase are:
1. `git filter-repo` + force-push (destructive, rewrites shared history)
2. **Rotating the keys** on the actual service providers — recommended

---

## 🔴 RECOMMENDED: rotate every exposed key

Treat every value in the table above as **public**. Rotate them at the
provider, update Coolify env vars, restart services. Until rotation, an
attacker with read access to the repo has live production credentials.

| Provider | Rotation path |
|---|---|
| OpenPhone / Quo | Settings → Integrations → API → Regenerate |
| Retell | Dashboard → API Keys → Revoke + Create |
| Supabase | Project → Settings → API → Roll service-role JWT secret |
| Turso | Dashboard → DB → Rotate token |
| Composio | Dashboard → API Keys → Regenerate |
| Deepgram | Console → API Keys → Roll |
| Bold Connect (internal) | Coordinate with Bold Business dev team |
| BK JR (internal) | Coordinate with Bold Business dev team |

After rotation:
1. Update each Coolify app's env vars (bkjr-backend + bkjr-mcp + any staging apps)
2. Trigger redeploy (`coolify trigger_deploy` or push to `main`)
3. Verify live endpoints still healthy (`curl https://bkjr-api.getbijou.xyz/health`)

---

## 🛡️ Going forward

- **All secrets → Coolify env vars.** The repo must NEVER contain a
  literal API key, token, or password. `.env*` is gitignored; `render.yaml`
  is gitignored; the deployment manifests reference env-var names only.
- **`render.yaml` re-creation is blocked.** Added to `.gitignore` —
  accidental recreation won't be committed.
- **Coolify env vars are encrypted at rest** in the Coolify database, and
  the UI masks them on display. They are visible only to operators with
  Coolify admin access.
- **Webhook signing keys** stay on the provider side and are verified
  server-side (see `retell_client.verify_webhook`).

---

## Reporting

If you discover a new secret leak, **do not** push a fix to git. Rotate
the key, then update env vars, then optionally commit the
fix-and-rotation PR with the secret value redacted in the diff.