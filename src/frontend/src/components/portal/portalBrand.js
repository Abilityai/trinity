/**
 * The Workspace's brand facts (ent#556).
 *
 * A plain module, not a constant inside the SFC, for the reason the rest of
 * this directory splits the same way: `vitest` runs `environment: 'node'` with
 * no component-mount harness, so anything a test must read has to be importable
 * without mounting.
 */

/**
 * What the Workspace calls itself, in both places that must agree.
 *
 * Deliberately the PRODUCT name and not an instance name. The platform does
 * resolve a per-instance label (`services/instance_identity.py`,
 * `TRINITY_INSTANCE_NAME`), and a company running Trinity could plausibly want
 * its own name in this corner — but this surface's audience is the operator's
 * own organisation (ent#78, ruled 2026-09-06) and naming the product is the
 * point of the ask. Instance naming and white-labelling are a separate, later
 * call; one constant with one reader is what keeps that call cheap.
 */
export const WORKSPACE_BRAND_NAME = 'Trinity Workspace'
