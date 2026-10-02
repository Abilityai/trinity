"""#3177: intended-recipient consent links survive; credential URLs redact."""
import importlib.util
import json
from pathlib import Path
from urllib.parse import urlencode

import pytest

ROOT = Path(__file__).resolve().parents[2]
def load_module(path):
    spec = importlib.util.spec_from_file_location("consent_sanitizer", ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

MODULES = [load_module("docker/base-image/agent_server/utils/credential_sanitizer.py"),
           load_module("src/backend/utils/credential_sanitizer.py")]
sanitizer = None


def consent_url(**changes):
    query = {"access_type": "offline", "client_id": "123-example.apps.googleusercontent.com",
             "redirect_uri": "http://127.0.0.1:34837/oauth2/callback", "response_type": "code",
             "scope": "https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/gmail.modify",
             "state": "public-consent-state", "prompt": "consent", "include_granted_scopes": "true"}
    query.update(changes)
    return "https://accounts.google.com/o/oauth2/auth?" + urlencode(query)


@pytest.fixture(autouse=True, params=MODULES, ids=["agent", "backend"])
def isolated_credentials(monkeypatch, request):
    global sanitizer
    sanitizer = request.param
    if hasattr(sanitizer, "_credential_values"):
        monkeypatch.setattr(sanitizer, "_credential_values", set())


@pytest.mark.parametrize("wrap", [lambda url: url, lambda url: "Open " + url,
    lambda url: "[Authorize the Google connection](" + url + ")",
    lambda url: json.dumps({"auth_url": url}), lambda url: "<" + url + ">"])
def test_public_consent_url_survives_all_output_shapes(wrap):
    text = wrap(consent_url())
    assert sanitizer.sanitize_text(text) == text


def test_structured_tool_output_preserves_public_consent():
    value = {"type": "tool_result", "content": json.dumps({"auth_url": consent_url()})}
    assert json.loads(sanitizer.sanitize_json_string(json.dumps(value))) == value


def test_v2_consent_endpoint_preserved():
    url = consent_url().replace("/o/oauth2/auth?", "/o/oauth2/v2/auth?")
    assert sanitizer.sanitize_text(url) == url


@pytest.mark.parametrize("change", [
    {"state": "safe&access_token=private-token"}, {"client_id": "safe&client_secret=private.apps.googleusercontent.com"},
    {"scope": "https://www.googleapis.com/auth/calendar&access_token=private-token"},
    {"code": "private-code"}, {"access_token": "private-token"}, {"refresh_token": "private-refresh"},
    {"client_secret": "private-secret"}, {"unknown": "private-value"}, {"response_type": "token"},
    {"redirect_uri": "https://evil.example/callback"}, {"redirect_uri": "http://127.0.0.1:34837/wrong"},
    {"redirect_uri": "http://127.0.0.1:34837/oauth2/callback?code=private"},
])
def test_nonpublic_or_unrecognized_query_still_redacts(change):
    url = consent_url(**change)
    result = sanitizer.sanitize_text(url)
    assert result != url
    assert sanitizer.REDACTION_PLACEHOLDER in result


@pytest.mark.parametrize("mutate", [
    lambda url: url.replace("accounts.google.com", "accounts.google.com.evil.example"),
    lambda url: url.replace("https:", "http:", 1),
    lambda url: url + "&state=duplicate", lambda url: url + "#access_token=private",
])
def test_malformed_or_imposter_authorize_url_still_redacts(mutate):
    url = mutate(consent_url())
    assert sanitizer.sanitize_text(url) != url


def test_callback_url_is_not_exempted():
    url = "http://127.0.0.1:34837/oauth2/callback?code=private-code&state=public-consent-state"
    assert "private-code" not in sanitizer.sanitize_text(url)


@pytest.mark.parametrize("known", ["entire_url", "public-consent-state"])
def test_known_credential_replacement_still_precedes_consent_exception(monkeypatch, known):
    if not hasattr(sanitizer, "_credential_values"):
        pytest.skip("Backend sanitizer has no exact credential cache")
    url = consent_url()
    secret = url if known == "entire_url" else known
    monkeypatch.setattr(sanitizer, "_credential_values", {secret})
    result = sanitizer.sanitize_text(url)
    assert secret not in result
    assert sanitizer.REDACTION_PLACEHOLDER in result


def test_secret_value_patterns_still_redact_inside_authorize_url():
    secret = "sk-proj-" + "a" * 32
    assert secret not in sanitizer.sanitize_text(consent_url(state=secret))


def test_adjacent_secret_and_secret_assignment_remain_redacted():
    url = consent_url()
    result = sanitizer.sanitize_text("[Authorize](" + url + ") API_TOKEN=private-secret")
    assert url in result
    assert "private-secret" not in result
    assert url not in sanitizer.sanitize_text("AUTH_SECRET=" + url)


def test_percent_encoded_secret_pattern_cannot_hide_in_public_state():
    secret = "sk-proj-" + "a" * 32
    url = consent_url(state=secret).replace("state=sk-", "state=%73k-")
    result = sanitizer.sanitize_text(url)
    assert "%73k-proj-" not in result
    assert sanitizer.REDACTION_PLACEHOLDER in result


def test_encoded_known_credential_cannot_hide_in_public_state(monkeypatch):
    if not hasattr(sanitizer, "_credential_values"):
        pytest.skip("Backend sanitizer has no exact credential cache")
    secret = "registered-private-value"
    monkeypatch.setattr(sanitizer, "_credential_values", {secret})
    url = consent_url(state=secret).replace("state=registered", "state=%72egistered")
    result = sanitizer.sanitize_text(url)
    assert "%72egistered-private-value" not in result
    assert sanitizer.REDACTION_PLACEHOLDER in result


@pytest.mark.parametrize("origin", [
    "https://accounts.google.com:443", "https://user@accounts.google.com",
    "https://accounts.google.com.", "https://accounts.google.com%2eevil.example",
    "https://accounts.google.com\\@evil.example",
])
def test_origin_variants_do_not_receive_an_exception(origin):
    url = consent_url().replace("https://accounts.google.com", origin, 1)
    assert sanitizer.sanitize_text(url) != url


@pytest.mark.parametrize("redirect", [
    "http://127.0.0.1:0/oauth2/callback", "http://127.0.0.1:65536/oauth2/callback",
    "http://127.0.0.1/oauth2/callback", "http://localhost:34837/oauth2/callback",
    "http://127.0.0.1.evil.example:34837/oauth2/callback",
    "http://user:password@127.0.0.1:34837/oauth2/callback",
    "http://127.0.0.1:34837/oauth2/callback#fragment",
])
def test_redirect_variants_do_not_receive_an_exception(redirect):
    url = consent_url(redirect_uri=redirect)
    assert sanitizer.sanitize_text(url) != url


@pytest.mark.parametrize("suffix", [
    "&%73tate=duplicate", "&%61ccess_token=private", "&malformed",
    "&code_challenge=unsupported&code_challenge_method=S256",
])
def test_encoded_duplicate_unknown_and_unsupported_fields_fail_closed(suffix):
    url = consent_url() + suffix
    assert sanitizer.sanitize_text(url) != url


@pytest.mark.parametrize("state", ["", "a" * 257, "%2573k-proj-" + "a" * 32])
def test_state_boundaries_fail_closed(state):
    url = consent_url(state=state)
    assert sanitizer.sanitize_text(url) != url


@pytest.mark.parametrize("prefix,suffix", [("AUTH_SECRET=", ""), ("AUTH_SECRET='", "'"), ("AUTH_SECRET=\"", "\"")])
def test_explicit_secret_assignments_take_precedence(prefix, suffix):
    url = consent_url()
    assert url not in sanitizer.sanitize_text(prefix + url + suffix)
