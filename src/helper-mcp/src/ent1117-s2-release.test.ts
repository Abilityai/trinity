/**
 * Release-time facts for the published trinity-docs-mcp package.
 *
 * 1. The server reports the version npm installed. The publish workflow
 *    writes the release version into package.json before it builds and runs
 *    these tests, so a hard-coded version string would report a stale number
 *    for every release after the one it was typed for.
 * 2. The committed manifest and lockfile stay at or above the first patched
 *    version of every dependency a Critical/High advisory was fixed for.
 *    Consumers resolve from the package.json ranges and this repo installs
 *    from the lockfile, so both are checked.
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { createServer, SERVER_VERSION } from "./server.js";

function readJson(relative: string): Record<string, unknown> {
  return JSON.parse(readFileSync(new URL(relative, import.meta.url), "utf8")) as Record<
    string,
    unknown
  >;
}

const pkg = readJson("../package.json") as {
  version: string;
  dependencies: Record<string, string>;
};
const lock = readJson("../package-lock.json") as {
  packages: Record<string, { version?: string }>;
};

function parseVersion(v: string): [number, number, number] {
  const m = /^(\d+)\.(\d+)\.(\d+)$/.exec(v);
  assert.ok(m, `not a plain x.y.z version: ${v}`);
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

function atLeast(v: string, floor: string): boolean {
  const a = parseVersion(v);
  const b = parseVersion(floor);
  for (let i = 0; i < 3; i++) {
    if (a[i] !== b[i]) return a[i] > b[i];
  }
  return true;
}

describe("release facts (ent1117-s2)", () => {
  test("serverInfo.version is the package.json version", async () => {
    assert.equal(SERVER_VERSION, pkg.version);
    const server = createServer();
    const client = new Client({ name: "ent1117-s2-release", version: "0.0.0" });
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    await Promise.all([client.connect(clientTransport), server.connect(serverTransport)]);
    assert.equal(client.getServerVersion()?.version, pkg.version);
  });

  // First patched versions, from the Dependabot alerts this package carried.
  const patched: Array<{ name: string; floor: string; direct: boolean }> = [
    // GHSA-46jr-4wx8-v453, GHSA-22jm-h49p-29qw (1.32.0), GHSA-6qxp-vccf-f47h, GHSA-f897-62hf-6gxq
    { name: "@modelcontextprotocol/sdk", floor: "1.32.0", direct: true },
    // GHSA-mrhx-c7xc-m3w5
    { name: "zod", floor: "4.6.6", direct: true },
    // GHSA-jqcg-44mw-7w3h (Critical)
    { name: "proxy-addr", floor: "2.0.8", direct: false },
    // GHSA-27r3-jxmh-p828
    { name: "body-parser", floor: "2.4.0", direct: false },
  ];

  for (const { name, floor, direct } of patched) {
    test(`${name} stays at or above ${floor} in the lockfile${direct ? " and the declared range" : ""}`, () => {
      const copies = Object.entries(lock.packages).filter(
        ([path]) => path === `node_modules/${name}` || path.endsWith(`/node_modules/${name}`),
      );
      assert.ok(copies.length > 0, `${name} is not in package-lock.json`);
      for (const [path, entry] of copies) {
        assert.ok(entry.version, `${path} has no version in package-lock.json`);
        assert.ok(atLeast(entry.version, floor), `${path} is ${entry.version}, below ${floor}`);
      }
      if (direct) {
        const range = pkg.dependencies[name];
        assert.ok(range, `${name} is not a declared dependency`);
        const m = /^\^(\d+\.\d+\.\d+)$/.exec(range);
        assert.ok(m, `${name} range is not a plain caret range: ${range}`);
        assert.ok(atLeast(m[1], floor), `${name} range ${range} admits versions below ${floor}`);
      }
    });
  }
});
