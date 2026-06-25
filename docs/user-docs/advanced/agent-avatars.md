# Agent Avatars

AI-generated avatars for agents using reference images, emotion variants, and default generation.

> 📺 **Watch:** [Trinity Platform Demo — avatar generation](https://youtu.be/ivljtZqsxeo) *(May 2026)* · [all videos](../videos.md)

## Features

- **Reference Image** -- Upload a reference image and the avatar is generated in that style.
- **Variation Regeneration** -- Generate new variations from an existing avatar.
- **Emotion Variants** -- The Agent Detail page cycles through emotion-based avatar variants every 30 seconds.
- **Default Avatar Generation** -- Admin button in Settings generates robot/android-style avatars for all agents without a custom avatar.
- **WebP Conversion** -- Avatars are converted to WebP via Pillow for optimization.
- **Stable Emotion Cache Keys** -- Emotion variants use stable cache keys to avoid redundant generation.
- **Dark Mode Compatible** -- Avatar styling adapts to dark mode.
- **Dashboard Timeline** -- Avatars display in Dashboard Timeline tiles at large size with a border ring.

## Generation Failures

When avatar generation fails, the **Generate** dialog shows an actionable reason instead of a generic "Failed to generate avatar." Each failure is classified so you know whether to fix configuration, change the prompt, or just retry:

| Reason | Meaning | What to do |
|--------|---------|------------|
| `not_configured` | No image-generation API key is set | Add `GEMINI_API_KEY` in **Settings → AI Keys** |
| `invalid_input` | The reference image or prompt was rejected | Adjust the prompt or upload a different reference image |
| `safety_filter` | The upstream model blocked the request on safety grounds | Reword the identity prompt |
| `rate_limited` | The image provider is throttling requests | Wait and retry |
| `timeout` | The request timed out (e.g. a gateway 504) | Retry; if persistent, check provider status |
| `upstream_error` / `unknown` | An unexpected provider or network error | Retry; check the platform logs if it recurs |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/avatar` | GET | Serve the agent's current avatar |
| `/api/agents/{name}/avatar/generate` | POST | Generate a new avatar (optionally from a reference image / identity prompt) |
| `/api/agents/{name}/avatar/regenerate` | POST | Generate a fresh variation from the existing avatar |
| `/api/agents/{name}/avatar` | DELETE | Remove the agent's custom avatar |
| `/api/agents/avatars/generate-defaults` | POST | Admin — generate default avatars for all agents without one |

## See Also

- [Managing Agents](../agents/managing-agents.md)
- [Dashboard](../operations/dashboard.md)
