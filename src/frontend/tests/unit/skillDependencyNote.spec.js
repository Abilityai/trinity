/**
 * #3136 + trinity-enterprise#736 — the two sentences an assign adds beside its
 * delivery note, as pure rules.
 *
 *   `dependencyText(report)` — what the injection found missing on THIS agent
 *   (`missing_binary:<b>` / `missing_env:<KEY>` in `delivery.skills[n].warnings`).
 *   Its own amber line: the delivery stays green, delivery and dependencies are
 *   two facts (the ent#672 precedent). Only the two "missing" codes are said;
 *   "not checked" codes (`packages_not_checked`, `dep_check_skipped`) are not.
 *
 *   `memberDeprecationReport(members)` — a set's member rows as a report
 *   `deprecationText` reads, so a set assign names its deprecated members with
 *   the same sentence a skill assign uses, even when nothing new was added.
 */
import { describe, it, expect } from 'vitest'
import { dependencyText, memberDeprecationReport, deprecationText } from '../../src/utils/skillDelivery'

const report = (skills) => ({ status: 'injected', skills })

describe('dependencyText', () => {
  it('is null when there is nothing missing to say', () => {
    expect(dependencyText(null)).toBeNull()
    expect(dependencyText({ status: 'injected' })).toBeNull()
    expect(dependencyText(report({ live: { status: 'injected' } }))).toBeNull()
    expect(dependencyText(report({ old: { status: 'injected', warnings: ['deprecated:backlog'] } }))).toBeNull()
  })

  it('names the skill and the binary it needs', () => {
    expect(dependencyText(report({
      'pdf-report': { status: 'injected', warnings: ['missing_binary:pdftotext'] },
    }))).toBe("pdf-report needs the binary pdftotext, which this agent doesn't have.")
  })

  it('says both kinds in one sentence per skill, plurals included', () => {
    expect(dependencyText(report({
      'pdf-report': { status: 'injected', warnings: ['missing_binary:pdftotext', 'missing_env:PDF_API_KEY'] },
      mailer: { status: 'injected', warnings: ['missing_env:SMTP_HOST', 'missing_env:SMTP_PASS'] },
      convert: { status: 'injected', warnings: ['missing_binary:a', 'missing_binary:b', 'missing_binary:c'] },
    }))).toBe(
      "pdf-report needs the binary pdftotext and the environment variable PDF_API_KEY, which this agent doesn't have. "
      + "mailer needs the environment variables SMTP_HOST and SMTP_PASS, which this agent doesn't have. "
      + "convert needs the binaries a, b and c, which this agent doesn't have.",
    )
  })

  it('says nothing for a dependency that was only not checked', () => {
    expect(dependencyText(report({
      x: { status: 'injected', warnings: ['packages_not_checked', 'dep_check_skipped', 'multi_file_dropped_old_image'] },
    }))).toBeNull()
  })

  it('names a dependency once even when the code repeats', () => {
    expect(dependencyText(report({
      x: { status: 'injected', warnings: ['missing_binary:jq', 'missing_binary:jq'] },
    }))).toBe("x needs the binary jq, which this agent doesn't have.")
  })

  it('leaves env keys out where another line already names them (the set notes)', () => {
    const r = report({
      a: { status: 'injected', warnings: ['missing_binary:jq', 'missing_env:TOKEN'] },
      b: { status: 'injected', warnings: ['missing_env:ONLY_ENV'] },
    })
    expect(dependencyText(r, { env: false })).toBe("a needs the binary jq, which this agent doesn't have.")
    expect(dependencyText(report({ b: r.skills.b }), { env: false })).toBeNull()
  })
})

describe('memberDeprecationReport', () => {
  it('turns deprecated member rows into the report deprecationText reads', () => {
    const members = [
      { name: 'old', state: 'assigned', deprecated: true, deprecation: 'deprecated:backlog' },
      { name: 'prose', state: 'assigned', deprecated: true, deprecation: 'deprecated' },
      { name: 'live', state: 'assigned', deprecated: false, deprecation: null },
    ]
    expect(deprecationText(memberDeprecationReport(members)))
      .toBe('old is deprecated — superseded by backlog; prose is deprecated.')
  })

  it('is empty for no members, an older backend, or none deprecated', () => {
    expect(deprecationText(memberDeprecationReport(undefined))).toBeNull()
    expect(deprecationText(memberDeprecationReport([{ name: 'a', state: 'assigned' }]))).toBeNull()
  })

  it('still names a flagged member whose code is missing', () => {
    expect(deprecationText(memberDeprecationReport([{ name: 'a', deprecated: true }])))
      .toBe('a is deprecated.')
  })
})
