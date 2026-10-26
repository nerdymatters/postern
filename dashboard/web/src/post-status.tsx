import type { Post } from './api'

const statuses = {
  draft: { label: 'Draft', style: 'border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-950 text-amber-900 dark:text-amber-200' },
  publishing: { label: 'Publishing', style: 'border-sky-200 dark:border-sky-800 bg-sky-50 dark:bg-sky-950 text-sky-900 dark:text-sky-200' },
  published: { label: 'Published', style: 'border-emerald-200 dark:border-emerald-800 bg-emerald-50 dark:bg-emerald-950 text-emerald-900 dark:text-emerald-200' },
  archived: { label: 'Archived', style: 'border-neutral-300 dark:border-neutral-700 bg-neutral-100 dark:bg-neutral-800 text-neutral-600 dark:text-neutral-300' },
}

export function PostStatus({ status }: { status: Post['status'] }) {
  const { label, style } = statuses[status]
  return <span className={`inline-flex shrink-0 rounded-md border px-2 py-1 text-xs font-semibold ${style}`}>{label}</span>
}
