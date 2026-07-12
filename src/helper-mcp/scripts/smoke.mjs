#!/usr/bin/env node
/**
 * Pack-and-run stdio smoke test.
 *
 * Catches what mocked-fetch unit tests structurally cannot:
 *   - bin/shebang/ESM resolution of the BUILT dist/ from the PACKED tarball
 *   - `files` whitelist omissions (a missing dist/ file only fails here)
 *   - stdout purity (every stdout line must parse as JSON-RPC)
 *
 * Flow: npm pack → extract to temp dir → npm install --omit=dev → spawn the
 * bin against a local mock endpoint → initialize / tools-list / ask_trinity.
 * Exit 0 on success, 1 with a reason on any failure.
 */
import { execFileSync, spawn } from "node:child_process";
import { createServer } from "node:http";
import { mkdtempSync, rmSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const pkgRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function fail(reason) {
  console.error(`SMOKE FAIL: ${reason}`);
  process.exit(1);
}

// 1. Mock ask-trinity endpoint
const mock = createServer((req, res) => {
  let body = "";
  req.on("data", (c) => (body += c));
  req.on("end", () => {
    let question = "";
    try {
      question = JSON.parse(body).question ?? "";
    } catch {}
    res.setHeader("Content-Type", "application/json");
    res.end(
      JSON.stringify({
        answer: `Mock answer to: ${question}`,
        state: "SUCCEEDED",
        session_id: "3056475319750407883",
      }),
    );
  });
});
await new Promise((r) => mock.listen(0, "127.0.0.1", r));
const endpoint = `http://127.0.0.1:${mock.address().port}/ask-trinity`;

// 2. Pack and extract
const workDir = mkdtempSync(join(tmpdir(), "trinity-docs-mcp-smoke-"));
let child;
try {
  const tarball = execFileSync("npm", ["pack", "--pack-destination", workDir], {
    cwd: pkgRoot,
    encoding: "utf8",
  })
    .trim()
    .split("\n")
    .pop();
  execFileSync("tar", ["-xzf", join(workDir, tarball), "-C", workDir]);
  const extracted = join(workDir, "package");
  if (!readdirSync(join(extracted, "dist")).includes("index.js")) {
    fail("packed tarball is missing dist/index.js — check the files whitelist and run npm run build first");
  }
  execFileSync("npm", ["install", "--omit=dev", "--no-audit", "--no-fund"], {
    cwd: extracted,
    stdio: ["ignore", "ignore", "inherit"],
  });

  // 3. Spawn the packed bin over stdio
  child = spawn("node", [join(extracted, "bin", "trinity-docs-mcp.js")], {
    cwd: extracted,
    env: { ...process.env, ASK_TRINITY_ENDPOINT: endpoint },
    stdio: ["pipe", "pipe", "inherit"],
  });

  const pending = new Map();
  let stdoutBuf = "";
  child.stdout.on("data", (chunk) => {
    stdoutBuf += chunk.toString();
    let nl;
    while ((nl = stdoutBuf.indexOf("\n")) >= 0) {
      const line = stdoutBuf.slice(0, nl).trim();
      stdoutBuf = stdoutBuf.slice(nl + 1);
      if (!line) continue;
      let msg;
      try {
        msg = JSON.parse(line);
      } catch {
        fail(`stdout purity violated — non-JSON line on the RPC channel: ${line.slice(0, 120)}`);
      }
      if (msg.id !== undefined && pending.has(msg.id)) {
        pending.get(msg.id)(msg);
        pending.delete(msg.id);
      }
    }
  });

  function request(id, method, params) {
    return new Promise((resolvePromise, rejectPromise) => {
      const timer = setTimeout(
        () => rejectPromise(new Error(`timeout waiting for response to ${method}`)),
        15_000,
      );
      pending.set(id, (msg) => {
        clearTimeout(timer);
        resolvePromise(msg);
      });
      child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    });
  }

  const init = await request(1, "initialize", {
    protocolVersion: "2025-03-26",
    capabilities: {},
    clientInfo: { name: "smoke", version: "0.0.0" },
  });
  if (!init.result?.serverInfo?.name) fail("initialize returned no serverInfo");
  child.stdin.write(
    JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n",
  );

  const list = await request(2, "tools/list", {});
  const names = (list.result?.tools ?? []).map((t) => t.name).sort();
  if (JSON.stringify(names) !== JSON.stringify(["ask_trinity", "get_agent_requirements"])) {
    fail(`unexpected tool list: ${JSON.stringify(names)}`);
  }

  const call = await request(3, "tools/call", {
    name: "ask_trinity",
    arguments: { question: "smoke check?" },
  });
  const text = call.result?.content?.[0]?.text ?? "";
  if (!text.includes("Mock answer to: smoke check?")) {
    fail(`ask_trinity did not return the mock answer: ${text.slice(0, 200)}`);
  }
  if (!text.includes("session_id: 3056475319750407883")) {
    fail(">2^53 session_id did not survive as an exact string");
  }

  console.error("SMOKE OK: pack → install → stdio initialize/list/call all green");
} catch (err) {
  fail(err?.message ?? String(err));
} finally {
  child?.kill();
  mock.close();
  rmSync(workDir, { recursive: true, force: true });
}
process.exit(0);
