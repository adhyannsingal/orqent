/**
 * Status presentation — the one place colour carries meaning in this app.
 *
 * Run and node-execution vocabularies come straight from the backend enums
 * (`RunStatus`, `NodeExecutionStatus`); nothing here invents a state or renames
 * one. An unrecognised value renders neutrally rather than throwing, so a
 * backend that adds a state degrades gracefully.
 */
import { cn } from '@/lib/utils'

type Tone = 'pending' | 'running' | 'succeeded' | 'failed' | 'skipped' | 'suspended'

const TONES: Record<Tone, { dot: string; text: string; bg: string; border: string }> = {
  pending:   { dot: 'bg-status-pending',   text: 'text-status-pending',   bg: 'bg-canvas',  border: 'border-line-strong' },
  running:   { dot: 'bg-status-running',   text: 'text-status-running',   bg: 'bg-blue-50',           border: 'border-blue-200' },
  succeeded: { dot: 'bg-status-succeeded', text: 'text-status-succeeded', bg: 'bg-green-50',          border: 'border-green-200' },
  failed:    { dot: 'bg-status-failed',    text: 'text-status-failed',    bg: 'bg-orange-50',         border: 'border-orange-200' },
  skipped:   { dot: 'bg-status-skipped',   text: 'text-status-skipped',   bg: 'bg-canvas',  border: 'border-line-strong' },
  suspended: { dot: 'bg-status-suspended', text: 'text-status-suspended', bg: 'bg-amber-50',          border: 'border-amber-200' },
}

/** Backend status → visual tone. Both enums map onto the same six tones. */
export function toneOf(status: string): Tone {
  switch (status) {
    case 'RUNNING': return 'running'
    case 'COMPLETED':
    case 'SUCCEEDED': return 'succeeded'
    case 'FAILED': return 'failed'
    case 'SKIPPED': return 'skipped'
    case 'SUSPENDED':
    case 'WAITING': return 'suspended'
    default: return 'pending'
  }
}

/** Sentence case, so `SUCCEEDED` reads as `Succeeded` without a lookup table. */
export function labelOf(status: string): string {
  if (!status) return 'Unknown'
  return status.charAt(0) + status.slice(1).toLowerCase()
}

export function StatusPill({ status, className }: { status: string; className?: string }) {
  const tone = TONES[toneOf(status)]
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5',
        'text-[11.5px] font-medium', tone.bg, tone.border, tone.text, className,
      )}
    >
      <span className={cn('size-1.5 rounded-full', tone.dot)} />
      {labelOf(status)}
    </span>
  )
}

export function StatusDot({ status, className }: { status: string; className?: string }) {
  const tone = TONES[toneOf(status)]
  return <span className={cn('size-2 rounded-full', tone.dot, className)} />
}
