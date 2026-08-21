import { cn } from '@/lib/utils'
import { formatTime } from '@/lib/utils'
import type { RunEvent } from '@/types/api'

/**
 * The run's event log, in sequence.
 *
 * A compact list, not an observability product: the backend's ten event types
 * already tell the story, and the graph above carries the spatial detail.
 */
const TONE: Record<string, string> = {
  RunStarted: 'bg-status-running',
  RunCompleted: 'bg-status-succeeded',
  RunFailed: 'bg-status-failed',
  RunSuspended: 'bg-status-suspended',
  RunResumed: 'bg-status-running',
  NodeStarted: 'bg-status-running',
  NodeSucceeded: 'bg-status-succeeded',
  NodeFailed: 'bg-status-failed',
  NodeSuspended: 'bg-status-suspended',
  NodeSkipped: 'bg-status-skipped',
}

export function EventTimeline({ events }: { events: RunEvent[] }) {
  return (
    <div className="flex h-full flex-col">
      <div className="flex h-8 shrink-0 items-center border-b border-line px-3">
        <span className="text-[11.5px] font-medium uppercase tracking-wide text-ink-muted">
          Timeline
        </span>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {events.length === 0 ? (
          <p className="px-3 py-4 text-[12px] text-ink-muted">No events yet.</p>
        ) : (
          <ul className="px-3 py-2">
            {events.map((event) => {
              const nodeKey = typeof event.payload?.node_key === 'string'
                ? event.payload.node_key
                : null
              return (
                <li key={event.seq} className="flex items-start gap-2 py-1">
                  <span
                    className={cn(
                      'mt-1.5 size-1.5 shrink-0 rounded-full',
                      TONE[event.event_type] ?? 'bg-status-pending',
                    )}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="text-[12px] text-ink">{event.event_type}</span>
                    {nodeKey && (
                      <span className="ml-1.5 font-mono text-[11px] text-ink-muted">
                        {nodeKey}
                      </span>
                    )}
                  </span>
                  <span className="tnum shrink-0 text-[10.5px] text-ink-muted">
                    {formatTime(event.created_at).split(', ')[1] ?? ''}
                  </span>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}
