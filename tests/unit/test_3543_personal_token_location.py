"""#3543 — messages that send the user to their saved personal GitHub token name
a place that exists on the Settings page: the "MCP Keys" tab, card "Personal
GitHub Token". Backend and frontend use the same wording, and the wording is
checked against the labels Settings actually renders, so a renamed tab or card
fails here.
"""
import re
from pathlib import Path

import pytest

from services.agent_service import fork_to_own

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "src" / "frontend" / "src"
LOCATION = "Settings → MCP Keys → Personal GitHub Token"
MESSAGE_FILES = [
    FRONTEND / "components" / "SavedGithubTokenField.vue",
    FRONTEND / "components" / "BindRepoPanel.vue",
    FRONTEND / "components" / "CreateAgentModal.vue",
]


def test_location_matches_the_rendered_settings_labels():
    settings = (FRONTEND / "views" / "Settings.vue").read_text()
    panel = (FRONTEND / "components" / "settings" / "UserGitHubPatPanel.vue").read_text()
    tab = re.search(r"id: 'mcp-keys',\s*label: '([^']+)'", settings).group(1)
    heading = re.search(r"<h2[^>]*>([^<]+)</h2>", panel).group(1).strip()
    assert "<UserGitHubPatPanel" in settings.split("activeTab === 'mcp-keys'", 1)[1].split("</template>", 1)[0]
    assert LOCATION == f"Settings → {tab} → {heading}"


def test_backend_messages_name_the_location():
    assert LOCATION in fork_to_own.fork_pat_required().detail["error"]
    msg = fork_to_own.saved_token_message(next(iter(fork_to_own._TOKEN_FAILURE_CODES)), "x.")
    assert LOCATION in msg


@pytest.mark.parametrize("path", MESSAGE_FILES, ids=lambda p: p.name)
def test_frontend_messages_name_the_location(path):
    text = path.read_text()
    assert LOCATION in text
    assert "Settings → GitHub token" not in text
