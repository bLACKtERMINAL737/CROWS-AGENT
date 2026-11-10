# Vercel and public deployment plan

## Current status

Crows's existing FastAPI application is designed for local-first use. Deploying the current HTML page to Vercel makes the page public, but **does not make the agent a safe public service**. The current API uses a shared `CROWS_API_TOKEN` or loopback-only fallback, and the application stores local task, session, memory, workflow, and workspace state in its configured SQLite/workspace paths. Do not expose that local-agent API directly to the internet.

## Safe first step: public UI preview

To deploy only the static UI for visual review:

1. Import `bLACKtERMINAL737/CROWS-AGENT` into Vercel.
2. Set the project Root Directory to `crows/static`.
3. Leave framework preset as Other and use no build command.
4. Deploy a preview URL for UI inspection only.

The page's API calls are relative to the current origin. In a static-only Vercel deployment, chat and agent operations will not work unless a compatible backend/proxy is deployed. Do not advertise a static-only preview as a working public AI assistant.

## Required architecture before a public beta

- **Vercel frontend:** public UI only; no provider keys, backend shared tokens, or privileged credentials in browser code or `NEXT_PUBLIC_*` variables.
- **Authenticated API boundary:** a server-side API/proxy layer validates each user's session and authorizes every request before forwarding it to the agent backend. Never embed the shared `CROWS_API_TOKEN` in frontend JavaScript.
- **Identity and isolation:** add sign-in and enforce user-scoped sessions, conversations, memory, tasks, workflows, exports, and file access. The current local SQLite store is not by itself a multi-tenant authorization layer.
- **Hosted backend:** run FastAPI on a supported persistent server/container host. Keep model provider keys in server-side environment variables. A Vercel static page cannot reach a user's localhost service.
- **Agent sandbox:** do not expose arbitrary local workspace, terminal, Docker, or coding-apply capabilities to anonymous users. Execute public-user work in per-user isolated sandboxes with strict resource limits, network policy, quotas, and explicit approvals.
- **Abuse and cost controls:** per-user/IP rate limits, request and output caps, concurrency limits, budget ceilings, monitoring, and a way to suspend abusive accounts.
- **Deployment configuration:** use HTTPS, exact allowed origins, secret rotation, backups, and a privacy/data-retention policy.

## Go-live checklist

- [ ] UI review completed at the Vercel preview URL.
- [ ] User authentication and server-side session validation implemented.
- [ ] Tenant isolation tests cover sessions, memories, tasks, workflows, exports, and files.
- [ ] Provider keys and backend credentials are absent from browser bundles and client storage.
- [ ] Backend CORS/Origin policy allows only the production frontend origin and is tested.
- [ ] Public coding/terminal tools are disabled until sandbox isolation is verified.
- [ ] Rate limits, usage quotas, and cost alarms are enabled.
- [ ] Automated tests and an end-to-end deployment smoke test pass.
- [ ] Privacy notice and terms are available before public sign-up.

## Recommendation

Treat the Vercel deployment as a **UI preview** until the authenticated API boundary and tenant isolation are implemented. The local shared-token mechanism is appropriate for a single-user development setup, not a public multi-user product.
