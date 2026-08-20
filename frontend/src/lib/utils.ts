import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Parse a backend timestamp as UTC.
 *
 * The API serialises naive UTC datetimes (`2026-08-20T12:04:11.123456`) with no
 * offset, and `new Date()` reads an offset-less string as *local* time — which
 * showed every freshly-created workflow as "5h ago" in IST. Appending `Z` when
 * no zone is present is what makes the displayed time correct anywhere.
 */
function parseUtc(value: string): number {
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value)
  return new Date(hasZone ? value : `${value}Z`).getTime()
}

/** Compact absolute time. Runs are inspected minutes later, not months. */
export function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  const millis = parseUtc(value)
  if (Number.isNaN(millis)) return '—'
  return new Date(millis).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return '—'
  const then = parseUtc(value)
  if (Number.isNaN(then)) return '—'
  const seconds = Math.round((Date.now() - then) / 1000)
  if (seconds < 60) return `${Math.max(seconds, 0)}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

/** Duration between two instants, for a finished node or run. */
export function formatDuration(from: string | null, to: string | null): string {
  if (!from || !to) return '—'
  const ms = parseUtc(to) - parseUtc(from)
  if (Number.isNaN(ms) || ms < 0) return '—'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`
}
