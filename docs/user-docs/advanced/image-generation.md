# Image Generation

Platform image generation via a two-step Gemini pipeline: prompt refinement then image generation.

> 📺 **Watch:** [I Let AI Agents Build My Entire Webinar](https://youtu.be/WBVbg_bwc_g) *(Dec 2025)* · [all videos](../videos.md)

## How It Works

1. Submit an image generation request via API.
2. **Step 1 -- Prompt Refinement**: Gemini refines the user's prompt using best-practice templates for the use case.
3. **Step 2 -- Image Generation**: Gemini generates the image from the refined prompt.
4. The image is returned as base64 or URL.

Used internally for agent avatars and other platform features.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/image/generate` | POST | Generate an image from a text prompt |

## See Also

- Backend API Docs: http://localhost:8000/docs
