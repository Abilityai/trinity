import functions_framework
from google.cloud import discoveryengine_v1 as discoveryengine
import json
import time
import urllib.request

from links import parse_catalogue, sanitize_links

PROJECT_ID = "mcp-server-project-455215"
ENGINE_ID = "trinity-search"
LOCATION = "global"

SYSTEM_PROMPT = """You are Trinity's documentation assistant.

Style guidelines:
- **Use markdown** — headers, bullets, code blocks where helpful
- **Be concise** — get to the point, no fluff
- **Plain language** — avoid jargon, explain simply
- **Practical** — focus on what the user needs to do

If you don't know something based on the docs, say so.

Links (people read your answer in a help widget, on the docs site or in an MCP
client — none of them can open a file from the source repository):
- When a relevant documentation page exists, end the answer with a line
  **Read more:** followed by a markdown link to that page on
  https://docs.ability.ai — at most two pages.
- When a relevant video exists, add a line **Watch:** followed by a markdown
  link to that YouTube video, titled with the video's name — at most two videos.
- Use ONLY URLs that appear verbatim in the documents you were given (the docs
  site catalogue lists every page and video URL with a description). Never
  invent, guess or rewrite a URL. If no source gives a URL for it, leave the
  line out.
- Never link to a relative path or a `.md` file (for example
  `../automation/scheduling.md` or `videos.md`), and never cite a source file
  name as if it were a link."""

FALLBACK_ANSWER = "I wasn't able to find a specific answer to that question. Try rephrasing, or check the [Trinity docs](https://docs.ability.ai) directly."

CATALOGUE_URL = "https://docs.ability.ai/llms.txt"
CATALOGUE_TTL_SECONDS = 3600
_catalogue = {"urls": set(), "fetched_at": 0.0}


def known_urls():
    """Every page/video URL the docs site serves, from its llms.txt (ent#1285).

    Cached for an hour per instance. A failed refresh keeps the last good set;
    with no set at all `sanitize_links` only drops relative `.md` links.
    """
    if time.time() - _catalogue["fetched_at"] < CATALOGUE_TTL_SECONDS and _catalogue["urls"]:
        return _catalogue["urls"]
    try:
        req = urllib.request.Request(CATALOGUE_URL, headers={"User-Agent": "ask-trinity"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            urls = parse_catalogue(resp.read().decode("utf-8", "replace"))
        if len(urls) >= 50:  # a truncated or error page must not shrink the allowlist
            _catalogue["urls"] = urls
    except Exception:
        pass
    _catalogue["fetched_at"] = time.time()
    return _catalogue["urls"]


def get_or_create_session(client, session_id=None):
    """Get existing session or create a new one."""
    parent = f"projects/{PROJECT_ID}/locations/{LOCATION}/collections/default_collection/engines/{ENGINE_ID}"

    if session_id:
        try:
            session_name = f"{parent}/sessions/{session_id}"
            session = client.get_session(name=session_name)
            return session.name, session_id
        except Exception:
            pass

    session = client.create_session(
        parent=parent,
        session=discoveryengine.Session()
    )
    new_session_id = session.name.split("/")[-1]
    return session.name, new_session_id

@functions_framework.http
def ask_trinity(request):
    if request.method == "OPTIONS":
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "3600",
        }
        return ("", 204, headers)

    headers = {
        "Access-Control-Allow-Origin": "*",
        "Content-Type": "application/json"
    }

    try:
        data = request.get_json(silent=True) or {}
        question = data.get("question") or request.args.get("q")
        session_id = data.get("session_id")

        if not question:
            return (json.dumps({"error": "Missing 'question' parameter"}), 400, headers)

        client = discoveryengine.ConversationalSearchServiceClient()
        session_name, session_id = get_or_create_session(client, session_id)

        serving_config = f"projects/{PROJECT_ID}/locations/{LOCATION}/collections/default_collection/engines/{ENGINE_ID}/servingConfigs/default_search"

        response = client.answer_query(
            request=discoveryengine.AnswerQueryRequest(
                serving_config=serving_config,
                query=discoveryengine.Query(text=question),
                session=session_name,
                answer_generation_spec=discoveryengine.AnswerQueryRequest.AnswerGenerationSpec(
                    model_spec=discoveryengine.AnswerQueryRequest.AnswerGenerationSpec.ModelSpec(
                        model_version="gemini-2.0-flash-001/answer_gen/v1"
                    ),
                    prompt_spec=discoveryengine.AnswerQueryRequest.AnswerGenerationSpec.PromptSpec(
                        preamble=SYSTEM_PROMPT
                    ),
                    include_citations=True,
                )
            )
        )

        answer_text = response.answer.answer_text if response.answer else ""
        # The model invents docs URLs from repo paths; keep only links the
        # docs site actually serves (repaired where unambiguous).
        answer_text = sanitize_links(answer_text, known_urls())
        state = str(response.answer.state.name) if response.answer else "UNKNOWN"

        return (json.dumps({
            "answer": answer_text or FALLBACK_ANSWER,
            "state": state,
            "session_id": session_id
        }), 200, headers)

    except Exception as e:
        return (json.dumps({"error": str(e)}), 500, headers)
