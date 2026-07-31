import { test, expect } from '@playwright/test'

/**
 * Install a system from a manifest (trinity-enterprise#126).
 *
 * Drives the real surface against a live stack: the ?tab= catalog on /templates,
 * paste -> preview -> the agents/topology/schedules tables, and the two transport
 * traps that a naive store gets wrong.
 *
 * Nothing here deploys for real by default. A deploy creates containers, and
 * there is no un-deploy — re-running the same manifest produces `_N`-suffixed
 * duplicates rather than converging, so an automated deploy would litter the
 * stack it runs on. The one deploy assertion is opt-in via
 * SYSTEM_INSTALL_DEPLOY=1 and uses a manifest that CANNOT create anything
 * (a nonexistent local: template), which exercises the 500-with-a-body path
 * without side effects.
 */

const VALID_MANIFEST = `name: e2e-preview-only
description: A preview-only fixture
agents:
  alpha:
    template: local:default
    resources:
      cpu: "1"
      memory: "2g"
    schedules:
      - name: nightly
        cron: "0 3 * * *"
        message: "/run"
  beta:
    template: local:default
permissions:
  preset: full-mesh
`

// Parses as YAML but fails validation: an uppercase/underscore system name.
const INVALID_NAME_MANIFEST = `name: E2E_Bad_Name
agents:
  alpha:
    template: local:default
`

// Valid shape, unresolvable template. #1793/#1759 made an unknown `local:` id a
// hard per-agent 404, so the preview reports blockers and a real deploy creates
// nothing -> status "failed" at HTTP 500 with the report as the body.
const UNRESOLVABLE_MANIFEST = `name: e2e-unresolvable
agents:
  ghost:
    template: local:definitely-not-a-real-template-ent126
`

async function openSystemsTab (page) {
  await page.goto('/templates?tab=systems')
  await expect(page.getByTestId('tab-systems')).toBeVisible()
  await expect(page.getByTestId('manifest-textarea')).toBeVisible()
}

async function pasteManifest (page, yaml) {
  await page.getByTestId('source-paste').click()
  await page.getByTestId('manifest-textarea').fill(yaml)
}

test.describe('System install surface', () => {
  test('tab deep-links and exposes both catalog sections', async ({ page }) => {
    await page.goto('/templates')
    // Default tab is the pre-existing agent-template catalog.
    await expect(page.getByTestId('tab-agents')).toBeVisible()
    await expect(page.getByTestId('tab-systems')).toBeVisible()

    // Switching writes ?tab= so the surface is linkable and survives a reload.
    await page.getByTestId('tab-systems').click()
    await expect(page).toHaveURL(/[?&]tab=systems/)
    await expect(page.getByTestId('manifest-textarea')).toBeVisible()

    await page.reload()
    await expect(page.getByTestId('manifest-textarea')).toBeVisible()
  })

  test('bundled manifests are offered and load into the editor', async ({ page }) => {
    await openSystemsTab(page)
    // `default-system.yaml` ships in-repo and is mounted read-only, so at least
    // one card is expected on any standard stack.
    const load = page.getByTestId('bundled-load-default-system')
    await expect(load).toBeVisible()
    await load.click()
    await expect(page.getByTestId('manifest-textarea')).toHaveValue(/name:\s*acme/)
  })

  test('preview shows agents, permission topology and schedules', async ({ page }) => {
    await openSystemsTab(page)
    await pasteManifest(page, VALID_MANIFEST)
    await page.getByTestId('dry-run').click()

    await expect(page.getByText('No blockers found')).toBeVisible()

    // Resolved agent names (AC #2) — from the backend, not a client-side parse.
    // Scoped to the agents section: a full-mesh manifest with schedules renders
    // each name in four places (agents table, both topology columns, schedules
    // table), so an unscoped getByText is a strict-mode violation. Scoping also
    // makes this assert the name is in the AGENTS table specifically, rather than
    // `.first()`, which would pass on whichever element happened to come first.
    const agentsSection = page.locator('section').filter({
      has: page.getByRole('heading', { name: /Agents to create/ }),
    })
    await expect(agentsSection.getByText('e2e-preview-only-alpha')).toBeVisible()
    await expect(agentsSection.getByText('e2e-preview-only-beta')).toBeVisible()

    // full-mesh topology.
    await expect(page.getByRole('heading', { name: /Permissions/ })).toBeVisible()
    await expect(page.getByText('2 grants')).toBeVisible()

    // Schedules, and the fact that they run on their own.
    await expect(page.getByText('nightly')).toBeVisible()
    await expect(page.getByText('0 3 * * *')).toBeVisible()
    await expect(page.getByText('runs automatically')).toBeVisible()
  })

  test('a manifest with schedules gates Deploy behind an acknowledgement', async ({ page }) => {
    await openSystemsTab(page)
    await pasteManifest(page, VALID_MANIFEST)
    await page.getByTestId('dry-run').click()
    await expect(page.getByText('No blockers found')).toBeVisible()

    // Enabled schedules start autonomous, budget-spending executions, so consent
    // is explicit rather than a banner.
    const deploy = page.getByTestId('deploy')
    await expect(deploy).toBeDisabled()
    await page.getByTestId('ack-checkbox').check()
    await expect(deploy).toBeEnabled()
  })

  test('the acknowledgement does not survive a manifest edit', async ({ page }) => {
    // Consent is per-manifest. Without the reset, a user could tick the box for
    // manifest A's schedules, edit the YAML, re-preview, and deploy manifest B's
    // consequences under A's consent — the deploy-what-you-didn't-look-at failure
    // the preview binding exists to prevent, one field over.
    await openSystemsTab(page)
    await pasteManifest(page, VALID_MANIFEST)
    await page.getByTestId('dry-run').click()
    await expect(page.getByText('No blockers found')).toBeVisible()
    await page.getByTestId('ack-checkbox').check()
    await expect(page.getByTestId('deploy')).toBeEnabled()

    // Same hazard shape (still has an enabled schedule), different manifest.
    await page.getByTestId('manifest-textarea').fill(
      VALID_MANIFEST.replace('e2e-preview-only', 'e2e-preview-edited')
    )
    await page.getByTestId('dry-run').click()
    await expect(page.getByText('No blockers found')).toBeVisible()

    // The box must be clear again, and Deploy must be blocked until it is re-ticked.
    await expect(page.getByTestId('ack-checkbox')).not.toBeChecked()
    await expect(page.getByTestId('deploy')).toBeDisabled()
  })

  test('editing after a preview disables Deploy until re-previewed', async ({ page }) => {
    await openSystemsTab(page)
    // No schedules and no prompt -> no acknowledgement needed, so Deploy's state
    // isolates the preview-binding behaviour.
    await pasteManifest(page, `name: e2e-binding
agents:
  alpha:
    template: local:default
`)
    await page.getByTestId('dry-run').click()
    await expect(page.getByText('No blockers found')).toBeVisible()
    await expect(page.getByTestId('deploy')).toBeEnabled()

    // Editing invalidates the preview: otherwise a user deploys B while reading
    // A's preview.
    await page.getByTestId('manifest-textarea').fill(`name: e2e-binding-edited
agents:
  alpha:
    template: local:default
`)
    await expect(page.getByTestId('deploy')).toBeDisabled()
    await expect(page.getByText(/preview again before deploying/i)).toBeVisible()
  })

  test('a validation error renders as a named message, not a raw blob', async ({ page }) => {
    // AC #4. The backend returns 400 with {detail: "<string>"}; the store must
    // surface that string rather than "[object Object]" or a stack.
    await openSystemsTab(page)
    await pasteManifest(page, INVALID_NAME_MANIFEST)
    await page.getByTestId('dry-run').click()

    const error = page.getByTestId('install-error')
    await expect(error).toBeVisible()
    await expect(error).toContainText('Invalid system name')
    await expect(error).not.toContainText('[object Object]')
    await expect(error).not.toContainText('Traceback')
  })

  test('malformed YAML renders as a named message', async ({ page }) => {
    await openSystemsTab(page)
    await pasteManifest(page, 'name: [broken\n  ::::\n')
    await page.getByTestId('dry-run').click()
    await expect(page.getByTestId('install-error')).toContainText(/YAML|parse/i)
  })

  test('an unresolvable template is a preview blocker at HTTP 200', async ({ page }) => {
    // Trap (a): `status: "invalid"` arrives with a 200, so a store that switched
    // on the HTTP code would render this as a clean, deployable preview.
    await openSystemsTab(page)
    await pasteManifest(page, UNRESOLVABLE_MANIFEST)
    await page.getByTestId('dry-run').click()

    await expect(page.getByText('This manifest cannot deploy yet')).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Blockers' })).toBeVisible()
    await expect(page.getByTestId('deploy')).toBeDisabled()
  })

  test('a manifest with a bad cpu is blocked before deploy', async ({ page }) => {
    // The regression that motivated the resource preflight: this previewed clean
    // and then failed 100% of its agents at create.
    await openSystemsTab(page)
    await pasteManifest(page, `name: e2e-badcpu
agents:
  alpha:
    template: local:default
    resources:
      cpu: 1.0
`)
    await page.getByTestId('dry-run').click()
    await expect(page.getByText('This manifest cannot deploy yet')).toBeVisible()
    await expect(page.getByText(/Invalid cpu/)).toBeVisible()
  })
})

test.describe('System install — deploy path', () => {
  // Opt-in: see the file header. Even this manifest creates nothing, but keeping
  // every deploy behind one flag means the suite can never litter a stack.
  test.skip(
    process.env.SYSTEM_INSTALL_DEPLOY !== '1',
    'set SYSTEM_INSTALL_DEPLOY=1 to exercise the real deploy path'
  )

  test('a total failure renders the report, not a generic error', async ({ page }) => {
    // Trap (b): `status: "failed"` arrives as HTTP 500 with the full report AS
    // THE BODY. A naive axios catch discards exactly the failed[] list AC #3
    // requires, so this asserts the per-agent failure is rendered.
    await openSystemsTab(page)
    await pasteManifest(page, UNRESOLVABLE_MANIFEST)
    await page.getByTestId('dry-run').click()
    await expect(page.getByText('This manifest cannot deploy yet')).toBeVisible()

    // Deploy is intentionally blocked by the preview, so drive the store the way
    // a user who ignored the preview cannot — via the API the button would call.
    await page.evaluate(async (manifest) => {
      const token = localStorage.getItem('token')
      window.__ent126 = await fetch('/api/systems/deploy', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ manifest, dry_run: false }),
      }).then(async (r) => ({ status: r.status, body: await r.json() }))
    }, UNRESOLVABLE_MANIFEST)

    const outcome = await page.evaluate(() => window.__ent126)
    // The contract the store depends on: a 500 whose body is the full report.
    expect(outcome.status).toBe(500)
    expect(outcome.body.status).toBe('failed')
    expect(outcome.body.agents_created).toEqual([])
    expect(outcome.body.failed.length).toBeGreaterThan(0)
    expect(outcome.body.failed[0].reason).toBeTruthy()
  })
})

/**
 * AC #5 — the post-deploy "View this fleet" link lands the Dashboard filtered to
 * the new system's tag.
 *
 * The trap is a PERSISTED view selection. `systemViewsStore.initialize()` restores
 * `trinity-active-view` from localStorage on mount, BEFORE the deep link is read,
 * so an early return that deferred to an active view made the link a silent no-op
 * for anyone who had ever selected a view — the failure is invisible on a fresh
 * profile and permanent on a real one. An explicit `?tags=` must win, exactly as
 * clicking a tag chip does (`toggleQuickTag` clears the selection for the same
 * reason). Asserted on localStorage rather than on tiles so it holds on a stack
 * with no agents.
 */
test.describe('Dashboard system deep link', () => {
  const VIEW_KEY = 'trinity-active-view'
  const TAGS_KEY = 'trinity-dashboard-quick-tags'

  async function seedPersistedView (page) {
    // Must be on the app origin before localStorage is reachable.
    await page.goto('/')
    await page.evaluate(([viewKey, tagsKey]) => {
      localStorage.setItem(viewKey, 'a-view-from-a-previous-session')
      localStorage.removeItem(tagsKey)
    }, [VIEW_KEY, TAGS_KEY])
  }

  test('?tags= wins over a view selection restored from localStorage', async ({ page }) => {
    await seedPersistedView(page)
    await page.goto('/?tags=e2e-deep-link')

    // The stale selection is cleared rather than deferred to...
    await expect
      .poll(() => page.evaluate(k => localStorage.getItem(k), VIEW_KEY))
      .toBeNull()
    // ...and the link's tags are the filter that actually took effect.
    await expect
      .poll(() => page.evaluate(k => localStorage.getItem(k), TAGS_KEY))
      .toBe(JSON.stringify(['e2e-deep-link']))
  })

  test('a restored view selection survives when there is no ?tags=', async ({ page }) => {
    // The control. Without it the test above is also satisfied by clearing the
    // selection unconditionally on every Dashboard mount, which would break the
    // view sidebar's own persistence.
    await seedPersistedView(page)
    await page.goto('/')

    await expect
      .poll(() => page.evaluate(k => localStorage.getItem(k), VIEW_KEY))
      .toBe('a-view-from-a-previous-session')
  })
})
