import {
  atom, Button, CopyButton, host, Popover, PopoverContent, PopoverTrigger, STATUSBAR_AREAS, StatusDot, useValue
} from '@hermes/plugin-sdk'
import type { HermesPlugin, PluginContext, StatusTone } from '@hermes/plugin-sdk'
import { createElement as h, useEffect, useState } from 'react'
import type { ComponentProps, CSSProperties, ReactNode } from 'react'

type ButtonVariant = NonNullable<ComponentProps<typeof Button>['variant']>
type NotificationKind = Parameters<typeof host.notify>[0]['kind']
type Store<T> = ReturnType<typeof atom<T>>

export const PLUGIN_ID = 'pushary'
export const CONTRACT_VERSION = 1
export const APPROVALS_CHANGED_WIRE_EVENT = 'plugin.pushary.approvals.changed'
export const APPROVALS_PATH = '/approvals'
export const ANSWER_PATH_SUFFIX = '/answer'
export const CORRELATION_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
export const TOKEN_PATTERN = /^[a-z][a-z0-9_]{0,63}$/

export const APPROVAL_CHOICES = ['once', 'session', 'always', 'deny'] as const
export const CLAIM_STATES = ['claiming', 'won', 'lost'] as const
export const RECENT_OUTCOMES = ['once', 'session', 'always', 'deny', 'handed_to_hermes', 'ended'] as const
export const ANSWER_OUTCOMES = [
  'answered_here', 'already_closed', 'unreachable',
  'not_found', 'not_waiting', 'not_answerable_here', 'not_offered', 'already_claimed'
] as const
export const HOST_FAILURE_OUTCOMES = [
  'transport_busy', 'transport_error', 'transport_interrupted', 'transport_invalid', 'transport_stale', 'transport_timeout'
] as const

export type ApprovalChoice = (typeof APPROVAL_CHOICES)[number]
export type ClaimState = (typeof CLAIM_STATES)[number]
export type RecentOutcome = (typeof RECENT_OUTCOMES)[number]
export type AnswerOutcome = (typeof ANSWER_OUTCOMES)[number]
export type HostFailureOutcome = (typeof HOST_FAILURE_OUTCOMES)[number]

export interface OfferedChoice {
  readonly choice: ApprovalChoice
  readonly label: string
}

export interface WaitingApproval {
  readonly correlationId: string
  readonly subject: string
  readonly choices: readonly OfferedChoice[]
  readonly openedAtMs: number
  readonly deadlineAtMs: number
  readonly answerableHere: boolean
  readonly routedTo: string | null
  readonly claim: ClaimState | null
}

export interface RecentApproval {
  readonly correlationId: string
  readonly subject: string
  readonly openedAtMs: number
  readonly settledAtMs: number
  readonly outcome: RecentOutcome
  readonly answerSource: string | null
  readonly hostOutcome: string | null
}

export interface Setup {
  readonly apiKey: boolean
  readonly transport: boolean
  readonly pluginVersion: string | null
}

export interface ApprovalsResponse {
  readonly version: typeof CONTRACT_VERSION
  readonly waiting: readonly WaitingApproval[]
  readonly recent: readonly RecentApproval[]
  readonly setup: Setup
}

export interface AnswerResponse {
  readonly version: typeof CONTRACT_VERSION
  readonly outcome: AnswerOutcome
}

export type PanelState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'offline' }
  | { readonly kind: 'incompatible' }
  | { readonly kind: 'ready'; readonly response: ApprovalsResponse }

interface Notice {
  readonly kind: NotificationKind
  readonly message: string
}

interface ChipSummary {
  readonly tone: StatusTone
  readonly text: string
  readonly label: string
}

type UnknownRecord = Readonly<Record<string, unknown>>

const isRecord = (value: unknown): value is UnknownRecord =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const isOneOf = <T extends string>(values: readonly T[]) => (value: unknown): value is T =>
  typeof value === 'string' && (values as readonly string[]).includes(value)

const isApprovalChoice = isOneOf(APPROVAL_CHOICES)
const isClaimState = isOneOf(CLAIM_STATES)
const isRecentOutcome = isOneOf(RECENT_OUTCOMES)
const isAnswerOutcome = isOneOf(ANSWER_OUTCOMES)
const isHostFailureOutcome = isOneOf(HOST_FAILURE_OUTCOMES)

const isInstant = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0

const isCorrelationId = (value: unknown): value is string =>
  typeof value === 'string' && CORRELATION_ID_PATTERN.test(value)

const isTokenOrNull = (value: unknown): value is string | null =>
  value === null || (typeof value === 'string' && TOKEN_PATTERN.test(value))

const isContractVersion = (value: unknown): value is typeof CONTRACT_VERSION => value === CONTRACT_VERSION

const parseList = <T>(value: unknown, parse: (item: unknown) => T | null): readonly T[] | null => {
  if (!Array.isArray(value)) return null
  const parsed: T[] = []
  for (const item of value) {
    const result = parse(item)
    if (result === null) return null
    parsed.push(result)
  }
  return parsed
}

const parseOfferedChoice = (value: unknown): OfferedChoice | null => {
  if (!isRecord(value) || !isApprovalChoice(value.choice) || typeof value.label !== 'string') return null
  return { choice: value.choice, label: value.label }
}

const parseWaitingApproval = (value: unknown): WaitingApproval | null => {
  if (!isRecord(value)) return null
  const choices = parseList(value.choices, parseOfferedChoice)
  const claim = value.claim === null || isClaimState(value.claim) ? value.claim : undefined
  const routedTo = value.routedTo === null || typeof value.routedTo === 'string' ? value.routedTo : undefined
  if (
    !isCorrelationId(value.correlationId) || typeof value.subject !== 'string' || choices === null
    || !isInstant(value.openedAtMs) || !isInstant(value.deadlineAtMs) || claim === undefined
    || typeof value.answerableHere !== 'boolean' || routedTo === undefined
  ) return null
  return {
    correlationId: value.correlationId,
    subject: value.subject,
    choices,
    openedAtMs: value.openedAtMs,
    deadlineAtMs: value.deadlineAtMs,
    answerableHere: value.answerableHere,
    routedTo,
    claim
  }
}

const parseRecentApproval = (value: unknown): RecentApproval | null => {
  if (
    !isRecord(value) || !isCorrelationId(value.correlationId) || typeof value.subject !== 'string'
    || !isInstant(value.openedAtMs) || !isInstant(value.settledAtMs) || !isRecentOutcome(value.outcome)
    || !isTokenOrNull(value.answerSource) || !isTokenOrNull(value.hostOutcome)
  ) return null
  return {
    correlationId: value.correlationId,
    subject: value.subject,
    openedAtMs: value.openedAtMs,
    settledAtMs: value.settledAtMs,
    outcome: value.outcome,
    answerSource: value.answerSource,
    hostOutcome: value.hostOutcome
  }
}

const parseSetup = (value: unknown): Setup | null => {
  if (!isRecord(value) || typeof value.apiKey !== 'boolean' || typeof value.transport !== 'boolean') return null
  const pluginVersion = value.pluginVersion
  if (pluginVersion !== null && typeof pluginVersion !== 'string') return null
  return { apiKey: value.apiKey, transport: value.transport, pluginVersion }
}

export const parseApprovalsResponse = (value: unknown): ApprovalsResponse | null => {
  if (!isRecord(value) || !isContractVersion(value.version)) return null
  const waiting = parseList(value.waiting, parseWaitingApproval)
  const recent = parseList(value.recent, parseRecentApproval)
  const setup = parseSetup(value.setup)
  if (waiting === null || recent === null || setup === null) return null
  return { version: value.version, waiting, recent, setup }
}

export const parseAnswerResponse = (value: unknown): AnswerResponse | null => {
  if (!isRecord(value) || !isContractVersion(value.version) || !isAnswerOutcome(value.outcome)) return null
  return { version: value.version, outcome: value.outcome }
}

export const answerPath = (correlationId: string): string | null =>
  isCorrelationId(correlationId) ? `${APPROVALS_PATH}/${correlationId}${ANSWER_PATH_SUFFIX}` : null

const LIVE_REFRESH_MS = 2000
const IDLE_REFRESH_MS = 15000
const CLOCK_TICK_MS = 1000
const GATEWAY_OPEN = 'open'
const SETTINGS_CHANGED_EVENT = `plugin.${PLUGIN_ID}.settings.changed`
const TRANSPORT_COMMAND = 'hermes config set security.approval.transport pushary'
const SIGN_UP_URL = 'https://pushary.com/sign-up?from=hermes'
const UNKNOWN_SURFACE = 'Pushary'
const NOT_ANSWERED = 'Not answered'
const UNNAMED_SUBJECT = 'A command Hermes flagged'
const SENDING_ANSWER = 'Sending your answer…'

const ANSWER_SURFACES: Readonly<Record<string, string>> = {
  mobile_app: 'phone',
  mobile_background: 'phone',
  mac_app: 'Mac',
  slack: 'Slack',
  dashboard: 'web',
  decide_page: 'web',
  live_page: 'web',
  answer_link: 'web',
  inbox: 'web',
  sandbox: 'sandbox',
  hermes_desktop: 'Hermes Desktop'
}

const HOST_FAILURE_TEXT: Readonly<Record<HostFailureOutcome, string>> = {
  transport_busy: 'Hermes was busy',
  transport_error: 'Failed in Hermes',
  transport_interrupted: 'Turn interrupted',
  transport_invalid: 'Refused by Hermes',
  transport_stale: 'Refused by Hermes',
  transport_timeout: 'Timed out in Hermes'
}

const CHOICE_RESULTS: Readonly<Record<ApprovalChoice, string>> = {
  once: 'Allowed once',
  session: 'Allowed for this session',
  always: 'Always allowed',
  deny: 'Denied'
}

const CHOICE_BUTTON_VARIANTS: Readonly<Record<ApprovalChoice, ButtonVariant>> = {
  once: 'default',
  session: 'secondary',
  always: 'secondary',
  deny: 'outline'
}

const ANSWER_NOTICES: Readonly<Record<AnswerOutcome, Notice>> = {
  answered_here: { kind: 'success', message: 'Answered from Hermes Desktop.' },
  already_closed: { kind: 'info', message: 'This approval had already closed, so your answer was not used.' },
  unreachable: { kind: 'error', message: 'Pushary did not confirm your answer, so it was not used.' },
  not_found: { kind: 'info', message: 'This approval has already ended.' },
  not_waiting: { kind: 'info', message: 'This approval has already ended.' },
  not_answerable_here: { kind: 'info', message: 'A Team route sends this approval to someone else to answer.' },
  not_offered: { kind: 'error', message: 'Hermes did not offer that choice.' },
  already_claimed: { kind: 'info', message: 'This approval is already being answered.' }
}

const ANSWER_FAILED: Notice = {
  kind: 'error',
  message: 'Hermes Desktop could not send that answer. Answer on your phone.'
}

export const waitedFor = (ms: number): string => {
  const seconds = Math.max(0, Math.floor(ms / 1000))
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

export const ago = (ms: number): string => {
  const minutes = Math.floor(Math.max(0, ms) / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} h ago`
  return `${Math.floor(hours / 24)} d ago`
}

const surfaceOf = (answerSource: string): string => ANSWER_SURFACES[answerSource] ?? UNKNOWN_SURFACE

const choiceOutcomeText = (choice: ApprovalChoice, answerSource: string | null): string => {
  if (answerSource === null) return choice === 'deny' ? NOT_ANSWERED : CHOICE_RESULTS[choice]
  return `${CHOICE_RESULTS[choice]} · ${surfaceOf(answerSource)}`
}

export const outcomeText = (item: RecentApproval): string => {
  if (item.outcome === 'handed_to_hermes') return 'Handed to Hermes'
  if (isHostFailureOutcome(item.hostOutcome)) return HOST_FAILURE_TEXT[item.hostOutcome]
  if (item.outcome === 'ended') return 'Ended'
  return choiceOutcomeText(item.outcome, item.answerSource)
}

export const waitingLabel = (count: number): string =>
  count === 1 ? '1 approval waiting' : `${count} approvals waiting`

export const waitingLine = (item: WaitingApproval, now: number): string => {
  const waited = waitedFor(now - item.openedAtMs)
  if (!item.answerableHere) {
    return item.routedTo === null ? `Answer it on your phone · ${waited}` : `Routed to ${item.routedTo} · ${waited}`
  }
  switch (item.claim) {
    case 'claiming':
      return SENDING_ANSWER
    case 'won':
      return 'Answered here. Handing it to Hermes…'
    case 'lost':
      return `Answer it on your phone · ${waited}`
    case null:
      return `Waiting on your phone · ${waited}`
  }
}

export const approvalStatusLine = (item: WaitingApproval, now: number, answering: string | null): string =>
  answering === item.correlationId ? SENDING_ANSWER : waitingLine(item, now)

const subjectOf = (subject: string): string => subject || UNNAMED_SUBJECT

const readyChipSummary = (response: ApprovalsResponse): ChipSummary => {
  if (response.waiting.length > 0) {
    const waiting = waitingLabel(response.waiting.length)
    return { tone: 'warn', text: `Pushary · ${waiting}`, label: `Pushary: ${waiting}` }
  }
  if (!response.setup.apiKey) return { tone: 'warn', text: 'Pushary · set up', label: 'Pushary needs your API key' }
  if (!response.setup.transport) {
    return { tone: 'warn', text: 'Pushary · not routing', label: 'Approvals are not routed to Pushary' }
  }
  return { tone: 'good', text: 'Pushary', label: 'Pushary carries approvals to your phone' }
}

export const chipSummary = (state: PanelState): ChipSummary => {
  switch (state.kind) {
    case 'loading':
      return { tone: 'muted', text: 'Pushary', label: 'Pushary is loading' }
    case 'offline':
      return { tone: 'muted', text: 'Pushary', label: 'The Pushary backend is not running' }
    case 'incompatible':
      return { tone: 'bad', text: 'Pushary · update', label: 'This Pushary Desktop half does not match its backend' }
    case 'ready':
      return readyChipSummary(state.response)
  }
}

export const answerNotice = (value: unknown): Notice => {
  const response = parseAnswerResponse(value)
  return response === null ? ANSWER_FAILED : ANSWER_NOTICES[response.outcome]
}

export const panelStateFrom = (value: unknown): PanelState => {
  const response = parseApprovalsResponse(value)
  return response === null ? { kind: 'incompatible' } : { kind: 'ready', response }
}

interface ApprovalsStore {
  readonly state: Store<PanelState>
  readonly open: Store<boolean>
  readonly answering: Store<string | null>
  refresh(): Promise<void>
  answer(item: WaitingApproval, choice: ApprovalChoice): Promise<void>
  refreshIsDue(now: number): boolean
}

const waitingCount = (state: PanelState): number => (state.kind === 'ready' ? state.response.waiting.length : 0)

export const createApprovalsStore = (ctx: PluginContext): ApprovalsStore => {
  const state = atom<PanelState>({ kind: 'loading' })
  const open = atom(false)
  const answering = atom<string | null>(null)
  let inFlight: Promise<void> | null = null
  let refreshAgain = false
  let loadedAt = Number.NEGATIVE_INFINITY

  const load = async (): Promise<void> => {
    try {
      state.set(panelStateFrom(await ctx.rest<unknown>(APPROVALS_PATH)))
    } catch {
      state.set({ kind: 'offline' })
    } finally {
      loadedAt = Date.now()
    }
  }

  const refresh = (): Promise<void> => {
    if (inFlight !== null) {
      refreshAgain = true
      return inFlight
    }
    inFlight = load().finally(() => {
      inFlight = null
      if (refreshAgain) {
        refreshAgain = false
        void refresh()
      }
    })
    return inFlight
  }

  const answer = async (item: WaitingApproval, choice: ApprovalChoice): Promise<void> => {
    const path = answerPath(item.correlationId)
    if (answering.get() !== null || path === null) return
    answering.set(item.correlationId)
    try {
      host.notify(answerNotice(await ctx.rest<unknown>(path, { method: 'POST', body: { choice } })))
    } catch {
      host.notify(ANSWER_FAILED)
    } finally {
      answering.set(null)
      void refresh()
    }
  }

  const refreshIsDue = (now: number): boolean =>
    open.get() || waitingCount(state.get()) > 0 || now - loadedAt >= IDLE_REFRESH_MS

  return { state, open, answering, refresh, answer, refreshIsDue }
}

const STYLES = {
  chip: { display: 'inline-flex', alignItems: 'center', gap: '6px' },
  popover: { width: 'min(340px, calc(100vw - 24px))', padding: 0 },
  panel: {
    padding: '12px', display: 'flex', flexDirection: 'column', gap: '12px', overflowY: 'auto', overscrollBehavior: 'contain',
    maxHeight: 'min(560px, calc(var(--radix-popover-content-available-height, 100vh) - 16px))'
  },
  heading: { fontSize: '12px', fontWeight: 600, color: 'var(--ui-text-primary)' },
  muted: { fontSize: '11px', lineHeight: '16px', color: 'var(--ui-text-tertiary)' },
  section: { display: 'flex', flexDirection: 'column', gap: '8px' },
  card: {
    display: 'flex', flexDirection: 'column', gap: '8px', padding: '8px',
    borderRadius: '4px', boxShadow: 'inset 0 0 0 1px var(--ui-stroke-secondary)'
  },
  subject: {
    fontFamily: 'var(--font-mono, monospace)', fontSize: '11px', lineHeight: '16px',
    color: 'var(--ui-text-primary)', wordBreak: 'break-word', maxHeight: '160px', overflowY: 'auto'
  },
  choices: { display: 'flex', flexWrap: 'wrap', gap: '6px' },
  command: {
    fontFamily: 'var(--font-mono, monospace)', fontSize: '11px', padding: '6px 8px', borderRadius: '4px',
    background: 'var(--ui-bg-quaternary)', color: 'var(--ui-text-secondary)', userSelect: 'text', wordBreak: 'break-all'
  },
  row: { display: 'flex', justifyContent: 'space-between', gap: '8px', fontSize: '11px', lineHeight: '16px' },
  rowSubject: {
    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
    fontFamily: 'var(--font-mono, monospace)', color: 'var(--ui-text-secondary)'
  },
  rowOutcome: { flexShrink: 0, color: 'var(--ui-text-tertiary)' }
} satisfies Record<string, CSSProperties>

const useNow = (ticking: boolean): number => {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!ticking) return undefined
    const timer = setInterval(() => setNow(Date.now()), CLOCK_TICK_MS)
    return () => clearInterval(timer)
  }, [ticking])
  return now
}

const Muted = ({ text }: { readonly text: string }): ReactNode => h('div', { style: STYLES.muted }, text)

const SetupNotice = ({ setup, ctx }: { readonly setup: Setup; readonly ctx: PluginContext }): ReactNode => {
  if (!setup.apiKey) {
    return h('div', { style: STYLES.section },
      h(Muted, { text: 'Add your Pushary API key under Settings, Plugins, Pushary.' }),
      h('div', null, h(Button, {
        variant: 'secondary', size: 'xs', onClick: () => void ctx.os.openExternal(SIGN_UP_URL), children: 'Get a key'
      })))
  }
  if (!setup.transport) {
    return h('div', { style: STYLES.section },
      h(Muted, { text: 'Hermes is not sending approvals to Pushary yet. Run this once:' }),
      h('div', { style: STYLES.command }, TRANSPORT_COMMAND),
      h('div', null, h(CopyButton, { text: TRANSPORT_COMMAND, label: 'Copy', showLabel: true, buttonSize: 'xs' })))
  }
  return null
}

interface ApprovalCardProps {
  readonly item: WaitingApproval
  readonly store: ApprovalsStore
  readonly now: number
}

const ChoiceButtons = ({ item, store }: Omit<ApprovalCardProps, 'now'>): ReactNode => {
  const answering = useValue(store.answering)
  const disabled = answering !== null || item.claim !== null
  return h('div', { style: STYLES.choices }, ...item.choices.map(offered => h(Button, {
    key: offered.choice,
    variant: CHOICE_BUTTON_VARIANTS[offered.choice],
    size: 'xs',
    disabled,
    onClick: () => void store.answer(item, offered.choice),
    children: offered.label
  })))
}

const ApprovalCard = ({ item, store, now }: ApprovalCardProps): ReactNode => {
  const answering = useValue(store.answering)
  return h('div', { style: STYLES.card },
    h('div', { style: STYLES.subject }, subjectOf(item.subject)),
    h(Muted, { text: approvalStatusLine(item, now, answering) }),
    item.answerableHere ? h(ChoiceButtons, { item, store }) : null)
}

const RecentRow = ({ item, now }: { readonly item: RecentApproval; readonly now: number }): ReactNode =>
  h('div', { style: STYLES.row },
    h('span', { style: STYLES.rowSubject, title: subjectOf(item.subject) }, subjectOf(item.subject)),
    h('span', { style: STYLES.rowOutcome }, `${outcomeText(item)} · ${ago(now - item.settledAtMs)}`))

interface ReadyPanelProps {
  readonly response: ApprovalsResponse
  readonly store: ApprovalsStore
  readonly ctx: PluginContext
}

interface WaitingListProps {
  readonly response: ApprovalsResponse
  readonly store: ApprovalsStore
  readonly now: number
}

const WaitingList = ({ response, store, now }: WaitingListProps): ReactNode => {
  if (response.waiting.length === 0) {
    return h(Muted, { text: 'No approvals waiting. Answer them here or on your phone, whichever is closer.' })
  }
  return h('div', { style: STYLES.section },
    ...response.waiting.map(item => h(ApprovalCard, { key: item.correlationId, item, store, now })))
}

const RecentList = ({ recent, now }: { readonly recent: readonly RecentApproval[]; readonly now: number }): ReactNode => {
  if (recent.length === 0) return null
  return h('div', { style: STYLES.section },
    h(Muted, { text: 'Recent' }),
    ...recent.map(item => h(RecentRow, { key: item.correlationId, item, now })))
}

const ReadyPanel = ({ response, store, ctx }: ReadyPanelProps): ReactNode => {
  const now = useNow(response.waiting.length > 0)
  const heading = response.waiting.length ? waitingLabel(response.waiting.length) : 'Pushary'
  return h('div', { style: STYLES.panel },
    h('div', { style: STYLES.heading }, heading),
    h(SetupNotice, { setup: response.setup, ctx }),
    h(WaitingList, { response, store, now }),
    h(RecentList, { recent: response.recent, now }))
}

const PANEL_MESSAGES: Readonly<Record<Exclude<PanelState['kind'], 'ready'>, string>> = {
  loading: 'Loading…',
  offline: 'The Pushary backend is not running. Enable Pushary under Capabilities, Plugins, then restart Hermes.',
  incompatible: 'This copy of the Pushary Desktop half does not match its backend. Rescan plugins under Capabilities.'
}

interface StoreProps {
  readonly store: ApprovalsStore
  readonly ctx: PluginContext
}

export const PusharyPanel = ({ store, ctx }: StoreProps): ReactNode => {
  const state = useValue(store.state)
  if (state.kind !== 'ready') return h('div', { style: STYLES.panel }, h(Muted, { text: PANEL_MESSAGES[state.kind] }))
  return h(ReadyPanel, { response: state.response, store, ctx })
}

export const PusharyChip = ({ store, ctx }: StoreProps): ReactNode => {
  const summary = chipSummary(useValue(store.state))
  const open = useValue(store.open)
  const onOpenChange = (value: boolean): void => {
    store.open.set(value)
    if (value) void store.refresh()
  }
  return h(Popover, {
    open,
    onOpenChange,
    children: [
      h(PopoverTrigger, { key: 'trigger', asChild: true, children: h(Button, {
        variant: 'ghost', size: 'micro', 'aria-label': summary.label, title: summary.label,
        children: h('span', { style: STYLES.chip }, h(StatusDot, { tone: summary.tone }), h('span', null, summary.text))
      }) }),
      h(PopoverContent, { key: 'content', side: 'top', align: 'end', 'aria-label': 'Pushary approvals',
        style: STYLES.popover, children: h(PusharyPanel, { store, ctx }) })
    ]
  })
}

const registerPushary = (ctx: PluginContext): void => {
  const store = createApprovalsStore(ctx)
  ctx.register({ id: 'approvals', area: STATUSBAR_AREAS.right, order: 120, render: () => h(PusharyChip, { store, ctx }) })
  ctx.onEvent(APPROVALS_CHANGED_WIRE_EVENT, () => void store.refresh())
  ctx.onEvent(SETTINGS_CHANGED_EVENT, () => void store.refresh())
  ctx.onDispose(host.state.gateway.listen(value => {
    if (value === GATEWAY_OPEN) void store.refresh()
  }))
  ctx.setInterval(() => {
    if (store.refreshIsDue(Date.now())) void store.refresh()
  }, LIVE_REFRESH_MS)
  void store.refresh()
}

const plugin: HermesPlugin = {
  id: PLUGIN_ID,
  name: 'Pushary',
  description: 'Approvals Pushary is carrying to your phone, answerable here too.',
  register: registerPushary
}

export default plugin
