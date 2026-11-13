/**
 * #4499 — `ask_trinity` sends an agent's free text to a vendor-hosted docs
 * service, so the operator can switch it off at run time. Off means the tool is
 * not registered: absent from every listing, impossible to call. The other docs
 * tool reads a local file and stays.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { askTrinityEnabled, createDocsTools } from "./docs.js";

const names = (env: NodeJS.ProcessEnv) =>
  Object.values(createDocsTools(env)).map((t) => (t as { name: string }).name).sort();

test("on by default", () => {
  assert.equal(askTrinityEnabled({}), true);
  assert.deepEqual(names({}), ["ask_trinity", "get_agent_requirements"]);
});

test("ASK_TRINITY_ENABLED=false leaves only the local docs tool", () => {
  for (const value of ["false", "False", " FALSE "]) {
    assert.equal(askTrinityEnabled({ ASK_TRINITY_ENABLED: value }), false, value);
    assert.deepEqual(names({ ASK_TRINITY_ENABLED: value }), ["get_agent_requirements"]);
  }
});

test("DO_NOT_TRACK turns it off for every value the backend treats as set (#1486)", () => {
  for (const value of ["1", "true", "TRUE", "yes", "on", "2", " 1 "]) {
    assert.equal(askTrinityEnabled({ DO_NOT_TRACK: value }), false, value);
    assert.deepEqual(names({ DO_NOT_TRACK: value }), ["get_agent_requirements"]);
  }
});

test("a DO_NOT_TRACK that allows leaves it on", () => {
  for (const value of ["0", "", "false", "FALSE", " 0 "]) {
    assert.equal(askTrinityEnabled({ DO_NOT_TRACK: value }), true, JSON.stringify(value));
  }
});

test("the switch cannot be overridden by the endpoint setting", () => {
  const env = { DO_NOT_TRACK: "1", ASK_TRINITY_ENDPOINT: "https://docs.internal.example/ask" };
  assert.deepEqual(names(env), ["get_agent_requirements"]);
});
