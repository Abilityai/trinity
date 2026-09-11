# Platform Keys: Claude, GitHub, Email and Gemini

The keys a Trinity instance needs, all configurable from the browser — in the first-run setup, or later under **Settings → Integrations**. No terminal and no `.env` edit is needed for any of them.

| Key | Required? | What it switches on | If you skip it |
|-----|-----------|---------------------|----------------|
| [Claude](#claude) | **Yes** | Every agent's thinking | No agent can run |
| [GitHub access token](#github-access-token) | No | Private repos, pushing agent work to GitHub | Public templates only; no push |
| [Email provider (Resend)](#email-provider-resend) | No | Email-code sign-in for you and anyone you share with | Only the admin password works; codes go to the server log |
| [Gemini](#gemini) | No | Voice conversations, generated agent avatars | No voice; agents show initials instead of pictures |

Every key is checked with its provider **before** it is saved, and stored encrypted (AES-256-GCM) in the platform database — never in plain text. A key saved in Settings takes precedence over the same key in the server's `.env`, and takes effect without a restart.

## Claude

The one credential Trinity cannot run without. The first-run setup will not finish until one is connected (you can choose **Finish later**, but your agents stay idle until you add one).

Two ways to connect — pick one tab:

- **Subscription token** — uses your Claude Pro or Max plan. On any computer where Claude Code is signed in to that plan, run `claude setup-token` and paste the token it prints (it starts with `sk-ant-oat01-`). See [Anthropic's guide](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token).
- **API key** — pay-as-you-go billing. Create a key in your browser at [console.anthropic.com](https://console.anthropic.com/settings/keys) (it starts with `sk-ant-api`). No terminal needed.

Pasting a subscription token into the API key tab (or the other way round) is caught before anything is sent, and the step offers to move it to the right tab.

**What happens when you connect the first credential.** Agents that were created before any Claude credential existed — for example the starter fleet a fresh install comes with — are switched onto it automatically. Running ones restart in the background, which takes about a minute. Agents that were deliberately set up with their own key are left alone. New agents pick up a credential when they are created.

Manage it later: **Settings → Integrations** — the API key field, and the Subscriptions panel for tokens (auto-assign, auto-switch, usage). See [Subscription Credentials](subscription-credentials.md).

## GitHub access token

Lets agents clone private repositories and push their work back to GitHub. Create a token at [github.com/settings/tokens](https://github.com/settings/tokens/new?scopes=repo&description=Trinity) with the `repo` scope (classic), or a fine-grained token with **Contents** read/write on the repositories your agents use. It starts with `ghp_` or `github_pat_`.

Saving it also updates running agents that use the platform token. Details: [GitHub PAT Setup](../integrations/github-pat-setup.md).

## Email provider (Resend)

Trinity sends a one-time code to sign in by email — for you, and for anyone you share an agent with. It sends those codes through [Resend](https://resend.com).

1. Create an API key at [resend.com/api-keys](https://resend.com/api-keys) (it starts with `re_`).
2. Verify the domain you want to send from at [resend.com/domains](https://resend.com/domains).
3. Paste the key and a **Send codes from** address on that domain (for example `noreply@your-domain.com`).

The check reads your Resend account's domains and refuses an address whose domain Resend has not verified — Resend would reject every code sent from it. A sending-only key cannot list domains; it is accepted with a warning to confirm the domain yourself.

A key saved here makes Resend the email provider, even if the server's `.env` says otherwise. **Remove** reverts email to the `.env` configuration.

If you skip it, nobody can sign in with an email code — only the admin password works, and the codes are written to the server log instead of being sent.

## Gemini

Powers voice conversations with agents and Telegram voice-note transcription, and generates agent avatars. (Phone calls and the Brain Orb voice tile also use it, but each has its own feature switch as well.) Create a key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey) (it starts with `AIza`).

If you skip it, voice is off and agents get no generated avatars — the starter fleet shows initials instead of pictures. Add the key later and voice switches on without a restart; generate avatars from each agent's avatar menu. See [Voice Chat](../advanced/voice-chat.md) and [Agent Avatars](../advanced/agent-avatars.md).

## For admins and automation

All endpoints are admin-only and never return a key — only whether one is set, where it comes from (`settings` or `env`) and its last four characters.

| Endpoint | Purpose |
|----------|---------|
| `GET /api/settings/api-keys` | Status of `anthropic`, `github`, `resend` (with `provider`, `from_address`), `gemini` |
| `POST /api/settings/api-keys/{anthropic,github,resend,gemini}/test` | Check a key with its provider; nothing is saved |
| `PUT /api/settings/api-keys/{anthropic,github,resend,gemini}` | Save a key (Resend also takes `from_address`) |
| `DELETE /api/settings/api-keys/{anthropic,github,resend,gemini}` | Remove the saved key; the `.env` value, if any, applies again |
| `POST /api/subscriptions/test` | Check a subscription token (one tiny request on your plan); nothing is saved |
| `POST /api/subscriptions` | Register a subscription token |

Environment fallbacks, used only when nothing is saved in Settings: `ANTHROPIC_API_KEY`, `GITHUB_PAT`, `RESEND_API_KEY` with `EMAIL_PROVIDER` and `SMTP_FROM`, and `GEMINI_API_KEY` (or `GOOGLE_API_KEY`).
