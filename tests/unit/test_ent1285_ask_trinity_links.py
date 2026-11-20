"""Ask Trinity answer-link sanitiser (trinity-enterprise#1285).

The answer model invents docs URLs by prefixing the docs domain onto the repo
path of the chunk it read (`https://docs.ability.ai/automation/scheduling`,
`.../digitalocean.md`) — measured live: 7 of 10 eval answers carried one, all
404. The prompt alone cannot stop that, because the real URL is usually not in
the retrieved context. `src/ask-trinity/links.py` checks every link against the
docs site's own catalogue (llms.txt) and repairs or unlinks the rest, so a link
the bot returns is one the site serves.
"""
import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "src" / "ask-trinity" / "links.py"
_spec = importlib.util.spec_from_file_location("ask_trinity_links", _PATH)
links = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(links)

CATALOGUE = """# Trinity

> Header.

## Guides

- [Scheduling](https://docs.ability.ai/guides/scheduling): Cron schedules
- [Credential Management](https://docs.ability.ai/guides/credential-management): Credentials
- [Monitoring](https://docs.ability.ai/guides/monitoring): Agent monitoring

## Deploying Trinity

- [Deploy on DigitalOcean](https://docs.ability.ai/getting-started/deploying/digitalocean): DO
- [Deployment Monitoring](https://docs.ability.ai/getting-started/deploying/monitoring): Probes

## FAQ

- [Deployment & Upgrades](https://docs.ability.ai/faq/deployment-and-upgrades): FAQ

## Videos

- [Video Library](https://docs.ability.ai/videos): All videos
- [Inside Trinity](https://youtu.be/Tam-o-cPDmk): Overview
"""

KNOWN = links.parse_catalogue(CATALOGUE)


def fix(text):
    return links.sanitize_links(text, KNOWN)


def test_parse_catalogue_collects_page_and_video_urls():
    assert "https://docs.ability.ai/guides/scheduling" in KNOWN
    assert "https://youtu.be/Tam-o-cPDmk" in KNOWN
    assert len(KNOWN) == 8


def test_known_urls_are_kept_unchanged():
    text = "**Read more:** [Scheduling](https://docs.ability.ai/guides/scheduling)\n**Watch:** [Inside Trinity](https://youtu.be/Tam-o-cPDmk)"
    assert fix(text) == text


def test_docs_domain_plus_repo_path_is_repaired_by_path_suffix():
    assert fix("[S](https://docs.ability.ai/automation/scheduling)") == "[S](https://docs.ability.ai/guides/scheduling)"
    assert fix("[D](https://docs.ability.ai/guides/deploying/digitalocean.md)") == "[D](https://docs.ability.ai/getting-started/deploying/digitalocean)"


def test_relative_md_link_is_repaired():
    assert fix("[S](../automation/scheduling.md)") == "[S](https://docs.ability.ai/guides/scheduling)"


def test_flattened_path_is_repaired():
    # the indexed file name flattens `/` to `-`
    assert fix("[F](https://docs.ability.ai/faq-deployment-and-upgrades)") == "[F](https://docs.ability.ai/faq/deployment-and-upgrades)"


def test_ambiguous_slug_disambiguated_by_longer_suffix_or_unlinked():
    assert fix("[M](https://docs.ability.ai/deploying/monitoring)") == "[M](https://docs.ability.ai/getting-started/deploying/monitoring)"
    # two pages end in /monitoring — no way to choose, so no link
    assert fix("[Monitoring](https://docs.ability.ai/monitoring.md)") == "Monitoring"


def test_unknown_docs_page_and_relative_md_are_unlinked():
    assert fix("See [Agent network](https://docs.ability.ai/collaboration/agent-network).") == "See Agent network."
    assert fix("See [videos](videos.md).") == "See [videos](https://docs.ability.ai/videos)."
    assert fix("See [nothing](nothing-like-it.md).") == "See nothing."


def test_invented_video_id_is_unlinked():
    assert fix("**Watch:** [Fake](https://youtu.be/AAAAAAAAAAA)") == ""


def test_other_absolute_links_are_left_alone():
    text = "[GitHub](https://github.com/abilityai/trinity)"
    assert fix(text) == text


def test_read_more_line_with_no_surviving_link_is_removed():
    text = "Answer body.\n\n**Read more:** [Nope](https://docs.ability.ai/nope/nope)\n**Watch:** [Inside Trinity](https://youtu.be/Tam-o-cPDmk)"
    assert fix(text) == "Answer body.\n\n**Watch:** [Inside Trinity](https://youtu.be/Tam-o-cPDmk)"


def test_bare_urls_are_repaired_or_removed():
    # the live Q6 answer shape: a plain label, then one bare URL per line
    text = (
        "Body.\n\nRead more:\n"
        "https://docs.ability.ai/guides/deploying/monitoring\n"
        "https://docs.ability.ai/faq-deployment-and-upgrades"
    )
    assert fix(text) == (
        "Body.\n\nRead more:\n"
        "https://docs.ability.ai/getting-started/deploying/monitoring\n"
        "https://docs.ability.ai/faq/deployment-and-upgrades"
    )
    assert fix("See https://docs.ability.ai/collaboration/agent-network for more.") == "See for more."


def test_label_line_left_with_no_url_below_is_removed():
    text = "Body.\n\nRead more:\nhttps://docs.ability.ai/nope/nope\n\nWatch:\nhttps://youtu.be/Tam-o-cPDmk"
    assert fix(text) == "Body.\n\n\nWatch:\nhttps://youtu.be/Tam-o-cPDmk"


def test_link_whose_text_is_the_url_carries_the_repaired_url_in_both():
    bad = "https://docs.ability.ai/credentials/credential-management"
    good = "https://docs.ability.ai/guides/credential-management"
    assert fix(f"*   [{bad}]({bad})") == f"*   [{good}]({good})"
    # unfixable: the visible text is itself a dead URL, so the item goes
    gone = "https://docs.ability.ai/nope/nope"
    assert fix(f"Read more:\n*   [{gone}]({gone})\n*   [{good}]({good})") == f"Read more:\n*   [{good}]({good})"


def test_bare_url_inside_inline_code_is_left_alone():
    text = "Run `curl https://docs.ability.ai/install.sh | bash`"
    assert fix(text) == text


def test_empty_catalogue_only_drops_relative_md_links():
    # catalogue unavailable: cannot judge absolute URLs, but a repo path is never a valid link
    text = "[S](https://docs.ability.ai/x) and [R](../a/b.md)"
    assert links.sanitize_links(text, set()) == "[S](https://docs.ability.ai/x) and R"
