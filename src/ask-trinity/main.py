import functions_framework
from google.cloud import discoveryengine_v1 as discoveryengine
import json

PROJECT_ID = "mcp-server-project-455215"
ENGINE_ID = "trinity-search"
LOCATION = "global"

SYSTEM_PROMPT = """You are Trinity's documentation assistant.

Style guidelines:
- **Use markdown** — headers, bullets, code blocks where helpful
- **Be concise** — get to the point, no fluff
- **Plain language** — avoid jargon, explain simply
- **Practical** — focus on what the user needs to do

If you don't know something based on the docs, say so."""

FALLBACK_ANSWER = "I wasn't able to find a specific answer to that question. Try rephrasing, or check the [Trinity docs](https://docs.trinity.abilityai.dev) directly."

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
        state = str(response.answer.state.name) if response.answer else "UNKNOWN"

        return (json.dumps({
            "answer": answer_text or FALLBACK_ANSWER,
            "state": state,
            "session_id": session_id
        }), 200, headers)

    except Exception as e:
        return (json.dumps({"error": str(e)}), 500, headers)
