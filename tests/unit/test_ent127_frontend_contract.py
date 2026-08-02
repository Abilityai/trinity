"""Frontend contract for the credential checklist (trinity-enterprise#127 §3.4, §4).

`src/frontend` has NO component-test runner in this repo (only Playwright e2e
against a live stack, gated on the `ui` label), so the binding rendering
contract is enforced two ways here:

  * source-anchored assertions over the two SFCs — each anchored on a construct
    that must EXIST, so a rename fails the test instead of silently matching
    nothing (the `str.find` -> -1 -> empty-slice trap);
  * a real execution round-trip: `parseEnvText` / `formatEnvContent` are
    extracted from the SFC and run under node, so the corruption fix is proven
    rather than grepped.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "src" / "frontend" / "src"
CHECKLIST = FRONTEND / "components" / "CredentialSetupChecklist.vue"
PANEL = FRONTEND / "components" / "CredentialsPanel.vue"
STORE = FRONTEND / "stores" / "agents.js"


def _template_section(sfc: str) -> str:
    """The SFC's `<template>` block only.

    The `<script>` block's own docstring quotes the very constructs these tests
    forbid (`v-html`, a hostile `<a href=...>OpenAI API keys</a>`) in order to
    explain why they are forbidden, so a whole-file grep matches the
    documentation and never reaches the markup.
    """
    start = sfc.find("<template>")
    end = sfc.rfind("</template>")
    assert start != -1 and end > start, "no <template> block — re-anchor this test"
    return sfc[start:end]


@pytest.fixture(scope="module")
def checklist():
    assert CHECKLIST.exists(), "the checklist component is missing"
    return _template_section(CHECKLIST.read_text())


@pytest.fixture(scope="module")
def checklist_script():
    """The `<script setup>` block — where the render helpers live."""
    sfc = CHECKLIST.read_text()
    return sfc[: sfc.find("<template>")]


@pytest.fixture(scope="module")
def panel():
    return PANEL.read_text()


# ---------------------------------------------------------------------------
# §3.4 rendering contract
# ---------------------------------------------------------------------------


class TestRenderingContract:
    def test_no_markdown_and_no_v_html(self, checklist):
        """Author-controlled text is TEXT.

        Markdown is a WIDENING here, not a mitigation: it hands the template
        author an arbitrary `[label](url)` surface immediately beside a
        credential input, which is exactly what having ONE validated `setup_url`
        exists to prevent. `v-html` is banned outright (H-005).
        """
        assert "v-html" not in checklist
        assert "renderMarkdown" not in checklist
        assert "utils/markdown" not in checklist

    def test_anchor_text_is_the_parsed_host_never_the_title(self, checklist):
        """`<a href="https://evil.tld">OpenAI API keys</a>` recreates the
        userinfo attack in pure HTML with no validator in the way."""
        anchors = re.findall(r"<a\b[^>]*>(.*?)</a>", checklist, re.S)
        assert anchors, "no anchor found — re-anchor this test on the new markup"
        for body in anchors:
            assert "row.title" not in body
            assert "row.description" not in body
            assert "hostParts(row)" in body

    def test_anchor_carries_the_full_safety_attribute_set(self, checklist):
        anchors = re.findall(r"<a\b[^>]*?>", checklist, re.S)
        assert anchors
        for tag in anchors:
            assert 'rel="noopener noreferrer"' in tag
            assert 'referrerpolicy="no-referrer"' in tag

    def test_link_is_gated_on_a_verified_https_host(self, checklist, checklist_script):
        """`display_host === null` means the host could not be verified: render
        inert text, never an anchor. Re-checked at render — the backend
        validates, and the UI must not become the second authority that forgets.
        """
        assert "function isLinkable" in checklist_script
        assert "row.setup_url_display_host" in checklist_script
        assert re.search(r"/\^https:\\/\\//i\.test", checklist_script)
        assert 'v-if="isLinkable(row)"' in checklist
        # The fail-closed branch must exist and must NOT be a link.
        assert "host could not be verified" in checklist

    def test_secret_inputs_are_masked_by_default(self, checklist):
        """`secret` defaults to true, so the check is `!== false`: an absent or
        malformed value must still mask."""
        assert "row.secret !== false && !revealed[row.name] ? 'password' : 'text'" in checklist

    def test_default_is_a_placeholder_and_only_when_not_secret(self, checklist, checklist_script):
        """Nothing enforces the schema's "NEVER put a real credential here", so
        prefilling `default` would turn author YAML — or a prompt-injected
        agent's own rewritten template.yaml — into a one-click credential
        write, with `secret: true` MASKING what the operator submits."""
        assert "function placeholderFor" in checklist_script
        assert "if (row.secret === false && row.default) return row.default" in checklist_script
        # `default` must never reach a value binding.
        assert ":value=\"row.default\"" not in checklist
        assert "values[row.name] = row.default" not in checklist

    def test_format_is_never_mapped_onto_a_dom_attribute(self, checklist):
        """`format` is an OPEN vocabulary — an unrecognised value reaches here."""
        assert ':type="row.format"' not in checklist
        assert ':pattern="row.format"' not in checklist

    def test_tri_state_required_has_its_own_rendering(self, checklist):
        assert 'row.required === true' in checklist
        assert 'row.required === false' in checklist
        assert "not stated" in checklist


class TestDegradedIsReachable:
    def test_requirements_are_fetched_unconditionally(self, panel):
        """`loadCredentialStatus` returns early unless the agent is running.
        Copying that gate onto the checklist would make the entire degraded
        design — and its tests — dead code."""
        loader = _function_source(panel, "loadRequirements")
        assert "agentStatus !== 'running'" not in loader
        assert "props.agentStatus" not in loader

    def test_only_the_inputs_are_gated_on_running(self, panel, checklist):
        assert ':can-submit="agentStatus === \'running\'"' in panel
        assert ':disabled="!canSubmit || saving"' in checklist

    def test_checklist_is_mounted_outside_the_running_guard(self, panel):
        assert "<CredentialSetupChecklist" in panel


class TestWritePath:
    def test_merge_base_is_mandatory(self, panel):
        """`formatEnvContent` rewrites `.env` wholesale, so a merge base we
        failed to read is not "start fresh" — it wipes every credential already
        configured. Only a genuine 404 is a safe empty base."""
        src = _function_source(panel, "readExistingEnv")
        assert "err.response?.status === 404" in src
        assert "throw new Error" in src
        assert "nothing was written" in src

    def test_the_swallow_all_fallback_is_gone(self, panel):
        assert "// File doesn't exist, start fresh" not in panel

    def test_checklist_writes_through_the_existing_inject_path(self, panel):
        """One writer, no new backend write surface."""
        src = _function_source(panel, "saveChecklistCredentials")
        assert "readExistingEnv()" in src
        assert "agentsStore.injectCredentials" in src
        assert "loadRequirements()" in src

    def test_store_action_uses_the_single_api_client(self):
        """Invariant #7: `api.js` owns the auth interceptor and the 401
        redirect. The raw-axios neighbours predate it."""
        store = STORE.read_text()
        src = _function_source(store, "getCredentialRequirements", arrow=False)
        assert "api.get(" in src
        assert "axios" not in src


# ---------------------------------------------------------------------------
# Executed round-trip — the corruption fix, proven not grepped
# ---------------------------------------------------------------------------


def _function_source(text: str, name: str, arrow: bool = True) -> str:
    """The source of one function from an SFC/JS module, brace-matched.

    Anchored on a construct that must EXIST: a rename raises here rather than
    silently returning an empty string that every assertion then passes against.
    """
    patterns = [
        "const {0} = async (".format(name),
        "const {0} = (".format(name),
        "async {0}(".format(name),
        "{0}(".format(name),
    ]
    start = -1
    for pattern in patterns:
        start = text.find(pattern)
        if start != -1:
            break
    assert start != -1, "function {0!r} not found — re-anchor this test".format(name)
    brace = text.find("{", text.find(")", start))
    assert brace != -1
    depth = 0
    for i in range(brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[brace : i + 1]
    raise AssertionError("unbalanced braces extracting {0!r}".format(name))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
class TestEnvRoundTrip:
    """`parseEnvText(formatEnvContent(x)) == x`.

    Before the fix, `formatEnvContent` wrote `\\"` for every `"` and
    `parseEnvText` stripped the surrounding quotes but never unescaped — so a
    value containing a quote grew one backslash PER SUBMIT, for every other
    credential in the file, not just the one being edited. Latent while Quick
    Inject was a rare bulk paste; a per-row checklist makes read-merge-write the
    normal interaction.
    """

    VALUES = [
        "sk-plain-token",
        'has"quote',
        'lead"and"trail"',
        "has spaces in it",
        "has=equals=signs",
        "has'single'quotes",
        "trailing-hash # not-a-comment",
        "",
    ]

    def _round_trip(self, value, times=1):
        panel = PANEL.read_text()
        script = (
            "const parseEnvText = (text) => " + _function_source(panel, "parseEnvText") + ";\n"
            "const formatEnvContent = (credentials) => "
            + _function_source(panel, "formatEnvContent")
            + ";\n"
            "let creds = JSON.parse(process.argv[1]);\n"
            "for (let i = 0; i < Number(process.argv[2]); i++) {\n"
            "  creds = parseEnvText(formatEnvContent(creds));\n"
            "}\n"
            "process.stdout.write(JSON.stringify(creds));\n"
        )
        proc = subprocess.run(
            ["node", "-e", script, json.dumps({"KEY": value, "OTHER": 'sib"ling'}), str(times)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    @pytest.mark.parametrize("value", VALUES)
    def test_single_round_trip(self, value):
        assert self._round_trip(value)["KEY"] == value

    @pytest.mark.parametrize("value", VALUES)
    def test_repeated_submits_do_not_accumulate_escapes(self, value):
        """The actual reported shape: `a"b` -> `a\\"b` -> `a\\\\"b` -> ...,
        one backslash per submit."""
        assert self._round_trip(value, times=5)["KEY"] == value

    def test_untouched_siblings_survive_repeated_submits(self):
        """The corruption hit every OTHER credential in the file, which is what
        made it a data-loss bug rather than an editing annoyance."""
        assert self._round_trip("plain", times=5)["OTHER"] == 'sib"ling'
