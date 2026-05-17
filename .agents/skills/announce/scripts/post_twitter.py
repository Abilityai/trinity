#!/usr/bin/env python3
"""Post a tweet via Twitter API v2 using OAuth 1.0a User Context.

Reads tweet text from stdin. Prints a single JSON line to stdout describing
the outcome and exits 0 on success / 1 on failure — matching the contract
the announce skill expects from each platform's send step.

Required env vars:
  ANNOUNCE_TWITTER_API_KEY
  ANNOUNCE_TWITTER_API_SECRET
  ANNOUNCE_TWITTER_ACCESS_TOKEN
  ANNOUNCE_TWITTER_ACCESS_TOKEN_SECRET
"""
import json
import os
import sys

REQUIRED = (
    "ANNOUNCE_TWITTER_API_KEY",
    "ANNOUNCE_TWITTER_API_SECRET",
    "ANNOUNCE_TWITTER_ACCESS_TOKEN",
    "ANNOUNCE_TWITTER_ACCESS_TOKEN_SECRET",
)

MAX_LEN = 280


def fail(msg, **extra):
    payload = {"ok": False, "error": msg}
    payload.update(extra)
    print(json.dumps(payload))
    sys.exit(1)


def main():
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        fail(f"missing env vars: {', '.join(missing)}")

    text = sys.stdin.read().rstrip("\n")
    if not text:
        fail("empty message")
    if len(text) > MAX_LEN:
        fail(f"message exceeds {MAX_LEN} chars (got {len(text)})", length=len(text))

    try:
        from requests_oauthlib import OAuth1Session
    except ImportError:
        fail(
            "requests-oauthlib not installed; run: "
            "python3 -m pip install --user requests-oauthlib"
        )

    oauth = OAuth1Session(
        os.environ["ANNOUNCE_TWITTER_API_KEY"],
        client_secret=os.environ["ANNOUNCE_TWITTER_API_SECRET"],
        resource_owner_key=os.environ["ANNOUNCE_TWITTER_ACCESS_TOKEN"],
        resource_owner_secret=os.environ["ANNOUNCE_TWITTER_ACCESS_TOKEN_SECRET"],
    )

    try:
        resp = oauth.post(
            "https://api.twitter.com/2/tweets",
            json={"text": text},
            timeout=30,
        )
    except Exception as exc:
        fail(f"network error: {exc}")

    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:500]}

    if resp.status_code == 201 and isinstance(body, dict) and body.get("data", {}).get("id"):
        tweet_id = body["data"]["id"]
        print(json.dumps({"ok": True, "id": tweet_id, "url": f"https://x.com/i/status/{tweet_id}"}))
        sys.exit(0)

    fail(
        f"twitter api error (HTTP {resp.status_code})",
        status=resp.status_code,
        body=body,
    )


if __name__ == "__main__":
    main()
