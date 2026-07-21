import { test, expect } from '@playwright/test'

/**
 * Dashboard responsive chrome regression (#1754).
 *
 * The global navigation and Replay Timeline toolbar are independent flex
 * surfaces, but both previously exposed their full desktop content around
 * 700px. Keep geometry assertions here so future labels, badges, or build
 * metadata cannot silently reintroduce page-level overflow.
 */

const VIEWPORT_WIDTHS = [375, 639, 640, 700, 714, 768, 1024, 1100, 1101, 1279, 1280, 1440]

async function selectTimeline(page) {
  const timelineButton = page.getByRole('button', { name: 'timeline', exact: true })
  await expect(timelineButton).toBeVisible({ timeout: 15000 })
  await timelineButton.click()
  await expect(page.getByTestId('replay-timeline')).toBeVisible()
}

async function responsiveGeometry(page) {
  return page.evaluate(() => {
    const isVisible = (element) => {
      if (!element) return false
      const rect = element.getBoundingClientRect()
      return getComputedStyle(element).display !== 'none' && rect.width > 0 && rect.height > 0
    }

    const overlaps = (first, second) => {
      if (!isVisible(first) || !isVisible(second)) return false
      const a = first.getBoundingClientRect()
      const b = second.getBoundingClientRect()
      return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top
    }

    const nav = document.querySelector('[data-testid="global-navigation"]')
    const desktopNav = document.querySelector('[data-testid="desktop-navigation"]')
    const desktopStatus = document.querySelector('[data-testid="desktop-connection-status"]')
    const compactTrigger = document.querySelector('[data-testid="compact-navigation-trigger"]')
    const settings = desktopNav?.querySelector('a[href="/settings"]')
    const toolbar = document.querySelector('[data-testid="replay-timeline-toolbar"]')
    const legend = document.querySelector('[data-testid="replay-timeline-legend"]')
    const activeOnly = Array.from(toolbar?.querySelectorAll('label') || [])
      .find((element) => element.textContent?.includes('Active only'))
    const agentTriggered = Array.from(legend?.querySelectorAll('span') || [])
      .find((element) => element.textContent?.trim() === 'Agent-Triggered')
    const timelineScroll = document.querySelector('.timeline-scroll-container')

    return {
      documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      headerBorderWidth: parseFloat(getComputedStyle(nav).borderBottomWidth),
      desktopNavVisible: isVisible(desktopNav),
      compactTriggerVisible: isVisible(compactTrigger),
      settingsStatusOverlap: overlaps(settings, desktopStatus),
      toolbarOverflow: toolbar.scrollWidth - toolbar.clientWidth,
      activeOnlyHeight: activeOnly?.getBoundingClientRect().height || 0,
      legendVisible: isVisible(legend),
      agentTriggeredHeight: agentTriggered?.getBoundingClientRect().height || 0,
      timelineHasInternalOverflow: timelineScroll.scrollWidth > timelineScroll.clientWidth,
    }
  })
}

test.describe('Dashboard responsive chrome (#1754)', () => {
  test('@smoke reflows without overlap across breakpoint boundaries', async ({ page }) => {
    await page.setViewportSize({ width: 714, height: 900 })
    await page.goto('/')
    await selectTimeline(page)

    for (const width of VIEWPORT_WIDTHS) {
      await page.setViewportSize({ width, height: 900 })
      const geometry = await responsiveGeometry(page)

      expect(geometry.documentOverflow, `document overflow at ${width}px`).toBeLessThanOrEqual(1)
      expect(geometry.headerBorderWidth, `header divider at ${width}px`).toBeGreaterThan(0)
      expect(geometry.settingsStatusOverlap, `Settings/status overlap at ${width}px`).toBe(false)
      expect(geometry.toolbarOverflow, `timeline toolbar overflow at ${width}px`).toBeLessThanOrEqual(1)
      expect(geometry.activeOnlyHeight, `Active only wraps at ${width}px`).toBeLessThanOrEqual(20)
      expect(geometry.timelineHasInternalOverflow, `timeline canvas overflow at ${width}px`).toBe(true)

      if (width < 1280) {
        expect(geometry.compactTriggerVisible, `compact navigation at ${width}px`).toBe(true)
        expect(geometry.desktopNavVisible, `desktop navigation at ${width}px`).toBe(false)
      } else {
        expect(geometry.compactTriggerVisible, `compact navigation at ${width}px`).toBe(false)
        expect(geometry.desktopNavVisible, `desktop navigation at ${width}px`).toBe(true)
      }

      if (geometry.legendVisible) {
        expect(geometry.agentTriggeredHeight, `Agent-Triggered wraps at ${width}px`).toBeLessThanOrEqual(20)
      }
    }
  })

  test('@smoke compact navigation keeps routes and runtime state reachable', async ({ page }) => {
    await page.setViewportSize({ width: 714, height: 900 })
    await page.goto('/')

    const trigger = page.getByTestId('compact-navigation-trigger')
    await expect(trigger).toBeVisible({ timeout: 15000 })
    await trigger.click()

    const panel = page.getByTestId('compact-navigation-panel')
    await expect(panel).toBeVisible()
    await expect(panel.locator('a[href="/"]')).toBeVisible()
    await expect(panel.locator('a[href="/agents"]')).toBeVisible()
    await expect(panel.locator('a[href="/templates"]')).toBeVisible()
    await expect(panel.locator('a[href="/operations"]')).toBeVisible()

    const settings = panel.locator('a[href="/settings"]')
    await expect(settings).toBeVisible()
    await expect(panel.locator('[role="status"]')).toContainText(/Connected|Disconnected/)

    const noDocumentOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1
    )
    expect(noDocumentOverflow).toBe(true)

    await settings.focus()
    await page.keyboard.press('Escape')
    await expect(panel).toHaveCount(0)
    await expect(trigger).toBeFocused()
  })

  test('@smoke resize preserves Timeline mode and internal scrolling', async ({ page }) => {
    await page.setViewportSize({ width: 700, height: 900 })
    await page.goto('/')
    await selectTimeline(page)

    const timelineButton = page.getByRole('button', { name: 'timeline', exact: true })
    for (const width of [1280, 700]) {
      await page.setViewportSize({ width, height: 900 })
      await expect(timelineButton).toHaveClass(/bg-blue-600/)
      await expect(page.getByTestId('replay-timeline')).toBeVisible()
    }
  })
})
