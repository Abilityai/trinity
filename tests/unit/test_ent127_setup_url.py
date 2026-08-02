"""`describe_setup_url` — the inherited IDN residual (trinity-enterprise#127 §3.3).

`template_service._setup_url_error`'s docstring records an explicitly UNCLOSED
residual: it rejects the `user@host` form but IDN homograph hosts survive, so
"a consumer MUST render the parsed hostname next to the link". #127 is that
consumer and the first renderer of the field.

The table below is the whole point of the module. Each row is a host that
PASSES `_setup_url_error` today, paired with what a browser actually resolves.
The rows that killed the obvious implementation (`hostname.encode("idna")`,
falling back to the raw host on failure) are marked.
"""

import pytest

from services.setup_url_display import describe_setup_url, describe_setup_url_or_none
from services.template_service import _setup_url_error


class TestCanonicalHost:
    def test_ascii_happy_path(self):
        got = describe_setup_url("https://platform.openai.com/api-keys")
        assert got["display_host"] == "platform.openai.com"
        assert got["registrable"] == "openai.com"
        assert got["verified"] is True
        assert got["idn"] is False

    def test_cyrillic_homograph_canonicalises_to_punycode(self):
        # The one case the stdlib codec also gets right.
        got = describe_setup_url("https://аpple.com/keys")
        assert got["display_host"] == "xn--pple-43d.com"
        assert got["verified"] is True
        assert got["idn"] is True

    def test_sharp_s_matches_browser_not_idna2003(self):
        # 🔴 stdlib `str.encode("idna")` gives `fass.de` — a DIFFERENT registrable
        # domain from the `xn--fa-hia.de` a browser visits. Displaying one while
        # resolving the other manufactures the split this module exists to close.
        got = describe_setup_url("https://faß.de/console")
        assert got["display_host"] == "xn--fa-hia.de"
        assert got["verified"] is True

    def test_final_sigma_matches_browser_not_idna2003(self):
        # 🔴 stdlib codec gives `xn--4xa.example`; browsers resolve `xn--3xa.example`.
        got = describe_setup_url("https://ς.example/token")
        assert got["display_host"] == "xn--3xa.example"

    def test_already_punycode_is_stable(self):
        got = describe_setup_url("https://xn--pple-43d.com/")
        assert got["display_host"] == "xn--pple-43d.com"
        # Already ASCII-canonical: nothing changed, so this is not "an IDN link".
        assert got["idn"] is False

    def test_host_is_lowercased(self):
        assert describe_setup_url("https://EXAMPLE.COM/A")["display_host"] == "example.com"


class TestFailsClosed:
    """Every failure path must be distinguishable from a pass.

    The rejected alternative returned the RAW host on exception, which made a
    failed check byte-identical to a successful one — it shipped the homograph
    under a clean flag.
    """

    @pytest.mark.parametrize(
        "url,why",
        [
            ("https://%D0%B0pple.com/", "percent-encoded authority: Python does not decode it, browsers do"),
            ("https://a..b.com/", "empty label — idna raises; the old code reported it clean"),
            ("https://" + "x" * 64 + ".com/", "over-long label — idna raises"),
            ("https://google.com／evil.tld", "NFKC-confusable delimiter — urlsplit itself raises ValueError"),
            ("https://[::1]/x", "IPv6 literal is not idna-encodable"),
            ("https://a_b.com/", "underscore is not a valid IDNA codepoint"),
            ("http://example.com/", "non-https: the describer must not vouch for it"),
            ("https:///justpath", "no host"),
            ("", "empty string"),
            (None, "not a string"),
            (12345, "not a string"),
        ],
    )
    def test_fails_closed(self, url, why):
        got = describe_setup_url(url)
        assert got["display_host"] is None, why
        assert got["registrable"] is None
        assert got["verified"] is False

    def test_never_raises_on_adversarial_input(self):
        for url in ["https://" + "а" * 300 + ".com", "https://.", "https://..", "https://:443", "://x"]:
            describe_setup_url(url)  # must not raise


class TestRegistrableEmphasis:
    """eTLD+1 is the part an operator can judge — punycode is irrelevant to the
    commonest attack, which is a pure-ASCII subdomain."""

    def test_subdomain_deception_emphasises_the_real_registrant(self):
        got = describe_setup_url("https://accounts.google.com.evil.tld/apikey")
        assert got["display_host"] == "accounts.google.com.evil.tld"
        assert got["registrable"] == "evil.tld"
        # NOT flagged as IDN — it is plain ASCII. A consumer keying its warning
        # off `idn` alone would wave this straight through.
        assert got["idn"] is False

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://console.example.co.uk/x", "example.co.uk"),
            ("https://example.co.uk/x", "example.co.uk"),
            ("https://a.b.c.example.com/x", "example.com"),
            ("https://example.com/x", "example.com"),
            ("https://localhost/x", "localhost"),
            ("https://myorg.github.io/x", "myorg.github.io"),
        ],
    )
    def test_registrable_domain(self, url, expected):
        assert describe_setup_url(url)["registrable"] == expected


class TestValidatorAgreement:
    """A URL the declaration validator rejects never reaches the describer —
    but if a future caller forgets, the describer must not vouch for it."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://google.com@evil.tld/apikey",   # userinfo
            "http://example.com/",                  # scheme
            "ftp://example.com/",
        ],
    )
    def test_rejected_urls_are_not_vouched_for(self, url):
        assert _setup_url_error(url) is not None
        if url.startswith("https://"):
            # userinfo survives urlsplit; the describer would happily canonicalise
            # `evil.tld`. That is *correct* (it is where the click goes) — the point
            # is that the anchor text is the describer's host, never the author's
            # title, so the label can no longer disagree with the destination.
            assert describe_setup_url(url)["display_host"] == "evil.tld"
        else:
            assert describe_setup_url(url)["display_host"] is None

    def test_accepted_url_is_describable(self):
        url = "https://platform.openai.com/api-keys"
        assert _setup_url_error(url) is None
        assert describe_setup_url(url)["verified"] is True


class TestOptionalWrapper:
    def test_none_in_none_out(self):
        assert describe_setup_url_or_none(None) is None

    def test_value_in_dict_out(self):
        assert describe_setup_url_or_none("https://example.com/")["display_host"] == "example.com"
