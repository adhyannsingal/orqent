import { StatusPill } from '@/components/ui/status'
import { formatDuration } from '@/lib/utils'
import type { NodeExecution } from '@/types/api'

/**
 * What one node did.
 *
 * Output is rendered as **text**, always. AI output, tool results, and document
 * content are untrusted strings; React escapes them, and nothing here reaches
 * for `dangerouslySetInnerHTML`. Errors come from the backend already
 * sanitised — no provider internals, no credentials — and are shown as-is.
 */
export function NodeExecutionPanel({ execution }: { execution: NodeExecution | null }) {
  if (!execution) {
    return (
      <div className="px-3 py-6 text-center">
        <p className="text-[12.5px] text-ink-muted">
          Select a node to see what it did.
        </p>
      </div>
    )
  }

  return (
    <div className="max-h-[46%] overflow-auto">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2.5">
        <span className="truncate font-mono text-[12px] font-medium">{execution.node_key}</span>
        <StatusPill status={execution.status} className="ml-auto shrink-0" />
      </div>

      <dl className="grid grid-cols-2 gap-x-3 gap-y-1 border-b border-line px-3 py-2 text-[11.5px]">
        <dt className="text-ink-muted">Attempt</dt>
        <dd className="tnum text-right">{execution.attempt}</dd>
        <dt className="text-ink-muted">Duration</dt>
        <dd className="tnum text-right">
          {formatDuration(execution.started_at, execution.finished_at)}
        </dd>
      </dl>

      {execution.error && (
        <div className="border-b border-line px-3 py-2.5">
          <p className="mb-1 text-[11.5px] font-medium uppercase tracking-wide text-status-failed">
            Error
          </p>
          <p className="whitespace-pre-wrap break-words text-[12px] text-ink">
            {execution.error}
          </p>
        </div>
      )}

      {execution.output != null && (
        <div className="px-3 py-2.5">
          <p className="mb-1 text-[11.5px] font-medium uppercase tracking-wide text-ink-muted">
            Output
          </p>
          <Output value={execution.output} />
        </div>
      )}

      {execution.status === 'WAITING' && (
        <p className="px-3 py-2.5 text-[12px] text-status-suspended">
          Waiting to be resumed.
        </p>
      )}
    </div>
  )
}

/**
 * A node's output handles.
 *
 * The common case is `{ main: "…" }` from an agent, so a single string is shown
 * as prose rather than as JSON with escape sequences in it — which is what a
 * demo of an AI answer needs to look like.
 */
function Output({ value }: { value: Record<string, unknown> }) {
  const entries = Object.entries(value)
  if (entries.length === 0) {
    return <p className="text-[12px] text-ink-muted">No output.</p>
  }

  return (
    <div className="space-y-2">
      {entries.map(([handle, payload]) => (
        <div key={handle}>
          {entries.length > 1 && (
            <p className="mb-0.5 font-mono text-[10.5px] text-ink-muted">{handle}</p>
          )}
          <div className="max-h-64 overflow-auto rounded-sm border border-line bg-canvas px-2.5 py-2">
            {typeof payload === 'string' ? (
              <p className="whitespace-pre-wrap break-words text-[12px] leading-relaxed">
                {payload}
              </p>
            ) : (
              <pre className="whitespace-pre-wrap break-words font-mono text-[11.5px] leading-relaxed">
                {JSON.stringify(payload, null, 2)}
              </pre>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}
