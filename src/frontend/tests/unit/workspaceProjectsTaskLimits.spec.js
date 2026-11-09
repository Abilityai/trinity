/**
 * abilityai/trinity-enterprise#1133 — the task-field limits the client checks
 * are the limits the server enforces: title 200, objective / definition of
 * done 4000 characters.
 *
 * The server-side test that compares the two skips wherever the frontend tree
 * is absent, so this spec pins the same pair from the side public CI always
 * runs. It executes `validateTaskFields` at each boundary (code points, as the
 * server's `len` counts them) rather than only reading the constants; when the
 * backend module is checked out beside it, its constants are compared too.
 */
import { describe, it, expect } from 'vitest'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import {
  TASK_TITLE_MAX,
  TASK_TEXT_MAX,
  validateTaskFields,
} from '@/components/portal/projects/projectsUtils'

// The pair the server holds (work.py `TITLE_MAX, TEXT_MAX`). Change both sides together.
const SERVER = { title: 200, text: 4000 }

describe('task-field limits match the server (ent#1133)', () => {
  it('exports the server pair', () => {
    expect({ title: TASK_TITLE_MAX, text: TASK_TEXT_MAX }).toEqual(SERVER)
  })

  it('accepts a title at the limit and refuses one past it', () => {
    expect(validateTaskFields({ title: 'x'.repeat(SERVER.title) })).toEqual({})
    expect(validateTaskFields({ title: 'x'.repeat(SERVER.title + 1) })).toHaveProperty('title')
  })

  it.each(['objective', 'done_definition'])('accepts %s at the limit and refuses one past it', (f) => {
    expect(validateTaskFields({ [f]: 'x'.repeat(SERVER.text) }, [f])).toEqual({})
    expect(validateTaskFields({ [f]: 'x'.repeat(SERVER.text + 1) }, [f])).toHaveProperty(f)
  })

  it('counts code points, not UTF-16 units, the way the server does', () => {
    expect(validateTaskFields({ title: '😀'.repeat(SERVER.title) })).toEqual({})
  })

  it('agrees with the backend constants when that module is checked out', () => {
    const here = dirname(fileURLToPath(import.meta.url))
    const py = resolve(here, '../../../backend/enterprise/backend/projects/work.py')
    if (!existsSync(py)) return // public checkout: the literal pair above is the pin
    const m = readFileSync(py, 'utf8').match(
      /^TITLE_MAX, TEXT_MAX, ACTOR_MAX, LOG_MAX = (\d+), (\d+), \d+, \d+$/m,
    )
    expect(m, 'work.py limits line not found — update this spec with it').not.toBeNull()
    expect({ title: Number(m[1]), text: Number(m[2]) }).toEqual(SERVER)
  })
})
