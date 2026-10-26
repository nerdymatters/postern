import type { ComponentProps, ReactNode } from 'react'
import { PosternWordmark } from './PosternWordmark'

const VARIANTS = {
  primary: 'bg-neutral-900 dark:bg-neutral-100 text-white dark:text-neutral-900 hover:bg-neutral-700 dark:hover:bg-neutral-300 disabled:bg-neutral-300 disabled:text-neutral-600 dark:disabled:bg-neutral-700 dark:disabled:text-neutral-400',
  secondary: 'border border-neutral-300 dark:border-neutral-700 bg-white dark:bg-neutral-900 text-neutral-900 dark:text-neutral-100 hover:bg-neutral-50 dark:hover:bg-neutral-800 disabled:text-neutral-400 dark:disabled:text-neutral-500',
  quiet: 'text-neutral-600 dark:text-neutral-300 hover:text-neutral-900 dark:hover:text-neutral-100 disabled:text-neutral-300 dark:disabled:text-neutral-600',
}

// ComponentProps includes ref, which React 19 passes to function components as a prop.
type ButtonProps = ComponentProps<'button'> & { variant?: keyof typeof VARIANTS }

/** A Button's look, for a link that leads somewhere rather than doing something. */
export function buttonClass(variant: keyof typeof VARIANTS = 'primary', className = '') {
  const padding = variant === 'quiet' ? 'px-1 py-2' : 'px-4 py-3'
  return `inline-flex items-center justify-center gap-2 rounded-xl ${padding} text-base font-medium ${VARIANTS[variant]} ${className}`
}

export function Button({ variant = 'primary', className = '', ...rest }: ButtonProps) {
  return <button {...rest} className={buttonClass(variant, className)} />
}

export function Panel({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <section className={`rounded-2xl border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 p-5 ${className}`}>{children}</section>
}

/** Bilingual copy arrives as "English / ไทย": the Thai goes on its own lang-tagged line, as everywhere else. */
export function Bilingual({ text }: { text: string }) {
  const split = text.search(/ \/ (?=[\u0E00-\u0E7F])/)
  const [en, th] = split < 0 ? [text, ''] : [text.slice(0, split), text.slice(split + 3)]
  return <>{en}{th ? <Thai tone="inherit" className="mt-1">{th}</Thai> : null}</>
}

export function Message({ text, id }: { text: string; id?: string }) {
  if (!text) return null
  return <p id={id} role="alert" className="rounded-xl bg-amber-50 dark:bg-amber-950 px-4 py-3 text-sm text-amber-900 dark:text-amber-200"><Bilingual text={text} /></p>
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="space-y-2">
      <label className="block space-y-2">
        <span className="text-sm font-medium text-neutral-700 dark:text-neutral-300">{label}</span>
        {children}
      </label>
      {hint ? <p className="text-xs text-neutral-500 dark:text-neutral-400">{hint}</p> : null}
    </div>
  )
}

export const input = 'w-full rounded-xl border border-neutral-300 dark:border-neutral-700 bg-white dark:bg-neutral-900 px-4 py-3 text-base outline-none focus:border-neutral-900 dark:focus:border-neutral-100'

export function Centred({ title, children }: { title: ReactNode; children?: ReactNode }) {
  return (
    <main className="mx-auto flex w-full max-w-md flex-col gap-5 px-5 py-16">
      <h1 className="text-2xl font-semibold tracking-tight text-neutral-900 dark:text-neutral-100">{title}</h1>
      {children}
    </main>
  )
}

export function Wordmark() {
  return <PosternWordmark className="block h-6 w-auto self-start text-neutral-900 dark:text-neutral-100" />
}

/** A button's English label with its Thai beneath. Screen readers read both, each in its own
 *  language, as many staff read Thai first. */
export function Label({ en, th }: { en: ReactNode; th: string }) {
  return <span className="min-w-0"><span className="block">{en}</span><span lang="th" className="block text-sm font-normal">{th}</span></span>
}

/** The Thai line under an English one: the same size family, never fainter than secondary text. A size is
 *  a prop, not a class: two size utilities on one element resolve by CSS order, not by the order written. */
export function Thai({ children, className = '', tone = 'muted', size = 'text-sm' }: { children: ReactNode; className?: string; tone?: 'muted' | 'inherit'; size?: 'text-sm' | 'text-base' | 'text-lg' | 'text-xl' }) {
  return <span lang="th" className={`block ${size} ${tone === 'muted' ? 'text-neutral-600 dark:text-neutral-300' : ''} ${className}`}>{children}</span>
}
