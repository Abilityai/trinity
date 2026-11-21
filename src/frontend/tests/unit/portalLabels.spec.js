/**
 * #4344 — the Workspace shows words, not the backend's enum values. Every map
 * has a humanised fallback so a value the backend adds tomorrow never renders
 * empty or raw.
 */
import { describe, it, expect } from 'vitest'
import {
  humaniseEnum,
  autonomyLevelLabel,
  decisionOutcomeLabel,
  decisionStatusLabel,
  roleStatusLabel,
  readinessLabel,
  reportTypeLabel,
  reportTitle,
} from '../../src/components/portal/portalLabels'
import { materialKindLabel, priorityLabel } from '../../src/components/portal/projects/projectsUtils'

describe('humaniseEnum', () => {
  it('turns a code into a capitalised phrase', () => {
    expect(humaniseEnum('kpi_snapshot')).toBe('Kpi snapshot')
    expect(humaniseEnum('needs-decision')).toBe('Needs decision')
    expect(humaniseEnum('ready')).toBe('Ready')
  })
  it('never renders empty', () => {
    expect(humaniseEnum('')).toBe('Unknown')
    expect(humaniseEnum(null)).toBe('Unknown')
    expect(humaniseEnum(undefined)).toBe('Unknown')
  })
})

describe('label maps', () => {
  it('autonomy levels read as words', () => {
    expect(autonomyLevelLabel('L0')).toBe('On request')
    expect(autonomyLevelLabel('L1')).toBe('On request')
    expect(autonomyLevelLabel('L2')).toBe('Delegated')
    expect(autonomyLevelLabel('L3')).toBe('Broad')
    expect(autonomyLevelLabel('L9')).toBe('L9')
    expect(autonomyLevelLabel('')).toBe('Unknown')
  })
  it('decision outcomes and statuses', () => {
    expect(decisionOutcomeLabel('approved')).toBe('Approved')
    expect(decisionOutcomeLabel('deferred')).toBe('Deferred')
    expect(decisionOutcomeLabel('killed')).toBe('Dropped')
    expect(decisionOutcomeLabel('vetoed')).toBe('Vetoed')
    expect(decisionStatusLabel('active')).toBe('Active')
    expect(decisionStatusLabel('routed')).toBe('Routed')
    expect(decisionStatusLabel('expired')).toBe('Expired')
    expect(decisionStatusLabel('superseded')).toBe('Replaced')
    expect(decisionStatusLabel('closed')).toBe('Closed')
    expect(decisionStatusLabel('reversed')).toBe('Reversed')
    expect(decisionStatusLabel('on_hold')).toBe('On hold')
  })
  it('role status and readiness', () => {
    expect(roleStatusLabel('active')).toBe('Active')
    expect(roleStatusLabel('draft')).toBe('Draft')
    expect(roleStatusLabel('under_review')).toBe('Under review')
    expect(readinessLabel('calibrating')).toBe('Calibrating')
    expect(readinessLabel('ready')).toBe('Ready')
    expect(readinessLabel('paused_for_review')).toBe('Paused for review')
  })
  it('report type and title', () => {
    expect(reportTypeLabel('kpi_snapshot')).toBe('KPI snapshot')
    expect(reportTypeLabel('weekly_digest')).toBe('Weekly digest')
    expect(reportTitle({ title: 'Q3 numbers', report_type: 'kpi_snapshot' })).toBe('Q3 numbers')
    expect(reportTitle({ title: '', report_type: 'kpi_snapshot', created_at: '2026-10-21T09:00:00Z' })).toBe('Report · Oct 21, 2026')
    expect(reportTitle({ report_type: 'kpi_snapshot' })).toBe('Report')
    expect(reportTitle({ report_type: 'x', created_at: 'not a date' })).toBe('Report')
  })
  it('project material kinds and task priority', () => {
    expect(materialKindLabel('file')).toBe('File')
    expect(materialKindLabel('report')).toBe('Report')
    expect(materialKindLabel('decision')).toBe('Decision')
    expect(materialKindLabel('canvas_snapshot')).toBe('Canvas snapshot')
    expect(priorityLabel('p1')).toBe('P1')
    expect(priorityLabel('p3')).toBe('P3')
    expect(priorityLabel('urgent')).toBe('Urgent')
    expect(priorityLabel('')).toBe('—')
  })
})
