/**
 * ent#1101 — external MCP access is off by default on a fresh install.
 *
 * Two leaves, no fastmcp dependency:
 *
 *  - `classifyPeer`: a socket peer is ON-BOX when it is loopback or inside the
 *    agent network (`trinity-agent-network`, 172.28.0.0/16) but NOT that
 *    network's gateway `.0.1` — the gateway is what a host-forwarded raw-port
 *    caller (or Docker Desktop) arrives as. Everything else is OFF-BOX: the
 *    ingress /24 (nginx, cloudflared), every other bridge gateway, 172.29/16,
 *    public addresses, IPv6 ULAs (IPv6-enabled daemons are unsupported for
 *    the gate and read as off-box), and a socket that is already gone.
 *
 *  - `ExternalAccessGate`: the backend is the SOLE resolver of the switch
 *    (Settings row → `MCP_EXTERNAL_ACCESS` env opt-in → default off); this
 *    server never reads the env itself. The verdict comes from
 *    `GET /api/internal/mcp-external-access` behind `X-Internal-Secret`, is
 *    pre-fetched with a bounded retry before the transport starts, cached for
 *    a TTL, and kept (stale-while-error) through a backend blip. With no
 *    verdict obtainable — never answered yet, or `INTERNAL_API_SECRET` empty so
 *    the backend can never answer — the gate is "unverified": the off-box
 *    caller gets a 503 + Retry-After, never a silent pass.
 */
import { isIP } from "node:net";

export type PeerClass = "on-box" | "off-box";

/** Must equal docker-compose.yml's `trinity-agent-network` subnet (parity-tested). */
export const AGENT_NETWORK_SUBNET = "172.28.0.0/16";
/** The agent network's gateway: a host-forwarded caller arrives as this. */
export const AGENT_NETWORK_GATEWAY = "172.28.0.1";

const [SUBNET_BASE, SUBNET_BITS] = AGENT_NETWORK_SUBNET.split("/");
const SUBNET_MASK = SUBNET_BITS === "0" ? 0 : (~0 << (32 - Number(SUBNET_BITS))) >>> 0;

function ipv4ToInt(addr: string): number {
  return addr.split(".").reduce((n, octet) => ((n << 8) | Number(octet)) >>> 0, 0);
}

/** Loopback or the agent network minus its gateway → on-box; anything else → off-box. */
export function classifyPeer(remoteAddress: string | undefined): PeerClass {
  if (!remoteAddress) return "off-box";
  let addr = remoteAddress;
  if (addr.toLowerCase().startsWith("::ffff:") && isIP(addr.slice(7)) === 4) addr = addr.slice(7);
  if (addr === "::1") return "on-box";
  if (isIP(addr) !== 4) return "off-box";
  const n = ipv4ToInt(addr);
  if ((n >>> 24) === 127) return "on-box";
  if (addr === AGENT_NETWORK_GATEWAY) return "off-box";
  return ((n & SUBNET_MASK) >>> 0) === ipv4ToInt(SUBNET_BASE) ? "on-box" : "off-box";
}

export type Verdict =
  | { kind: "on"; source: string }
  | { kind: "off"; source: string }
  | { kind: "unverified"; reason: string };

export const REASON_UNREACHABLE = "backend not reachable yet";
export const REASON_NO_SECRET = "INTERNAL_API_SECRET is not set";

/** What `front()` needs; tests pass a stub, index.ts passes the real gate. */
export interface ExternalAccessSource {
  /** The verdict without waiting, when the cache is fresh; undefined when it must be fetched. */
  cached(): Verdict | undefined;
  verdict(): Promise<Verdict>;
}

export interface ExternalAccessGateOptions {
  trinityApiUrl: string;
  internalSecret: string;
  fetch?: (url: string, init: any) => Promise<any>;
  now?: () => number;
  /** How long a verdict answers without asking again. */
  ttlMs?: number;
  /** Extra attempts `prefetch()` makes after the first failure. */
  retries?: number;
  retryDelayMs?: number;
  /** Per-request timeout to the backend. */
  timeoutMs?: number;
  log?: (message: string) => void;
}

export const DEFAULT_TTL_MS = 10_000;

export class ExternalAccessGate implements ExternalAccessSource {
  private readonly url: string;
  private readonly secret: string;
  private readonly fetchImpl: (url: string, init: any) => Promise<any>;
  private readonly now: () => number;
  private readonly ttlMs: number;
  private readonly retries: number;
  private readonly retryDelayMs: number;
  private readonly timeoutMs: number;
  private readonly log: (message: string) => void;
  private last: { verdict: Verdict; at: number } | undefined;
  private inFlight: Promise<Verdict> | undefined;

  constructor(opts: ExternalAccessGateOptions) {
    this.url = `${opts.trinityApiUrl.replace(/\/+$/, "")}/api/internal/mcp-external-access`;
    this.secret = opts.internalSecret;
    this.fetchImpl = opts.fetch ?? ((url, init) => fetch(url, init));
    this.now = opts.now ?? Date.now;
    this.ttlMs = opts.ttlMs ?? DEFAULT_TTL_MS;
    this.retries = opts.retries ?? 5;
    this.retryDelayMs = opts.retryDelayMs ?? 1_000;
    this.timeoutMs = opts.timeoutMs ?? 3_000;
    this.log = opts.log ?? ((m) => console.warn(m));
  }

  cached(): Verdict | undefined {
    if (!this.secret) return { kind: "unverified", reason: REASON_NO_SECRET };
    if (!this.last || this.now() - this.last.at > this.ttlMs) return undefined;
    return this.last.verdict;
  }

  /** Fetch once, with bounded retries; resolves whatever happened (the gate reports unverified). */
  async prefetch(): Promise<void> {
    if (!this.secret) {
      this.log(`[ent#1101] ${REASON_NO_SECRET}: off-box MCP clients are refused as unverified until it is`);
      return;
    }
    for (let attempt = 0; attempt <= this.retries; attempt++) {
      if (await this.refresh()) return;
      if (attempt < this.retries && this.retryDelayMs > 0) await new Promise((r) => setTimeout(r, this.retryDelayMs));
    }
    this.log(`[ent#1101] external-access verdict not obtained from the backend after ${this.retries + 1} attempts; off-box MCP clients are refused as unverified until it answers`);
  }

  async verdict(): Promise<Verdict> {
    const fresh = this.cached();
    if (fresh) return fresh;
    if (!this.inFlight) {
      this.inFlight = this.refresh()
        .then(() => this.last?.verdict ?? { kind: "unverified" as const, reason: REASON_UNREACHABLE })
        .finally(() => (this.inFlight = undefined));
    }
    return this.inFlight;
  }

  /** One backend read. True when a verdict was stored; an error keeps the last one. */
  private async refresh(): Promise<boolean> {
    try {
      const signal = typeof AbortSignal !== "undefined" && "timeout" in AbortSignal ? AbortSignal.timeout(this.timeoutMs) : undefined;
      const res = await this.fetchImpl(this.url, { method: "GET", headers: { "X-Internal-Secret": this.secret }, signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      if (!body || typeof body.enabled !== "boolean") throw new Error("malformed body");
      const source = typeof body.source === "string" ? body.source : "unknown";
      this.last = { verdict: body.enabled ? { kind: "on", source } : { kind: "off", source }, at: this.now() };
      return true;
    } catch (error) {
      if (this.last) this.last.at = this.now(); // stale-while-error: re-ask after another TTL, not on every request
      else this.log(`[ent#1101] external-access read failed (${(error as Error).message}); ${REASON_UNREACHABLE}`);
      return false;
    }
  }
}

export const REFUSAL_OFF = "external_mcp_access_off";
export const REFUSAL_UNVERIFIED = "external_mcp_access_unverified";
export const JSONRPC_EXTERNAL_ACCESS_CODE = -32003;

export const OFF_MESSAGE =
  "external_mcp_access_off: External MCP access is off on this Trinity instance. " +
  "An admin can turn it on under Settings → MCP Keys → External MCP access.";

/** The refusal `front()` writes for an off-box `/mcp` request that may not pass. */
export function refusalFor(verdict: Verdict): { status: number; headers: Record<string, string>; body: string } | undefined {
  if (verdict.kind === "on") return undefined;
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  let status: number;
  let message: string;
  if (verdict.kind === "off") {
    status = 403;
    headers["X-Trinity-Refusal"] = REFUSAL_OFF;
    message = OFF_MESSAGE;
  } else {
    status = 503;
    headers["X-Trinity-Refusal"] = REFUSAL_UNVERIFIED;
    headers["Retry-After"] = "5";
    message = `${REFUSAL_UNVERIFIED}: External MCP access could not be verified (${verdict.reason}). Retry shortly.`;
  }
  const body = JSON.stringify({ jsonrpc: "2.0", error: { code: JSONRPC_EXTERNAL_ACCESS_CODE, message }, id: null });
  return { status, headers, body };
}
