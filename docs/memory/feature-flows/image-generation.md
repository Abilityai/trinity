# Feature: Platform Image Generation (IMG-001)

## Overview
Platform-level image generation service using Google Gemini models with a two-step pipeline: prompt refinement via Gemini 2.0 Flash text model, then image generation via the `GEMINI_IMAGE_MODEL` model (default `gemini-nano-banana-2.1`, #4717). Supports use-case-specific best practices (general, thumbnail, diagram, social, avatar), configurable aspect ratios, and image-to-image variation generation from a reference image.

## User Story
As a platform user (or internal service), I want to generate high-quality images from text descriptions so that agents and platform features can produce visual content without managing their own image generation infrastructure.

## Entry Points
- **API**: `POST /api/images/generate` -- Generate an image from a text prompt (JWT required)
- **API**: `GET /api/images/models` -- List available models and configuration options (JWT required)
- **Internal**: `generate_variation()` -- Generate a variation from a reference image (called by avatar service)
- **Internal**: `generate_emotion_variation()` -- Generate an emotion variant from a reference image (called by avatar emotion service, AVATAR-002)
- **No frontend UI** -- Backend-only platform capability (no Vue components yet)
- **No MCP tools** -- Not yet exposed via MCP server

---

## Backend Layer

### Router
**File**: `src/backend/routers/image_generation.py`

```python
router = APIRouter(prefix="/api/images", tags=["images"])
```

#### POST /api/images/generate (line 35)
- **Handler**: `generate_image(request, current_user)`
- **Auth**: `Depends(get_current_user)` -- JWT or MCP API key required
- **Request body** (`ImageGenerateRequest`, line 28):
  ```python
  class ImageGenerateRequest(BaseModel):
      prompt: str                          # Required -- raw text description
      use_case: Optional[str] = "general"  # "general", "thumbnail", "diagram", "social"
      aspect_ratio: Optional[str] = "1:1"  # "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"
      refine_prompt: Optional[bool] = True # Whether to refine prompt via text model first
  ```
- **Flow**:
  1. Get singleton `ImageGenerationService` via `get_image_generation_service()`
  2. Check `service.available` -- returns 501 if `GEMINI_API_KEY` not configured
  3. Call `service.generate_image(...)` with `agent_name=f"user:{current_user.username}"`
  4. If `result.success` is False, return 422 with error details
  5. If success, return JSON with base64-encoded image data
- **Success response** (200):
  ```json
  {
      "image_base64": "<base64 string>",
      "mime_type": "image/png",
      "refined_prompt": "the refined prompt used for generation",
      "original_prompt": "the user's raw prompt",
      "model_used": "gemini-nano-banana-2.1",
      "use_case": "general",
      "aspect_ratio": "1:1"
  }
  ```

#### GET /api/images/models (line 85)
- **Handler**: `get_models(current_user)`
- **Auth**: `Depends(get_current_user)`
- **Response** (200):
  ```json
  {
      "available": true,
      "models": {
          "text_refinement": "gemini-3.5-flash",
          "image_generation": "gemini-nano-banana-2.1"
      },
      "default_model": "gemini-nano-banana-2.1",
      "use_cases": ["general", "thumbnail", "diagram", "social", "avatar"],
      "aspect_ratios": ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"]
  }
  ```

### Router Registration
**File**: `src/backend/main.py`
- **Line 68**: `from routers.image_generation import router as image_generation_router`
- **Line 334**: `app.include_router(image_generation_router)  # Image Generation (IMG-001)`

---

### Service Layer
**File**: `src/backend/services/image_generation_service.py`

#### ImageGenerationService (line 52)
Singleton service accessed via `get_image_generation_service()` (line 395).

**Constants** (lines 29-35):
| Constant | Value |
|----------|-------|
| `GEMINI_TEXT_MODEL` | `gemini-3.5-flash` (env-overridable, defined in `config.py` — #1130) |
| `GEMINI_IMAGE_MODEL` | `gemini-nano-banana-2.1` (env-overridable, defined in `config.py`) |
| `GEMINI_API_BASE` | `https://generativelanguage.googleapis.com/v1beta/models` |
| `PROMPT_REFINEMENT_TIMEOUT` | 30 seconds |
| `IMAGE_GENERATION_TIMEOUT` | 120 seconds |

**HTTP Client** (line 58): Lazy-initialized `httpx.AsyncClient` with 120s timeout, 10s connect timeout.

#### generate_image() (line 71)
Main entry point. Parameters: `prompt`, `use_case`, `aspect_ratio`, `refine_prompt`, `agent_name`.

**Two-step pipeline**:

1. **Step 1 -- Prompt Refinement** (line 122, optional):
   - Calls `refine_prompt()` which calls `_call_gemini_text()`
   - Uses `system_instruction` with use-case-specific best practices
   - Sends raw prompt + aspect ratio as user message
   - On failure: logs warning and falls back to raw prompt (does not abort)

2. **Step 2 -- Image Generation** (line 131):
   - Calls `_call_gemini_image()` with refined prompt and aspect ratio
   - Uses `responseModalities: ["image", "text"]` and `imageConfig.aspectRatio`
   - Extracts `inlineData` from response candidates, base64-decodes image bytes
   - Returns `ImageGenerationResult` dataclass with image bytes, mime type, and metadata

#### _call_gemini_text() (line 183)
- **URL**: `{GEMINI_API_BASE}/{GEMINI_TEXT_MODEL}:generateContent`
- **Auth**: `x-goog-api-key` header with `GEMINI_API_KEY`
- **Config**: temperature 0.7, maxOutputTokens 512
- **Response parsing**: `data["candidates"][0]["content"]["parts"][0]["text"]`

#### _call_gemini_image() (line 232)
- **URL**: `{GEMINI_API_BASE}/gemini-nano-banana-2.1:generateContent`
- **Auth**: `x-goog-api-key` header with `GEMINI_API_KEY`
- **Config**: `responseModalities: ["image", "text"]`, `imageConfig.aspectRatio`
- **Parameters**:
  - `prompt: str` -- The refined text prompt
  - `aspect_ratio: str` -- Target aspect ratio
  - `reference_image: Optional[bytes] = None` -- Optional reference image for style/likeness guidance
  - `reference_mime_type: str = "image/png"` -- MIME type of the reference image
- **Reference image support** (lines 255-263): When `reference_image` is provided, it is base64-encoded and prepended as an `inlineData` part before the text prompt in the request payload. This enables image-to-image generation where Gemini uses the reference for style and likeness guidance.
  ```python
  parts = []
  if reference_image:
      parts.append({
          "inlineData": {
              "mimeType": reference_mime_type,
              "data": base64.b64encode(reference_image).decode("utf-8"),
          }
      })
  parts.append({"text": prompt})
  ```
- **Response parsing**: Iterates candidates/parts looking for `inlineData`, base64-decodes

#### generate_variation() (line 313)
Generates an image variation from a reference image. Located after `generate_image()` and before `close()`.

- **Parameters**:
  - `prompt: str` -- Text prompt (already refined, passed through from original generation)
  - `reference_image: bytes` -- Reference image bytes (required, not optional)
  - `reference_mime_type: str = "image/png"` -- MIME type of the reference
  - `aspect_ratio: str = "1:1"` -- Target aspect ratio
  - `agent_name: Optional[str] = None` -- For logging
- **Flow**:
  1. Check `self.available` -- return error result if no API key
  2. Wrap the prompt with variation instructions:
     ```python
     variation_prompt = (
         f"Generate a new variation of this portrait. Keep the same subject identity, "
         f"features, and overall style but create a fresh natural variation -- slightly "
         f"different expression, micro-changes in lighting angle, or subtle pose shift. "
         f"The result should look like a different photo from the same session.\n\n"
         f"Original prompt: {prompt}"
     )
     ```
  3. Call `_call_gemini_image()` with `variation_prompt`, `aspect_ratio`, `reference_image`, and `reference_mime_type`
  4. Return `ImageGenerationResult` with `use_case="avatar"`
- **Error handling**: Catches all exceptions, returns failure result with error message
- **Primary consumer**: The avatar generation service (AVATAR-001), which calls this to produce multiple avatar candidates from a single initial generation

#### generate_emotion_variation() (AVATAR-002)
Generates an emotion variant of an avatar using a caller-supplied emotion prompt and reference image. Unlike `generate_variation()`, this uses the caller's full prompt instead of wrapping with generic variation instructions.

- **Parameters**:
  - `emotion_prompt: str` -- Full prompt describing the desired emotion/expression (caller-built)
  - `reference_image: bytes` -- Reference image bytes for identity preservation
  - `reference_mime_type: str = "image/png"` -- MIME type of the reference
  - `aspect_ratio: str = "1:1"` -- Target aspect ratio
  - `agent_name: Optional[str] = None` -- For logging
- **Flow**: Calls `_call_gemini_image()` directly with the emotion prompt and reference image (no prompt refinement step)
- **Returns**: `ImageGenerationResult` with `use_case="avatar"`
- **Primary consumer**: Background emotion generation in `avatar.py` (`_generate_emotions_background()`), which generates 8 emotion variants per agent avatar

#### ImageGenerationResult (line 39)
```python
@dataclass
class ImageGenerationResult:
    success: bool
    image_data: Optional[bytes] = None
    mime_type: str = "image/png"
    refined_prompt: Optional[str] = None
    original_prompt: Optional[str] = None
    model_used: str = GEMINI_IMAGE_MODEL
    use_case: str = "general"
    aspect_ratio: str = "1:1"
    error: Optional[str] = None
```

---

### Prompt Engineering Layer
**File**: `src/backend/services/image_generation_prompts.py`

Four use-case-specific system prompts, each containing best practices for image generation:

| Use Case | Constant | Key Rules |
|----------|----------|-----------|
| `general` | `GENERAL_BEST_PRACTICES` (line 8) | Be specific, specify style/medium, include lighting/mood, composition, color palette, avoid negatives, no text in images |
| `thumbnail` | `THUMBNAIL_BEST_PRACTICES` (line 52) | Bold high-contrast, simple compositions, vibrant colors, clean backgrounds, leave space for overlay text |
| `diagram` | `DIAGRAM_BEST_PRACTICES` (line 89) | Clean minimal style, clear hierarchy, limited palette (3-5 colors), flat vector style, no text |
| `social` | `SOCIAL_BEST_PRACTICES` (line 127) | Scroll-stopping visuals, emotional resonance, brand-safe, platform-aware aspect ratios |
| `avatar` | `AVATAR_BEST_PRACTICES` (line 166) | Dark mode studio portrait style (see below) |

#### Avatar Prompt Style (AVATAR_BEST_PRACTICES, lines 166-226)
The avatar prompt enforces extreme consistency through a fixed technical specification appended to every refined prompt. Key style parameters:

- **Framing**: Extreme close-up portrait, face filling 85-90% of frame, subject centered, front-facing with eyes at camera
- **Background**: Smooth dark gradient from deep slate-navy (#1a1f2e) to dark charcoal (#111827), no objects or patterns
- **Lighting**: Single soft key light from upper-left at 45 degrees, cool 5600K color temperature, subtle cool indigo-blue rim light (#6366f1) on the right edge for subject separation
- **Color grading**: Modern digital, cool desaturated palette, deep rich shadows, clean highlight rolloff, neutral-to-cool midtones, no film grain, sharp and clean
- **Lens**: 85mm f/1.4 prime, shallow depth of field on background only
- **Style**: Modern studio portrait photograph, photographic realism (not illustration, not CGI)
- **Subject rules**: Preserve all user details exactly, add only missing defaults (calm confident expression), literal descriptions not poetic, consistent front-facing pose, circular crop safe

**Exports**:
```python
USE_CASE_PROMPTS = {"general": ..., "thumbnail": ..., "diagram": ..., "social": ..., "avatar": ...}
VALID_USE_CASES = ["general", "thumbnail", "diagram", "social", "avatar"]
VALID_ASPECT_RATIOS = ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"]
```

All prompts instruct the model to return ONLY the refined prompt text with no explanations, markdown, or quotes.

#### Emotion Variant Constants (AVATAR-002)

```python
AVATAR_EMOTIONS = ["happy", "thoughtful", "surprised", "determined", "calm", "amused", "curious", "confident"]
AVATAR_EMOTION_PROMPTS = {emotion: "facial expression description", ...}  # 8 entries
```

Used by `avatar.py:_generate_emotions_background()` to build emotion-specific prompts for `generate_emotion_variation()`.

---

### Configuration
**File**: `src/backend/config.py`
- `GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")`
- `GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL") or "gemini-3.5-flash"` -- prompt-refinement model, env-overridable (#1130; `or`-coalesce per #1076 so an empty var can't shadow the default)
- `GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL") or "gemini-nano-banana-2.1"` -- image-generation model, env-overridable (#4717; same coalesce)

**Files**: `docker-compose.yml` + `docker-compose.prod.yml`
- `GEMINI_API_KEY=${GEMINI_API_KEY:-}` -- Passed through from host `.env` to backend container
- `GEMINI_TEXT_MODEL=${GEMINI_TEXT_MODEL:-gemini-3.5-flash}` -- non-empty default (#1130)
- `GEMINI_IMAGE_MODEL=${GEMINI_IMAGE_MODEL:-gemini-nano-banana-2.1}` -- non-empty default (#4717)

---

## Data Layer

### Database Operations
None. This feature has no database storage. Images are returned inline as base64 and not persisted.

### Redis Operations
None.

---

## Side Effects

- **No WebSocket broadcasts** -- Synchronous request/response only
- **No activity tracking** -- Not integrated with `activity_service`
- **Logging**: Structured logging via `logger.info()` / `logger.warning()` / `logger.error()` with `[IMG {agent_name}]` prefix, captured by Vector

---

## Error Handling

| Error Case | HTTP Status | Response |
|------------|-------------|----------|
| `GEMINI_API_KEY` not configured | 501 | `{"detail": "Image generation not available: GEMINI_API_KEY not configured"}` |
| Invalid `use_case` value | 422 | `{"detail": "Invalid use_case: X. Must be one of: [...]", ...}` |
| Invalid `aspect_ratio` value | 422 | `{"detail": "Invalid aspect_ratio: X. Must be one of: [...]", ...}` |
| Gemini text API error (refinement) | -- | Falls back to raw prompt silently (logged as warning) |
| Gemini image API error (non-200) | 422 | `{"detail": "Gemini image API error {status}: {body}"}` |
| No image in Gemini response (safety filter) | 422 | `{"detail": "Gemini image API returned no image data. The prompt may have been blocked by safety filters."}` |
| Unexpected response structure | 422 | `{"detail": "Unexpected Gemini text response structure: {error}"}` |
| No JWT token | 401 | `{"detail": "Could not validate credentials"}` |

---

## Testing

### Prerequisites
- Backend running (`docker-compose up backend`)
- `GEMINI_API_KEY` set in `.env` with a valid Google API key
- Authenticated user (JWT token)

### Test Steps

1. **Check service availability**
   **Action**: `GET /api/images/models`
   **Expected**: Returns JSON with `available: true` and model/use_case/aspect_ratio lists
   **Verify**: `curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/images/models`

2. **Generate image with defaults**
   **Action**: `POST /api/images/generate` with `{"prompt": "a red apple on a wooden table"}`
   **Expected**: 200 with `image_base64`, `mime_type`, `refined_prompt` (different from original)
   **Verify**: Decode base64 to verify valid image bytes

3. **Generate with specific use case**
   **Action**: `POST /api/images/generate` with `{"prompt": "agent dashboard", "use_case": "thumbnail", "aspect_ratio": "16:9"}`
   **Expected**: 200 with thumbnail-optimized image in 16:9 aspect ratio

4. **Skip prompt refinement**
   **Action**: `POST /api/images/generate` with `{"prompt": "a blue cube", "refine_prompt": false}`
   **Expected**: 200 with `refined_prompt` equal to `original_prompt` (no refinement)

5. **Invalid use case**
   **Action**: `POST /api/images/generate` with `{"prompt": "test", "use_case": "invalid"}`
   **Expected**: 422 with error about invalid use_case

6. **Service unavailable (no API key)**
   **Action**: Remove `GEMINI_API_KEY` from environment, restart backend, call generate
   **Expected**: 501 with "not available" message

7. **Generate variation (internal API)**
   **Action**: Call `service.generate_variation(prompt, reference_image_bytes)` from code
   **Expected**: Returns `ImageGenerationResult` with `use_case="avatar"` and new image bytes
   **Verify**: Result image differs from reference but maintains subject identity

### curl Examples

```bash
# Check models
curl -s http://localhost:8000/api/images/models \
  -H "Authorization: Bearer $TOKEN" | jq

# Generate image
curl -s -X POST http://localhost:8000/api/images/generate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "a futuristic city skyline at sunset", "use_case": "social", "aspect_ratio": "16:9"}' \
  | jq -r '.image_base64' | base64 -d > output.png
```

---

## Architecture Notes

- **Singleton pattern**: `get_image_generation_service()` returns a module-level singleton, lazily initialized
- **Graceful degradation**: If prompt refinement fails, the raw prompt is used directly (no abort)
- **No frontend coupling**: Pure backend capability designed for internal service consumption
- **Extensible**: `agent_name` parameter enables per-agent tracking; designed for future MCP tool and agent-level integration
- **No persistence**: Images are ephemeral (returned inline, not stored). Future iterations may add storage
- **Image-to-image**: `_call_gemini_image()` supports optional `reference_image` parameter for variation generation, used by the avatar service (AVATAR-001) and emotion variant generation (AVATAR-002)

---

## Related Flows
- [agent-avatars.md](agent-avatars.md) -- AI-generated agent avatars, primary consumer of `generate_variation()`
- [nevermined-payments.md](nevermined-payments.md) -- Paid agent API could use image generation
- [authenticated-chat-tab.md](authenticated-chat-tab.md) -- Chat could eventually render generated images
