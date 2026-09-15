# Image Generation

Platform image generation via a two-step Gemini pipeline: prompt refinement then image generation.

> 📺 **Watch:** [I Let AI Agents Build My Entire Webinar](https://youtu.be/WBVbg_bwc_g) *(Dec 2025)* · [all videos](../videos.md)

## How It Works

1. Submit an image generation request via API with a `prompt`, an optional `use_case` (`general`, `thumbnail`, `diagram`, `social`, `avatar`; default `general`) and an optional `aspect_ratio` (`1:1`, `16:9`, `9:16`, `4:3`, `3:4`, `3:2`, `2:3`; default `1:1`).
2. **Step 1 -- Prompt Refinement**: Gemini refines the prompt using best-practice templates for the use case. Send `refine_prompt: false` to skip this step and use your prompt as written.
3. **Step 2 -- Image Generation**: Gemini generates the image from the refined prompt.
4. The image is returned as base64 (`image_base64`, `mime_type`) together with the `refined_prompt`, the `original_prompt` and the `model_used`.

Generation needs a Gemini key — saved in **Settings → Integrations** ([Platform Keys](../credentials/platform-keys.md#gemini)) or set as `GEMINI_API_KEY`. Without one the endpoint answers **501**; a generation the provider rejects answers **422** with the reason and the refined prompt that was tried.

Used internally for [agent avatars](agent-avatars.md) and other platform features.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/images/generate` | POST | Generate an image from a text prompt (`prompt`, `use_case?`, `aspect_ratio?`, `refine_prompt?`) |
| `/api/images/models` | GET | Whether generation is available, the text-refinement and image models in use, and the valid use cases and aspect ratios |

## See Also

- [Agent Avatars](agent-avatars.md)
- Backend API Docs: http://localhost:8000/docs
