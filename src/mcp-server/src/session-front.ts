/**
 * #4060 — keyed MCP sessions survive an MCP server restart.
 *
 * mcp-proxy (bundled by fastmcp) keeps transport sessions in a closure-private
 * map, mints ids with `randomUUID`, and answers any unknown `Mcp-Session-Id`
 * with `404 Session not found`. That is spec-correct, but a widely used client
 * never re-initializes on it, so every redeploy disconnected every operator and
 * agent until a manual `/mcp`. Neither library offers a hook for either half.
 *
 * This module wraps the listener mcp-proxy passes to `http.createServer` —
 * same process, same port, no fork — and owns the session id the client sees:
 *
 *  - A KEYED `initialize` (exactly server.ts's predicate: `Bearer <key>`) gets
 *    an outer id signed with HMAC over (random, issued-at, SHA-256 of the key).
 *    The inner random id never leaves the process.
 *  - A request on a known outer id must carry the key it is bound to, on every
 *    verb. Today mcp-proxy re-authenticates each request and SWAPS the
 *    session's auth to whatever key it carries, so a second valid key on a live
 *    session ran with the first key's tool surface; bound sessions close that.
 *  - An outer id this process does not hold (restart, or the session was
 *    reaped while idle) is REVIVED when its signature verifies for the key
 *    presented: a synthetic `initialize` + `notifications/initialized` on
 *    loopback with that key builds a fresh inner session — through the normal
 *    `authenticate`, so a revoked key fails exactly as on a fresh session — and
 *    the original request is forwarded on it. The client never notices.
 *  - KEYLESS sessions (#848/#2035 anonymous sign-in, where the id is
 *    bearer-equivalent) are never signed and never revived: their ids pass
 *    through untouched and keep the strict 404.
 *
 * Fail-closed: no secret, or the listener cannot be captured (a library
 * upgrade moved `http.createServer`), means a warning and today's behaviour.
 * `session-front.guard.test.ts` fails the build when the capture point moves.
 */
import http, { type IncomingMessage, type ServerResponse } from "node:http";
import {
  createHash,
  createHmac,
  hkdfSync,
  randomBytes,
  timingSafeEqual,
} from "node:crypto";

type Listener = (req: IncomingMessage, res: ServerResponse) => void;

const HEADER = "mcp-session-id";
const ID_PREFIX = "t1";
const RAND_BYTES = 16;
const TAG_BYTES = 16;
const HKDF_INFO = "trinity-mcp-session-revival-v1";

/** A signed id may be revived this long after it was issued. */
export const REVIVE_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;
/** Tolerated clock skew for an issued-at in the future. */
const FUTURE_SKEW_MS = 60 * 1000;
/** Outer→inner mappings held; insertion-ordered, oldest evicted first. */
export const MAX_MAPPED_SESSIONS = 10_000;
/** Explicitly DELETEd ids, refused for revival within this process. */
const MAX_TOMBSTONES = 10_000;
/** Revivals in flight at once (a restart storm queues, it is not refused). */
export const MAX_CONCURRENT_REVIVALS = 32;
/** Revivals queued or in flight per key. */
export const MAX_REVIVALS_PER_KEY = 16;
/** Never synthesise this version: it routes to the stateless modern leg. */
const STATELESS_PROTOCOL = "2026-07-28";
const DEFAULT_PROTOCOL = "2025-03-26";

/**
 * The API key in an Authorization header, by EXACTLY the rule server.ts's
 * `authenticate` applies (`startsWith("Bearer ")`, `substring(7)`). Anything
 * else is the anonymous tier and must never be signed.
 */
export function bearerKey(header: string | string[] | undefined): string | undefined {
  const h = Array.isArray(header) ? header[0] : header;
  if (!h || !h.startsWith("Bearer ")) return undefined;
  return h.substring(7);
}

const keyFingerprint = (key: string): Buffer => createHash("sha256").update(key).digest();

/** Strict base64url: the decoded bytes must re-encode to the exact input. */
function decodeExact(part: string, bytes: number): Buffer | undefined {
  if (!/^[A-Za-z0-9_-]+$/.test(part)) return undefined;
  const buf = Buffer.from(part, "base64url");
  if (buf.length !== bytes || buf.toString("base64url") !== part) return undefined;
  return buf;
}

export interface SessionIdCodec {
  issue(key: string, nowMs: number): string;
  /** The issued-at (ms) when `id` is a genuine id issued to `key`, else undefined. */
  verify(id: string, key: string): number | undefined;
}

export function createSessionIdCodec(secret: string): SessionIdCodec {
  const macKey = Buffer.from(hkdfSync("sha256", secret, Buffer.alloc(0), HKDF_INFO, 32));
  const tag = (rand: Buffer, issued: Buffer, fp: Buffer) =>
    createHmac("sha256", macKey)
      .update(ID_PREFIX)
      .update(rand)
      .update(issued)
      .update(fp)
      .digest()
      .subarray(0, TAG_BYTES);
  return {
    issue(key, nowMs) {
      const rand = randomBytes(RAND_BYTES);
      const issued = Buffer.alloc(4);
      issued.writeUInt32BE(Math.floor(nowMs / 1000));
      const t = tag(rand, issued, keyFingerprint(key));
      return [ID_PREFIX, rand.toString("base64url"), issued.toString("base64url"), t.toString("base64url")].join(".");
    },
    verify(id, key) {
      const parts = id.split(".");
      if (parts.length !== 4 || parts[0] !== ID_PREFIX) return undefined;
      const rand = decodeExact(parts[1], RAND_BYTES);
      const issued = decodeExact(parts[2], 4);
      const given = decodeExact(parts[3], TAG_BYTES);
      if (!rand || !issued || !given) return undefined;
      const expected = tag(rand, issued, keyFingerprint(key));
      if (!timingSafeEqual(given, expected)) return undefined;
      return issued.readUInt32BE() * 1000;
    },
  };
}

/** First 8 characters of the random part — the only form an outer id is logged in. */
export const idPrefix = (id: string): string => (id.split(".")[1] ?? id).slice(0, 8);

/**
 * Synthetic revival `initialize` requests, as seen by `authenticate`, mapped to
 * the outer id's log prefix. server.ts audits a revival when it authenticates a
 * request found here, so the row carries the key identity it already resolved.
 */
export const revivalRequests = new WeakMap<IncomingMessage, string>();

function setRequestHeader(req: IncomingMessage, name: string, value: string | undefined): void {
  const raw = req.rawHeaders;
  const out: string[] = [];
  for (let i = 0; i < raw.length; i += 2) {
    if (raw[i].toLowerCase() !== name) out.push(raw[i], raw[i + 1]);
  }
  if (value !== undefined) out.push(name, value);
  // The bundled transport reads rawHeaders (hono adapter); mcp-proxy reads headers.
  req.rawHeaders = out;
  const headers = { ...req.headers };
  delete headers[name];
  if (value !== undefined) headers[name] = value;
  req.headers = headers;
}

function headerCount(req: IncomingMessage, name: string): number {
  let n = 0;
  for (let i = 0; i < req.rawHeaders.length; i += 2) if (req.rawHeaders[i].toLowerCase() === name) n++;
  return n;
}

const sessionNotFound = (res: ServerResponse) => {
  res.setHeader("Content-Type", "application/json");
  res.writeHead(404).end(JSON.stringify({ jsonrpc: "2.0", error: { code: -32001, message: "Session not found" }, id: null }));
};

interface Mapping {
  inner: string;
  fp: Buffer;
}

interface SyntheticResult {
  status: number;
  headers: http.IncomingHttpHeaders;
  body: string;
}

export interface SessionFrontOptions {
  secret: string;
  host: string;
  port: number;
  now?: () => number;
  maxMapped?: number;
}

export interface SessionFront {
  /** True when the listener was captured and a secret is set. */
  readonly active: boolean;
  /** FastMCP `disconnect`: the inner session is gone; its outer id revives on next use. */
  innerClosed(innerId: string | undefined): void;
  /** Outer ids currently mapped (tests). */
  readonly mappedCount: number;
}

/**
 * Run `start` (which must call `http.createServer` exactly once, as fastmcp's
 * httpStream start does through mcp-proxy) with the session front installed.
 */
export async function startWithSessionFront(
  start: () => Promise<void>,
  opts: SessionFrontOptions
): Promise<SessionFront> {
  const now = opts.now ?? Date.now;
  const maxMapped = opts.maxMapped ?? MAX_MAPPED_SESSIONS;
  const codec = opts.secret ? createSessionIdCodec(opts.secret) : undefined;
  const nonce = randomBytes(32).toString("hex");
  const nonceBuf = Buffer.from(nonce);
  const synthHost = ["", "0.0.0.0", "::", "[::]"].includes(opts.host) ? "127.0.0.1" : opts.host;

  const byOuter = new Map<string, Mapping>();
  const byInner = new Map<string, string>();
  const tombstones = new Set<string>();
  const inFlight = new Map<string, Promise<SyntheticResult | string>>();
  const perKey = new Map<string, number>();
  let running = 0;
  const waiters: Array<() => void> = [];

  let inner: Listener | undefined;
  const original = http.createServer;
  if (codec) {
    (http as any).createServer = (...args: unknown[]) => {
      (http as any).createServer = original;
      const listener = args.find((a) => typeof a === "function") as Listener | undefined;
      if (!listener) return (original as any).apply(http, args);
      inner = listener;
      const rest = args.filter((a) => a !== listener);
      return (original as any).call(http, ...rest, front);
    };
  }
  try {
    await start();
  } finally {
    (http as any).createServer = original;
  }

  if (!codec) {
    console.warn("[#4060] INTERNAL_API_SECRET unset: MCP sessions will not survive a restart");
  } else if (!inner) {
    console.warn("[#4060] could not wrap the MCP HTTP listener: MCP sessions will not survive a restart");
  }
  const active = Boolean(codec && inner);

  function remember(outer: string, innerId: string, fp: Buffer): void {
    const old = byOuter.get(outer);
    if (old) byInner.delete(old.inner);
    byOuter.delete(outer);
    byOuter.set(outer, { inner: innerId, fp });
    byInner.set(innerId, outer);
    while (byOuter.size > maxMapped) {
      const [oldest, m] = byOuter.entries().next().value as [string, Mapping];
      byOuter.delete(oldest);
      byInner.delete(m.inner);
    }
  }

  function forget(outer: string): void {
    const m = byOuter.get(outer);
    if (!m) return;
    byOuter.delete(outer);
    byInner.delete(m.inner);
  }

  function tombstone(outer: string): void {
    tombstones.add(outer);
    if (tombstones.size > MAX_TOMBSTONES) tombstones.delete(tombstones.values().next().value as string);
  }

  async function slot<T>(fn: () => Promise<T>): Promise<T> {
    if (running >= MAX_CONCURRENT_REVIVALS) await new Promise<void>((r) => waiters.push(r));
    running++;
    try {
      return await fn();
    } finally {
      running--;
      waiters.shift()?.();
    }
  }

  function synthetic(authorization: string, body: unknown, sessionId?: string, protocol?: string, outerPrefix?: string): Promise<SyntheticResult> {
    return new Promise((resolve, reject) => {
      const headers: Record<string, string> = {
        "content-type": "application/json",
        accept: "application/json, text/event-stream",
        authorization,
        "x-trinity-session-front": nonce,
      };
      if (sessionId) headers[HEADER] = sessionId;
      if (protocol) headers["mcp-protocol-version"] = protocol;
      if (outerPrefix) headers["x-trinity-revival-of"] = outerPrefix;
      const req = http.request(
        { host: synthHost, port: opts.port, path: "/mcp", method: "POST", headers, agent: false },
        (res) => {
          let data = "";
          res.setEncoding("utf8");
          res.on("data", (c) => (data += c));
          res.on("end", () => resolve({ status: res.statusCode ?? 0, headers: res.headers, body: data }));
        }
      );
      req.on("error", reject);
      req.end(JSON.stringify(body));
    });
  }

  /** A fresh inner session for `outer`, or the failed initialize to relay. */
  function revive(outer: string, authorization: string, fp: Buffer, protocol: string): Promise<SyntheticResult | string> {
    const pending = inFlight.get(outer);
    if (pending) return pending;
    const fpKey = fp.toString("hex");
    if ((perKey.get(fpKey) ?? 0) >= MAX_REVIVALS_PER_KEY) {
      return Promise.resolve({ status: 503, headers: { "retry-after": "1" }, body: "" });
    }
    perKey.set(fpKey, (perKey.get(fpKey) ?? 0) + 1);
    const p = slot(async () => {
      const init = await synthetic(
        authorization,
        {
          jsonrpc: "2.0",
          id: "trinity-session-revival",
          method: "initialize",
          params: { protocolVersion: protocol, capabilities: {}, clientInfo: { name: "trinity-session-revival", version: "1" } },
        },
        undefined,
        protocol,
        idPrefix(outer)
      );
      const innerId = init.headers[HEADER];
      if (init.status !== 200 || typeof innerId !== "string") return init;
      await synthetic(authorization, { jsonrpc: "2.0", method: "notifications/initialized" }, innerId, protocol);
      remember(outer, innerId, fp);
      console.log(`[#4060] revived MCP session ${idPrefix(outer)}…`);
      return innerId;
    }).finally(() => {
      inFlight.delete(outer);
      const left = (perKey.get(fpKey) ?? 1) - 1;
      if (left > 0) perKey.set(fpKey, left);
      else perKey.delete(fpKey);
    });
    inFlight.set(outer, p);
    return p;
  }

  /** Rewrite the inner id on the way out; mint the outer id for a keyed initialize. */
  function patchResponse(res: ServerResponse, outer: string | undefined, key: string | undefined, fp: Buffer | undefined, method: string | undefined): void {
    const writeHead = res.writeHead;
    (res as any).writeHead = function (status: number, ...rest: any[]) {
      const headerObj = rest.find((o) => o && typeof o === "object" && !Array.isArray(o));
      const fromObj = headerObj ? Object.keys(headerObj).find((k) => k.toLowerCase() === HEADER) : undefined;
      const innerId = (res.getHeader(HEADER) as string | undefined) ?? (fromObj ? headerObj[fromObj] : undefined);
      let replacement: string | undefined;
      if (typeof innerId === "string") {
        replacement = byInner.get(innerId);
        if (!replacement && key && fp && !outer && status >= 200 && status < 300) {
          replacement = codec!.issue(key, now());
          remember(replacement, innerId, fp);
        }
      }
      if (typeof innerId === "string" && (replacement || key)) {
        // Keyed traffic never sees an inner id; an unmapped one is stripped.
        if (res.getHeader(HEADER) !== undefined) {
          if (replacement) res.setHeader(HEADER, replacement);
          else res.removeHeader(HEADER);
        }
        if (fromObj) {
          if (replacement) headerObj[fromObj] = replacement;
          else delete headerObj[fromObj];
        }
      }
      if (outer && status === 404) forget(outer); // inner reaped in a race: revive next time
      if (outer && method === "DELETE" && status >= 200 && status < 300) {
        forget(outer);
        tombstone(outer);
      }
      return (writeHead as any).call(this, status, ...rest);
    };
  }

  function front(req: IncomingMessage, res: ServerResponse): void {
    const marker = req.headers["x-trinity-session-front"];
    if (typeof marker === "string" && marker.length === nonce.length && timingSafeEqual(Buffer.from(marker), nonceBuf)) {
      const revivalOf = req.headers["x-trinity-revival-of"];
      setRequestHeader(req, "x-trinity-session-front", undefined);
      setRequestHeader(req, "x-trinity-revival-of", undefined);
      if (typeof revivalOf === "string") revivalRequests.set(req, revivalOf);
      return inner!(req, res);
    }
    // A forged marker is just another header to strip.
    setRequestHeader(req, "x-trinity-session-front", undefined);
    setRequestHeader(req, "x-trinity-revival-of", undefined);

    const count = headerCount(req, HEADER);
    if (count > 1) {
      res.setHeader("Content-Type", "application/json");
      res.writeHead(400).end(JSON.stringify({ jsonrpc: "2.0", error: { code: -32000, message: "Bad Request: multiple Mcp-Session-Id headers" }, id: null }));
      return;
    }
    const sessionId = count === 1 ? (req.headers[HEADER] as string) : undefined;
    const authorization = req.headers.authorization as string | undefined;
    const key = bearerKey(authorization);
    const fp = key !== undefined ? keyFingerprint(key) : undefined;

    if (!sessionId) {
      patchResponse(res, undefined, key, fp, req.method);
      return inner!(req, res);
    }

    const mapped = byOuter.get(sessionId);
    if (mapped) {
      if (!fp || !timingSafeEqual(fp, mapped.fp)) return sessionNotFound(res);
      byOuter.delete(sessionId); // refresh recency
      byOuter.set(sessionId, mapped);
      setRequestHeader(req, HEADER, mapped.inner);
      patchResponse(res, sessionId, key, fp, req.method);
      return inner!(req, res);
    }

    if (key === undefined) {
      // Keyless: a raw (anonymous) id passes through untouched, but never an
      // inner id that belongs to a keyed session.
      if (byInner.has(sessionId)) return sessionNotFound(res);
      return inner!(req, res);
    }

    // Keyed, unknown to this process: revive when the id is genuinely ours and this key's.
    if (byInner.has(sessionId)) return sessionNotFound(res); // an inner id is never presented directly
    const issuedAt = codec!.verify(sessionId, key);
    const t = now();
    if (issuedAt === undefined || tombstones.has(sessionId) || t - issuedAt > REVIVE_MAX_AGE_MS || issuedAt - t > FUTURE_SKEW_MS) {
      return inner!(req, res); // not ours: mcp-proxy answers 404 as it always has
    }
    const requested = req.headers["mcp-protocol-version"];
    const protocol = typeof requested === "string" && requested && requested !== STATELESS_PROTOCOL ? requested : DEFAULT_PROTOCOL;

    req.pause();
    revive(sessionId, authorization!, fp!, protocol).then(
      (result) => {
        if (typeof result === "string") {
          setRequestHeader(req, HEADER, result);
          patchResponse(res, sessionId, key, fp, req.method);
          req.resume();
          return inner!(req, res);
        }
        // The revival initialize was refused (revoked key, backend down, cap):
        // relay it as a fresh session's initialize would have been answered.
        req.resume();
        req.on("data", () => {});
        const headers: Record<string, string> = {};
        for (const [k, v] of Object.entries(result.headers)) {
          if (typeof v === "string" && ["content-type", "www-authenticate", "retry-after"].includes(k)) headers[k] = v;
        }
        if (result.status === 200) return sessionNotFound(res);
        res.writeHead(result.status, headers).end(result.body);
      },
      (error) => {
        console.error(`[#4060] revival of ${idPrefix(sessionId)}… failed:`, error);
        req.resume();
        sessionNotFound(res);
      }
    );
  }

  return {
    active,
    innerClosed(innerId) {
      if (!innerId) return;
      const outer = byInner.get(innerId);
      if (outer) forget(outer);
    },
    get mappedCount() {
      return byOuter.size;
    },
  };
}
