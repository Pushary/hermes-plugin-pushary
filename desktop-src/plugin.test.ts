import { beforeEach, describe, expect, mock, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { createElement, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const notices: unknown[] = []

const fakeAtom = <T>(initial: T) => {
  let value = initial
  const listeners = new Set<(next: T) => void>()
  return {
    get: () => value,
    set: (next: T) => {
      value = next
      for (const listener of listeners) listener(next)
    },
    listen: (listener: (next: T) => void) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    }
  }
}

const passThrough = ({ children }: { children?: ReactNode }) => createElement('div', null, children)
const popoverSurface = ({ children, style }: { children?: ReactNode; style?: Record<string, unknown> }) =>
  createElement('section', { style }, children)

mock.module('@hermes/plugin-sdk', () => ({
  atom: fakeAtom,
  useValue: (store: { get(): unknown }) => store.get(),
  Button: ({ children, disabled }: { children?: ReactNode; disabled?: boolean }) =>
    createElement('button', { disabled }, children),
  CopyButton: ({ label }: { label?: string }) => createElement('button', null, label),
  Popover: passThrough,
  PopoverContent: popoverSurface,
  PopoverTrigger: passThrough,
  StatusDot: ({ tone }: { tone: string }) => createElement('i', { 'data-tone': tone }),
  STATUSBAR_AREAS: { left: 'statusBar.left', right: 'statusBar.right' },
  host: {
    notify: (notice: unknown) => notices.push(notice),
    state: { gateway: fakeAtom('open') }
  }
}))

const desktop = await import('./plugin.ts')

const fixture = (name: string): unknown =>
  JSON.parse(readFileSync(join(import.meta.dir, 'contract-fixtures', name), 'utf-8'))

const approvalsFixture = fixture('approvals-response.json')
const facts = fixture('contract-facts.json') as Record<string, unknown>

interface FakeCall {
  readonly path: string
  readonly opts?: { readonly method?: string; readonly body?: unknown }
}

const fakeContext = (answer: (call: FakeCall) => Promise<unknown>) => {
  const calls: FakeCall[] = []
  const ctx = {
    register: () => () => {},
    rest: (path: string, opts?: FakeCall['opts']) => {
      const call = opts === undefined ? { path } : { path, opts }
      calls.push(call)
      return answer(call)
    },
    onEvent: () => () => {},
    onDispose: () => {},
    setInterval: () => () => {},
    os: { openExternal: async () => true }
  }
  return { ctx, calls }
}

const clone = (value: unknown): Record<string, unknown> => JSON.parse(JSON.stringify(value))

beforeEach(() => {
  notices.length = 0
})

describe('the wire contract is the one the Python backend produces', () => {
  test('names, paths, patterns and enumerations match the backend facts', () => {
    expect(desktop.CONTRACT_VERSION).toBe(facts.version)
    expect(desktop.APPROVALS_CHANGED_WIRE_EVENT).toBe(facts.approvalsChangedWireEvent)
    expect(desktop.APPROVALS_PATH).toBe(facts.approvalsPath)
    expect(desktop.ANSWER_PATH_SUFFIX).toBe(facts.answerPathSuffix)
    expect(desktop.CORRELATION_ID_PATTERN.source).toBe(facts.correlationIdPattern)
    expect(desktop.TOKEN_PATTERN.source).toBe(facts.tokenPattern)
    expect([...desktop.APPROVAL_CHOICES]).toEqual(facts.approvalChoices)
    expect([...desktop.CLAIM_STATES]).toEqual(facts.claimStates)
    expect([...desktop.RECENT_OUTCOMES]).toEqual(facts.recentOutcomes)
    expect([...desktop.ANSWER_OUTCOMES]).toEqual(facts.answerOutcomes)
    expect([...desktop.HOST_FAILURE_OUTCOMES]).toEqual(facts.hostFailureOutcomes)
  })

  test('every variant the backend emits parses without loss', () => {
    expect(desktop.parseApprovalsResponse(approvalsFixture)).toEqual(approvalsFixture)
  })

  test('every answer outcome the backend emits parses', () => {
    for (const response of fixture('answer-responses.json') as unknown[]) {
      expect(desktop.parseAnswerResponse(response)).toEqual(response)
    }
  })

  test('a response from another contract version, or with any malformed part, is refused whole', () => {
    const mutations: ReadonlyArray<(response: Record<string, unknown>) => void> = [
      response => { response.version = 2 },
      response => { delete response.setup },
      response => { (response.waiting as Record<string, unknown>[])[0]!.claim = 'maybe' },
      response => { (response.waiting as Record<string, unknown>[])[0]!.answerableHere = 'yes' },
      response => { delete (response.waiting as Record<string, unknown>[])[0]!.answerableHere },
      response => { (response.waiting as Record<string, unknown>[])[0]!.routedTo = 7 },
      response => { (response.waiting as Record<string, unknown>[])[0]!.correlationId = '../../config' },
      response => { ((response.waiting as Record<string, unknown>[])[0]!.choices as Record<string, unknown>[])[0]!.choice = 'yes' },
      response => { (response.recent as Record<string, unknown>[])[0]!.settledAtMs = -1 },
      response => { (response.recent as Record<string, unknown>[])[0]!.answerSource = 'Mobile App' },
      response => { (response.recent as Record<string, unknown>[])[0]!.outcome = 'approved' },
      response => { (response.setup as Record<string, unknown>).apiKey = 'yes' },
      response => { response.waiting = {} }
    ]
    for (const mutate of mutations) {
      const response = clone(approvalsFixture)
      mutate(response)
      expect(desktop.parseApprovalsResponse(response)).toBeNull()
    }
    for (const value of [null, undefined, 'ok', [], 1]) expect(desktop.parseApprovalsResponse(value)).toBeNull()
    expect(desktop.parseAnswerResponse({ version: 1, outcome: 'approved' })).toBeNull()
  })

  test('an answer path is only ever built from a correlation id', () => {
    expect(desktop.answerPath('6f1c2d3e-4b5a-4c6d-8e7f-000000000001')).toBe('/approvals/6f1c2d3e-4b5a-4c6d-8e7f-000000000001/answer')
    for (const hostile of ['../x', '6F1C2D3E-4B5A-4C6D-8E7F-000000000001', '', '6f1c2d3e-4b5a-4c6d-8e7f-000000000001/../x']) {
      expect(desktop.answerPath(hostile)).toBeNull()
    }
  })
})

describe('what the chip and the panel say', () => {
  const ready = desktop.panelStateFrom(approvalsFixture)

  test('waiting approvals come before setup advice, and the chip names Pushary', () => {
    expect(desktop.chipSummary(ready)).toEqual({ tone: 'warn', text: 'Pushary · 6 approvals waiting', label: 'Pushary: 6 approvals waiting' })
  })

  test('each panel state has its own chip', () => {
    expect(desktop.chipSummary({ kind: 'loading' }).tone).toBe('muted')
    expect(desktop.chipSummary({ kind: 'offline' }).tone).toBe('muted')
    expect(desktop.chipSummary({ kind: 'incompatible' }).text).toBe('Pushary · update')
    expect(desktop.panelStateFrom({ version: 2 })).toEqual({ kind: 'incompatible' })
  })

  test('setup gaps are named when nothing is waiting', () => {
    const response = clone(approvalsFixture)
    response.waiting = []
    response.setup = { apiKey: false, transport: false, pluginVersion: null }
    expect(desktop.chipSummary(desktop.panelStateFrom(response)).text).toBe('Pushary · set up')
    response.setup = { apiKey: true, transport: false, pluginVersion: null }
    expect(desktop.chipSummary(desktop.panelStateFrom(response)).text).toBe('Pushary · not routing')
    response.setup = { apiKey: true, transport: true, pluginVersion: null }
    expect(desktop.chipSummary(desktop.panelStateFrom(response)).tone).toBe('good')
  })

  test('recent outcomes say what happened and where, never more than the record proves', () => {
    if (ready.kind !== 'ready') throw new Error('fixture did not parse')
    expect(ready.response.recent.map(desktop.outcomeText)).toEqual([
      'Allowed once · phone',
      'Allowed for this session · Hermes Desktop',
      'Always allowed · Mac',
      'Denied · Pushary',
      'Handed to Hermes',
      'Timed out in Hermes'
    ])
  })

  test('a deny nobody gave is not answered, and every way Hermes fails a transport is named', () => {
    if (ready.kind !== 'ready') throw new Error('fixture did not parse')
    const item = ready.response.recent[0]!
    expect(desktop.outcomeText({ ...item, outcome: 'deny', answerSource: null })).toBe('Not answered')
    for (const hostOutcome of desktop.HOST_FAILURE_OUTCOMES) {
      expect(desktop.outcomeText({ ...item, outcome: 'ended', hostOutcome })).not.toBe('Ended')
    }
    expect(desktop.outcomeText({ ...item, outcome: 'ended', hostOutcome: 'transport_new_kind' })).toBe('Ended')
  })

  test('an unknown answering surface is named as Pushary, not guessed', () => {
    if (ready.kind !== 'ready') throw new Error('fixture did not parse')
    const item = { ...ready.response.recent[0]!, answerSource: 'new_surface' }
    expect(desktop.outcomeText(item)).toBe('Allowed once · Pushary')
  })

  test('a waiting card follows the claim through every state', () => {
    if (ready.kind !== 'ready') throw new Error('fixture did not parse')
    const item = ready.response.waiting[0]!
    const now = item.openedAtMs + 75_000
    expect(desktop.waitingLine(item, now)).toBe('Waiting on your phone · 1:15')
    expect(desktop.waitingLine({ ...item, claim: 'claiming' }, now)).toBe('Sending your answer…')
    expect(desktop.waitingLine({ ...item, claim: 'won' }, now)).toBe('Answered here. Handing it to Hermes…')
    expect(desktop.waitingLine({ ...item, claim: 'lost' }, now)).toBe('Answer it on your phone · 1:15')
  })

  test('the card being answered says so at once, before the backend reports a claim', () => {
    if (ready.kind !== 'ready') throw new Error('fixture did not parse')
    const [item, other] = ready.response.waiting
    if (!item || !other) throw new Error('fixture has too few waiting approvals')
    const now = item.openedAtMs + 75_000
    expect(desktop.approvalStatusLine(item, now, item.correlationId)).toBe('Sending your answer…')
    expect(desktop.approvalStatusLine(item, now, other.correlationId)).toBe(desktop.waitingLine(item, now))
    expect(desktop.approvalStatusLine(item, now, null)).toBe(desktop.waitingLine(item, now))
  })

  test('an approval a Team route sends to someone else says who answers it', () => {
    if (ready.kind !== 'ready') throw new Error('fixture did not parse')
    const routed = ready.response.waiting.filter(item => !item.answerableHere)
    expect(routed.map(item => desktop.waitingLine(item, item.openedAtMs + 75_000))).toEqual([
      'Routed to Sam Patel · 1:15',
      'Answer it on your phone · 1:15'
    ])
  })

  test('elapsed and age labels', () => {
    expect(desktop.waitedFor(-5)).toBe('0:00')
    expect(desktop.waitedFor(605_000)).toBe('10:05')
    expect(desktop.ago(30_000)).toBe('just now')
    expect(desktop.ago(5 * 60_000)).toBe('5 min ago')
    expect(desktop.ago(3 * 3_600_000)).toBe('3 h ago')
    expect(desktop.ago(50 * 3_600_000)).toBe('2 d ago')
  })

  test('an answer response that does not parse is reported as a failure, never as success', () => {
    expect(desktop.answerNotice({ version: 1, outcome: 'answered_here' }).kind).toBe('success')
    for (const value of [undefined, {}, { version: 1, outcome: 'ok' }, { version: 2, outcome: 'answered_here' }]) {
      expect(desktop.answerNotice(value).kind).toBe('error')
    }
  })
})

describe('the approvals store', () => {
  test('loads the backend view, and says offline or incompatible when it cannot', async () => {
    const loaded = desktop.createApprovalsStore(fakeContext(async () => approvalsFixture).ctx)
    await loaded.refresh()
    expect(loaded.state.get().kind).toBe('ready')
    const offline = desktop.createApprovalsStore(fakeContext(async () => { throw new Error('404') }).ctx)
    await offline.refresh()
    expect(offline.state.get()).toEqual({ kind: 'offline' })
    const incompatible = desktop.createApprovalsStore(fakeContext(async () => ({ version: 9 })).ctx)
    await incompatible.refresh()
    expect(incompatible.state.get()).toEqual({ kind: 'incompatible' })
  })

  test('overlapping refreshes collapse into one more read, not one per trigger', async () => {
    let release: (value: unknown) => void = () => {}
    const { ctx, calls } = fakeContext(() => new Promise(resolve => { release = resolve }))
    const store = desktop.createApprovalsStore(ctx)
    const first = store.refresh()
    void store.refresh()
    void store.refresh()
    release(approvalsFixture)
    await first
    await Promise.resolve()
    release(approvalsFixture)
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(calls.length).toBe(2)
  })

  test('an answer posts exactly the offered choice to its own approval and reports the outcome', async () => {
    const { ctx, calls } = fakeContext(async call => (call.opts?.method === 'POST'
      ? { version: 1, outcome: 'already_closed' }
      : approvalsFixture))
    const store = desktop.createApprovalsStore(ctx)
    await store.refresh()
    const state = store.state.get()
    if (state.kind !== 'ready') throw new Error('fixture did not parse')
    await store.answer(state.response.waiting[0]!, 'session')
    expect(calls[1]).toEqual({
      path: '/approvals/6f1c2d3e-4b5a-4c6d-8e7f-000000000001/answer',
      opts: { method: 'POST', body: { choice: 'session' } }
    })
    expect(notices).toEqual([{ kind: 'info', message: 'This approval had already closed, so your answer was not used.' }])
    expect(store.answering.get()).toBeNull()
  })

  test('refreshes live while the panel is open or something waits, and slowly otherwise', async () => {
    const idle = clone(approvalsFixture)
    idle.waiting = []
    const store = desktop.createApprovalsStore(fakeContext(async () => idle).ctx)
    expect(store.refreshIsDue(Date.now())).toBe(true)
    await store.refresh()
    const now = Date.now()
    expect(store.refreshIsDue(now)).toBe(false)
    expect(store.refreshIsDue(now + 15_000)).toBe(true)
    store.open.set(true)
    expect(store.refreshIsDue(now)).toBe(true)
    store.open.set(false)
    store.state.set(desktop.panelStateFrom(approvalsFixture))
    expect(store.refreshIsDue(now)).toBe(true)
  })

  test('a second answer while one is in flight sends nothing', async () => {
    let release: (value: unknown) => void = () => {}
    const { ctx, calls } = fakeContext(call => (call.opts?.method === 'POST'
      ? new Promise(resolve => { release = resolve })
      : Promise.resolve(approvalsFixture)))
    const store = desktop.createApprovalsStore(ctx)
    await store.refresh()
    const state = store.state.get()
    if (state.kind !== 'ready') throw new Error('fixture did not parse')
    const first = store.answer(state.response.waiting[0]!, 'once')
    await store.answer(state.response.waiting[0]!, 'deny')
    release({ version: 1, outcome: 'answered_here' })
    await first
    expect(calls.filter(call => call.opts?.method === 'POST').length).toBe(1)
  })

  test('a failed answer request is reported as a failure', async () => {
    const { ctx } = fakeContext(async call => {
      if (call.opts?.method === 'POST') throw new Error('gateway restarted')
      return approvalsFixture
    })
    const store = desktop.createApprovalsStore(ctx)
    await store.refresh()
    const state = store.state.get()
    if (state.kind !== 'ready') throw new Error('fixture did not parse')
    await store.answer(state.response.waiting[0]!, 'once')
    expect(notices).toEqual([{ kind: 'error', message: 'Hermes Desktop could not send that answer. Answer on your phone.' }])
  })
})

describe('the panel renders every state without React warnings', () => {
  const render = (state: unknown): string => {
    const store = desktop.createApprovalsStore(fakeContext(async () => approvalsFixture).ctx)
    store.state.set(state as Parameters<typeof store.state.set>[0])
    const errors: unknown[] = []
    const original = console.error
    console.error = (...args: unknown[]) => { errors.push(args) }
    try {
      return renderToStaticMarkup(createElement(desktop.PusharyChip, { store, ctx: fakeContext(async () => null).ctx }))
    } finally {
      console.error = original
      expect(errors).toEqual([])
    }
  }

  test('waiting approvals with their choices, then recent outcomes', () => {
    const markup = render(desktop.panelStateFrom(approvalsFixture))
    expect(markup).toContain('6 approvals waiting')
    expect(markup).toContain('git push --force origin release-1')
    expect(markup).toContain('>Allow for this session</button>')
    expect(markup).toContain('Sending your answer…')
    expect(markup).toContain('Recent')
    expect(markup).toContain('Allowed once · phone')
    expect(markup).toContain('hermes config set security.approval.transport pushary')
  })

  test('the panel replaces the popover default width, fits the window and scrolls instead of growing past it', () => {
    const markup = render(desktop.panelStateFrom(approvalsFixture))
    expect(markup).toContain('<section style="width:min(340px, calc(100vw - 24px));padding:0">')
    expect(markup).toContain('max-height:min(560px, calc(var(--radix-popover-content-available-height, 100vh) - 16px))')
    expect(markup.match(/overflow-y:auto/g)?.length).toBeGreaterThan(1)
  })

  test('a recent approval without a command still names what it was', () => {
    const parsed = desktop.parseApprovalsResponse(approvalsFixture)
    const settled = parsed?.recent[0]
    if (!parsed || !settled) throw new Error('fixture did not parse')
    const response = { ...parsed, waiting: [], recent: [{ ...settled, subject: '' }] }
    expect(render(desktop.panelStateFrom(response))).toContain('A command Hermes flagged')
  })

  test('a card already being answered cannot be answered again', () => {
    const markup = render(desktop.panelStateFrom(approvalsFixture))
    const claimedCard = markup.slice(markup.indexOf('release-2'), markup.indexOf('release-3'))
    expect(claimedCard).toContain('<button disabled="">Allow once</button>')
  })

  test('a card routed to someone else offers no buttons at all', () => {
    const markup = render(desktop.panelStateFrom(approvalsFixture))
    const routedCard = markup.slice(markup.indexOf('release-5'), markup.indexOf('release-6'))
    expect(routedCard).toContain('Routed to Sam Patel')
    expect(routedCard).not.toContain('<button')
  })

  test('loading, offline and incompatible each explain themselves', () => {
    expect(render({ kind: 'loading' })).toContain('Loading…')
    expect(render({ kind: 'offline' })).toContain('backend is not running')
    expect(render({ kind: 'incompatible' })).toContain('does not match its backend')
  })

  test('nothing waiting and fully set up says so plainly', () => {
    const response = clone(approvalsFixture)
    response.waiting = []
    response.recent = []
    response.setup = { apiKey: true, transport: true, pluginVersion: '1.0.0' }
    const markup = render(desktop.panelStateFrom(response))
    expect(markup).toContain('No approvals waiting.')
    expect(markup).not.toContain('Recent')
    expect(markup).toContain('data-tone="good"')
  })
})
