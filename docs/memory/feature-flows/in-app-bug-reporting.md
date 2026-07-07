# In-App Bug Reporting, Feature Requests & Feedback (#1116)

> Users report **bugs**, submit **feature requests**, and send general
> **feedback** from the floating **Help widget** without the deployed instance
> holding any GitHub credentials. Bugs and feature requests are filed as
> labelled GitHub issues in `abilityai/trinity`; **feedback is private** —
> emailed to the Trinity team only, never made public. Every report also emails
> the team. Reporters may optionally leave a contact email for follow-up (kept
> private — see below).

## Why a hosted intake

Trinity is open-source and self-hosted, so a deployed instance has no token to
file issues against `abilityai/trinity`. Reports POST to an **Ability.ai-operated
intake endpoint** that holds the GitHub token server-side — the same hosting
pattern as the `ask-trinity` Q&A function the widget already uses.

**Permanent, replaceable URL.** The widget URL is compiled into every frontend
bundle and can't be updated on already-deployed instances, so the intake is
fronted by a stable Cloudflare-managed vanity domain —
`https://intake.abilityai.dev/v1/report-bug`. The `/v1/` prefix versions the
contract; the backing service can be redeployed/replaced behind the domain
forever. (The intake service itself lives **outside this repo** in
`trinity-ops-agent/bug-intake/` — a Cloudflare Worker.)

## Flow

```
HelpChatWidget tabs: Ask · Bug · Feature · Feedback
  → form (title + description required; optional contact email)
  → client builds diagnostics + CLIENT-SIDE SCRUB (utils/scrub.js)
  → REVIEW screen (see-before-send): scrubbed preview + public/private warning
  → POST {type, title, description, email?, install_id, diagnostics} → intake Worker
       Worker: guards → SERVER-SIDE re-scrub → soft per-install cap → dedupe (type+content)
               bug/feature → create GitHub issue (type-bug | type-feature, + source:in-app)
               feedback    → NO issue (private)
               → email the team via Resend (reply-to = reporter email if given)
       ← { ok, type, deduped, issue_url? }   // issue_url only for bug/feature
  → success state (issue link for bug/feature; "sent" for feedback)
```

`type` is `bug` (default), `feature`, or `feedback`. Bugs get `type-bug`,
feature requests `type-feature` (both + `source:in-app`) for human triage;
**feedback files no issue** — it is delivered only as the private team email.

## Frontend (in this repo)

- `src/components/HelpChatWidget.vue` — adds **Bug**, **Feature**, and
  **Feedback** tabs alongside the existing Q&A **Ask** (no second floating
  button); all three share one form keyed on `mode`, with type-aware copy. The
  review warning is public-vs-private — feedback shows a "sent privately, not
  public" note instead of the public-issue warning. Stages: `form → review → success`.
  Form takes title, description, and an **optional contact email** (not scrubbed;
  shown in the review behind a "private — not published" note). The review stage
  is the explicit opt-in confirm (verified: no auto-submit) and renders exactly
  what will be sent. Returned `issue_url` is link-rendered only if it matches
  `https://github.com/` (href-injection guard). Gated by `bugReportingEnabled`.
- `src/utils/consoleBuffer.js` — capped ring buffer (60 entries) of recent
  `console.error`/`warn` + `window.error`/`unhandledrejection`, installed in
  `src/main.js` **before app mount** so early errors are captured.
- `src/utils/scrub.js` — client mirror of the Worker's scrubber; scrubs title,
  description, route/url, and console lines so the review screen shows the
  already-scrubbed payload.
- **Config knobs** (build-time): `VITE_BUG_INTAKE_URL` (default the stable
  domain — operators can repoint) and `VITE_BUG_REPORTING_ENABLED=false` to hide
  the tab entirely. Vite statically inlines `import.meta.env.*` at `npm run
  build`, so these are **build args, not runtime env** — the prod build path
  plumbs them the same way as `VITE_API_URL`: `ARG`+`ENV` in
  `docker/frontend/Dockerfile.prod` (before `RUN npm run build`) and
  `${VAR:-default}` under the frontend `build.args` in `docker-compose.prod.yml`,
  with the compose default == Dockerfile ARG default == the code default here so
  an unset `.env` reproduces today's behavior. Disabling therefore requires a
  frontend image rebuild. `.env.example` documents both (#1489, umbrella #1485).
  Static regression guard: `tests/unit/test_1489_vite_bug_build_args.py`.
- **CSP**: `https://intake.abilityai.dev` added to `connect-src` in **both**
  `vite.config.js` (dev) and `security-headers.conf` (prod nginx) — without this
  the browser blocks the POST (the April 2026 `ask-trinity` CSP bug class). Both
  copies hard-code the intake host, so a repointed `VITE_BUG_INTAKE_URL` to a
  self-hosted host is CSP-blocked until that host is added here too —
  parameterizing the CSP from the build arg is a deferred #1485 follow-up (the
  **disable** switch works end-to-end without it).
- Build info (`app_version`, `git_commit`) sourced via the existing
  `useBuildInfo()` composable (`GET /api/version`, Invariant #7).

## Diagnostics captured (scrubbed before the user reviews)

App version + git commit, current route/URL, user agent, viewport, OS, and the
last N console errors/warnings. Optional screenshot is **deferred** to a
follow-up (image hosting + visual-leak surface).

## Intake service (`trinity-ops-agent/bug-intake/`, out-of-repo)

Cloudflare Worker. `GITHUB_TOKEN` is a Worker secret (least-privilege
fine-grained PAT, `Issues: write` on `abilityai/trinity` only) — never client-exposed.

Defense-in-depth: Cloudflare edge DDoS (native) + a per-IP Rate Limiting Rule;
Worker method/`Content-Type`/256 KB body guards; KV soft per-`install_id` daily
cap; content-hash dedupe (a flapping client gets the existing issue URL back, no
duplicates); **second server-side scrub** (never trusts the client); optional
Turnstile.

**Report kind:** `type` selects routing — `bug` → `type-bug`, `feature` →
`type-feature` (both `+ source:in-app`, public issue); `feedback` → **no GitHub
issue at all**, delivered only as the private team email. Dedupe is keyed on
`(type, scrubbed content)` (feedback stores a sentinel, not a URL).

**Optional contact email:** rides only the private team notification (as its
`reply-to`) and is **never** written to the public issue — the body just notes
"a contact email was provided." It is exempt from the email-masking scrub since
the submitter intends to share it.

**Team notification (Resend):** on each NEW issue (not dedupe hits) the Worker
fires a best-effort email via Resend (`ctx.waitUntil`, non-blocking; a failure
never fails the report) to `NOTIFY_EMAIL_TO` from the Resend-verified
`NOTIFY_EMAIL_FROM` (`noreply@abilityai.dev`) — type, title, scrubbed
description, diagnostics, issue link, `reply-to` = reporter email when given.
`RESEND_API_KEY` is a Worker secret; absent → email silently skipped.

21 unit tests (scrubber + email/issue-body builders); verified live end-to-end
against `abilityai/trinity` (bug + feature filing, correct labels, feedback
files NO issue, secret scrubbing, submitter email kept out of the public body,
dedupe, 400 guards, Resend delivery).

## Security notes (public repo)

Reports land in a PUBLIC, indexed repo. Tokens (`Bearer`, `trinity_mcp_*`,
`sk-*`, `gh*_`, `xox*`, `AIza*`, `AKIA*`, JWTs), URL credentials/signed tokens,
`key=value` secrets, emails, and RFC-1918 IPs are scrubbed **on both the client
and the server**. The screenshot path is deferred precisely because an image
can leak secrets visually that regex scrubbing can't catch.
