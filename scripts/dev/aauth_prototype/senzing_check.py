#!/usr/bin/env python3
"""ent#879 — a sanctions check against a Senzing-shaped AAuth resource, as an agent, from anywhere.

Drives `POST /api/agents/<agent>/aauth/resource-call` with the agent's OWN MCP key
(the route refuses a human/admin token with 403 `aauth_agent_only`), so it works
from a laptop as well as from `demo.sh` step 8. Standard library only.

    senzing_check.py --agent test-echo --key "$TRINITY_MCP_API_KEY" \
        --name "<a sanctioned name>" --country "<country>" [--api http://localhost:8000]
        [--resource https://senzing.aauth.dev] [--path /v1/search_entities]
        [--execution-id <id>] [--no-wait] [--once]

What it prints, in order: the identity the call runs as (`token_kind` — agent /
person / auth), the challenge sequence the resource actually issued
(`challenges`, e.g. person-token → auth-token → 200), the top candidates'
`match_key` / `match_level` / entity name, and the `AAuth-Budget` cost; then a
SECOND call with a new `dedup_label` that must show `challenges: []` and
`token_kind: auth` — the held auth token, zero person-server trips. When the
person server needs the owner's consent (`interaction_required`), the link +
code are printed for the OWNER and the pending handle is polled honouring
`retry_after` (`--no-wait` prints the link and exits 3 instead — the rehearse
mode). Nothing printed here is a token: the response has no field for one.

Exit codes: 0 both calls answered · 2 bad arguments / HTTP refusal · 3 consent
still pending (`--no-wait`) · 4 denied / expired.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HANDLE_POLLS = 60


def post(api: str, agent: str, key: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{api}/api/agents/{agent}/aauth/resource-call", method="POST",
        data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"detail": raw[:500]}


def candidates(body_text: str, limit: int = 5) -> list[dict]:
    """Senzing's `search_entities` shape, read tolerantly: top candidates with match key / level."""
    try:
        doc = json.loads(body_text)
    except ValueError:
        return []
    rows = doc.get("RESOLVED_ENTITIES") if isinstance(doc, dict) else None
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        info = row.get("MATCH_INFO") or {}
        ent = (row.get("ENTITY") or {}).get("RESOLVED_ENTITY") or {}
        out.append({
            "match_key": info.get("MATCH_KEY"),
            "match_level": info.get("MATCH_LEVEL_CODE") or info.get("MATCH_LEVEL"),
            "entity": ent.get("ENTITY_NAME") or ent.get("ENTITY_ID"),
        })
        if len(out) >= limit:
            break
    return out


def show(label: str, status: int, out: dict) -> None:
    print(f"  {label}: HTTP {status} outcome={out.get('outcome')}")
    if out.get("outcome") == "result":
        print(f"    identity used : token_kind={out.get('token_kind')}  (the platform signed as the agent; no token shown)")
        chain = " → ".join(out.get("challenges") or []) or "(no challenge)"
        print(f"    challenges    : {chain} → {out.get('status')}")
        budget = out.get("budget") or {}
        print(f"    AAuth-Budget  : cost={budget.get('cost')} balance={budget.get('balance')} unit={budget.get('unit')}")
        rows = candidates(out.get("body") or "")
        if rows:
            print("    top candidates:")
            for r in rows:
                print(f"      match_key={r['match_key']}  match_level={r['match_level']}  entity={r['entity']}")
        else:
            text = (out.get("body") or "")[:300].replace("\n", " ")
            print(f"    body          : {text}{'…' if len(out.get('body') or '') > 300 else ''}")
        if out.get("replayed"):
            print("    (replayed from the effect-guard snapshot — no request was sent)")


def run_call(args, label: str, dedup_label: str) -> tuple[int, dict]:
    body = {
        "resource": args.resource, "method": "POST", "path": args.path,
        "body": {"FEATURES": [{"NAME_FULL": args.name}, {"ADDR_COUNTRY": args.country}]},
        "justification": f"Sanctions screening of '{args.name}' ({args.country}) for the AAuth demo",
        "dedup_label": dedup_label, "execution_id": args.execution_id, "trigger_source": "script",
    }
    status, out = post(args.api, args.agent, args.key, body)
    handle_seen = None
    for _ in range(HANDLE_POLLS):
        if status != 200:
            detail = out.get("detail") if isinstance(out, dict) else out
            reason = detail.get("reason") if isinstance(detail, dict) else None
            message = detail.get("message") if isinstance(detail, dict) else detail
            print(f"  {label}: HTTP {status} refusal reason={reason} — {message}")
            if isinstance(detail, dict) and detail.get("possibly_executed"):
                print("    possibly_executed: the resource may have run the call — not re-sent")
            return status, out
        outcome = out.get("outcome")
        if outcome in ("result", "denied", "expired"):
            show(label, status, out)
            return status, out
        inter = out.get("interaction") or {}
        handle = inter.get("pending_handle")
        if handle != handle_seen:
            handle_seen = handle
            if outcome == "interaction_required":
                print()
                print("  OWNER — open this link, privately (whoever approves first becomes the person this agent acts for):")
                print(f"    {inter.get('link')}")
                print(f"    url {inter.get('url')} · code {inter.get('code')} · expires_at {inter.get('expires_at')}")
                print()
            else:
                print(f"  {label}: pending ({inter.get('requirement')} at {inter.get('at')})")
        if args.no_wait:
            print(f"  {label}: consent pending — rerun without --no-wait after the owner approves (handle kept {inter.get('retry_after')}s+)")
            return 202, out
        wait = max(1, int(inter.get("retry_after") or 5))
        print(f"    polling again in {wait}s …")
        time.sleep(wait)
        status, out = post(args.api, args.agent, args.key,
                           {"pending_handle": handle, "wait": 15, "execution_id": args.execution_id,
                            "trigger_source": "script"})
    print(f"  {label}: gave up after {HANDLE_POLLS} polls")
    return 202, out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--api", default=os.environ.get("A_API", "http://localhost:8000"))
    ap.add_argument("--agent", required=True)
    ap.add_argument("--key", default=os.environ.get("TRINITY_MCP_API_KEY"), help="the agent's OWN MCP key")
    ap.add_argument("--resource", default=os.environ.get("AAUTH_SENZING", "https://senzing.aauth.dev"))
    ap.add_argument("--path", default="/v1/search_entities")
    ap.add_argument("--name", required=True, help="a sanctioned name (e.g. from OpenSanctions)")
    ap.add_argument("--country", required=True)
    ap.add_argument("--execution-id", default="manual",
                    help="the effect guard arms only for a real execution of this agent; `manual` (default) "
                         "proceeds unguarded without an alarm, an unknown id is refused on a pull-pilot agent")
    ap.add_argument("--no-wait", action="store_true", help="print the consent link and exit 3 instead of polling")
    ap.add_argument("--once", action="store_true", help="skip the second (cached auth token) call")
    args = ap.parse_args()
    if not args.key:
        print("missing --key (or TRINITY_MCP_API_KEY): the agent's own MCP key", file=sys.stderr)
        return 2

    stamp = int(time.time())
    print(f"== {args.agent} → {args.resource}{args.path} as the agent (via {args.api})")
    status, out = run_call(args, "call 1", f"sanctions-{stamp}-1")
    if status == 202:
        return 3
    if status != 200:
        return 2
    if out.get("outcome") in ("denied", "expired"):
        print(f"    reason={out.get('reason')} — {out.get('next_step')}")
        return 4
    if args.once:
        return 0

    print()
    print("== second call, new dedup_label: the held auth token, zero person-server trips")
    status, out = run_call(args, "call 2", f"sanctions-{stamp}-2")
    if status != 200 or out.get("outcome") != "result":
        return 2 if status != 200 else 4
    ok = out.get("token_kind") == "auth" and not out.get("challenges")
    print(f"  zero PS trips: {'yes' if ok else 'NO'} (token_kind={out.get('token_kind')}, challenges={out.get('challenges')})")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
