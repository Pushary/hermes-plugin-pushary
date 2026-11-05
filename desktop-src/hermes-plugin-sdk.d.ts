declare module '@hermes/plugin-sdk' {
  import type { ComponentType, CSSProperties, ReactNode } from 'react'

  interface ReadableAtom<T> {
    get(): T
    listen(listener: (value: T) => void): () => void
  }

  interface WritableAtom<T> extends ReadableAtom<T> {
    set(value: T): void
  }

  type ButtonVariant = 'default' | 'destructive' | 'outline' | 'secondary' | 'chip' | 'ghost'
  type ButtonSize = 'default' | 'xs' | 'sm' | 'lg' | 'micro'
  type NotificationKind = 'error' | 'warning' | 'info' | 'success'

  export function atom<T>(initial: T): WritableAtom<T>
  export function useValue<T>(store: ReadableAtom<T>): T

  export type StatusTone = 'good' | 'muted' | 'warn' | 'bad'

  export const StatusDot: ComponentType<{ readonly tone: StatusTone }>
  export const Button: ComponentType<{
    readonly variant?: ButtonVariant
    readonly size?: ButtonSize
    readonly disabled?: boolean
    readonly title?: string
    readonly 'aria-label'?: string
    readonly onClick?: () => void
    readonly children?: ReactNode
  }>
  export const CopyButton: ComponentType<{
    readonly text: string
    readonly label?: string
    readonly showLabel?: boolean
    readonly buttonSize?: ButtonSize
  }>
  export const Popover: ComponentType<{
    readonly open?: boolean
    readonly onOpenChange?: (open: boolean) => void
    readonly children?: ReactNode
  }>
  export const PopoverTrigger: ComponentType<{ readonly asChild?: boolean; readonly children?: ReactNode }>
  export const PopoverContent: ComponentType<{
    readonly side?: 'top' | 'bottom' | 'left' | 'right'
    readonly align?: 'start' | 'center' | 'end'
    readonly 'aria-label'?: string
    readonly style?: CSSProperties
    readonly children?: ReactNode
  }>

  export const STATUSBAR_AREAS: { readonly left: string; readonly right: string }

  export const host: {
    notify(input: { readonly kind: NotificationKind; readonly message: string }): void
    readonly state: { readonly gateway: ReadableAtom<string> }
  }

  interface PluginRestOptions {
    readonly method?: string
    readonly body?: unknown
    readonly timeoutMs?: number
  }

  interface PluginContribution {
    readonly id: string
    readonly area: string
    readonly order?: number
    readonly render?: () => ReactNode
  }

  export interface PluginContext {
    register(contribution: PluginContribution): () => void
    rest<T>(path: string, opts?: PluginRestOptions): Promise<T>
    onEvent(type: string, listener: (event: unknown) => void): () => void
    onDispose(fn: () => void): void
    setInterval(fn: () => void, ms: number): () => void
    readonly os: { openExternal(url: string): Promise<boolean> }
  }

  export interface HermesPlugin {
    readonly id: string
    readonly name?: string
    readonly description?: string
    register(ctx: PluginContext): void
  }
}
