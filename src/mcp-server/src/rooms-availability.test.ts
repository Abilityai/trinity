/**
 * ent#443 — the room tools must not report a REFUSAL as an ABSENCE.
 *
 * Rooms moved into OSS core, so `403` can no longer mean "this build does not
 * have the module". Before the move the two really were indistinguishable and
 * a status-only test was correct; after it, the same test tells an agent that
 * rooms are switched off on the instance when what actually happened is that
 * it may not reach an agent, or is not a member of the room.
 *
 * The discriminator is the SHAPE of `detail` — the serving module authors
 * `{code, message}`, absence is a plain string — which is the rule the
 * Workspace already follows for the same distinction (#2128).
 *
 * Runner: built-in `node:test`. Run via:
 *   node --import tsx --test src/*.test.ts
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { unavailable } from "./tools/rooms.js";
import { ApiError } from "./client.js";

const coded = (status: number, code: string) =>
  new ApiError(status, JSON.stringify({ detail: { code, message: "nope" } }));

describe("ent#443 — refusal vs absence", () => {
  it("treats a coded 403 as a REFUSAL, not an absence", () => {
    // The rooms module's own "you cannot reach that agent".
    assert.equal(unavailable(coded(403, "agent_not_accessible")), false);
  });

  it("treats a coded 404 as a REFUSAL, not an absence", () => {
    // Membership-scoped uniform 404: the room exists, the caller is not in it.
    // Reporting this as "rooms are not enabled" is the same bug one status over.
    assert.equal(unavailable(coded(404, "room_not_found")), false);
  });

  it("treats an unmounted route (plain-string detail) as ABSENCE", () => {
    const e = new ApiError(404, JSON.stringify({ detail: "Not Found" }));
    assert.equal(unavailable(e), true);
  });

  it("treats an older build's entitlement 403 as ABSENCE", () => {
    const e = new ApiError(403, JSON.stringify({ detail: "Feature not entitled" }));
    assert.equal(unavailable(e), true);
  });

  it("falls back to absence for a body it cannot parse", () => {
    // Unrecognised shapes keep the friendly result as the default.
    assert.equal(unavailable(new ApiError(404, "<html>gateway</html>")), true);
  });

  it("does not classify an unrelated status as absence", () => {
    assert.equal(unavailable(coded(500, "boom")), false);
    assert.equal(unavailable(new ApiError(500, JSON.stringify({ detail: "kaboom" }))), false);
  });

  it("keeps the pre-ent#443 string test for a non-ApiError", () => {
    // A transport-layer error that never went through ApiError still degrades
    // the way it used to.
    assert.equal(unavailable(new Error("API error (404): Not Found")), true);
    assert.equal(unavailable(new Error("socket hang up")), false);
  });
});
