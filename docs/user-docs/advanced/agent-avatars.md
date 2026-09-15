# Agent Avatars

AI-generated avatars for agents using reference images, emotion variants, and default generation.

> 📺 **Watch:** [Trinity Platform Demo — avatar generation](https://youtu.be/ivljtZqsxeo) *(May 2026)* · [all videos](../videos.md)

## Features

- **Reference Image** -- Upload a reference image and the avatar is generated in that style.
- **Variation Regeneration** -- Generate new variations from an existing avatar.
- **Emotion Variants** -- The Agent Detail page cycles through emotion-based avatar variants every 30 seconds.
- **Default Avatar Generation** -- The **Generate Default Avatars** button in Settings (admin) generates robot/android-style avatars for all agents without a custom avatar.
- **Bundled Default Avatars** -- The first-run templates (`scout`, `sage`, `scribe`) ship an `avatar.webp` beside their `template.yaml`, installed at creation as the agent's default avatar — so a fresh install shows faces rather than initials before any image-generation key exists. Any `local:` template that declares an `avatar_prompt` can bundle an `avatar.webp` or `avatar.png` (up to 2 MB) the same way. It counts as a default avatar, so **Generate Default Avatars** replaces it once a key is configured; a missing or unreadable image falls back to the prompt-only seed. An agent seeded from a GitHub template (such as the bundled Cornelius) still shows initials until a key exists.
- **WebP Conversion** -- Avatars are converted to WebP via Pillow for optimization.
- **Stable Emotion Cache Keys** -- Emotion variants use stable cache keys to avoid redundant generation.
- **Dark Mode Compatible** -- Avatar styling adapts to dark mode.
- **Dashboard Timeline** -- Avatars display in Dashboard Timeline tiles at large size with a border ring.

## Generation Failures

When avatar generation fails, the **Generate** dialog shows an actionable reason instead of a generic "Failed to generate avatar." Each failure is classified so you know whether to fix configuration, change the prompt, or just retry:

| Reason | Meaning | What to do |
|--------|---------|------------|
| `not_configured` | No image-generation API key is set | Add a Gemini key in **Settings → Integrations** ([Platform Keys](../credentials/platform-keys.md#gemini)) |
| `invalid_input` | The reference image or prompt was rejected | Adjust the prompt or upload a different reference image |
| `safety_filter` | The upstream model blocked the request on safety grounds | Reword the identity prompt |
| `rate_limited` | The image provider is throttling requests | Wait and retry |
| `timeout` | The request timed out (e.g. a gateway 504) | Retry; if persistent, check provider status |
| `upstream_error` / `unknown` | An unexpected provider or network error | Retry; check the platform logs if it recurs |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/avatar` | GET | Serve the agent's current avatar (no auth — avatars are public assets) |
| `/api/agents/{name}/avatar/reference` | GET | Serve the reference image the avatar was generated from (no auth) |
| `/api/agents/{name}/avatar/identity` | GET | The agent's identity prompt |
| `/api/agents/{name}/avatar/emotions` | GET | Which emotion variants exist, and their version |
| `/api/agents/{name}/avatar/emotion/{emotion}` | GET | Serve one emotion variant (no auth) |
| `/api/agents/{name}/avatar/generate` | POST | Generate a new avatar (optionally from a reference image / identity prompt) |
| `/api/agents/{name}/avatar/regenerate` | POST | Generate a fresh variation from the existing avatar |
| `/api/agents/{name}/avatar` | DELETE | Remove the agent's custom avatar |
| `/api/agents/avatars/generate-defaults` | POST | Admin — generate default avatars for all agents without one |

## See Also

- [Managing Agents](../agents/managing-agents.md)
- [Dashboard](../operations/dashboard.md)
