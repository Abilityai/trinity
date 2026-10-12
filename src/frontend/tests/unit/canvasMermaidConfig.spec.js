/**
 * #3285 — mermaid 12 switched its defaults to the ELK layout and the `neo`
 * look. The canvas pins the v11 pair so agents' existing diagrams keep their
 * layout and style; `initialize()` must receive it alongside the security keys.
 */
import { describe, it, expect } from 'vitest'
import { MERMAID_CONFIG } from '@/components/canvas/canvasUtils'

describe('MERMAID_CONFIG', () => {
  it('pins the pre-12 layout and look', () => {
    expect(MERMAID_CONFIG.layout).toBe('dagre')
    expect(MERMAID_CONFIG.look).toBe('classic')
  })

  it('keeps the strict security settings', () => {
    expect(MERMAID_CONFIG.securityLevel).toBe('strict')
    expect(MERMAID_CONFIG.secure).toEqual(expect.arrayContaining(['themeCSS', 'fontFamily', 'altFontFamily']))
  })
})
