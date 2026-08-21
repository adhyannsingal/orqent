import { AlertTriangle, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { ValidationReport } from '@/types/api'

/**
 * Validation results, anchored to the graph.
 *
 * The backend returns issues carrying `node_key` and `edge`, which is what lets
 * a click here select the offending node instead of leaving the author to hunt
 * for it. No validation is reimplemented in the browser — this only presents
 * what `POST /draft/validate` said.
 */
export function ValidationPanel({
  report, onClose, onSelect,
}: { report: ValidationReport; onClose: () => void; onSelect: (nodeKey: string) => void }) {
  const errors = report.issues.filter((issue) => issue.severity !== 'warning')
  const warnings = report.issues.filter((issue) => issue.severity === 'warning')

  return (
    <div className="absolute bottom-3 left-3 right-3 max-h-[42%] overflow-auto rounded-md border border-line-strong bg-surface shadow-sm">
      <div className="sticky top-0 flex items-center gap-2 border-b border-line bg-surface px-3 py-2">
        <AlertTriangle className="size-3.5 text-status-failed" />
        <span className="text-[12.5px] font-medium">
          {errors.length} {errors.length === 1 ? 'problem' : 'problems'}
          {warnings.length > 0 && `, ${warnings.length} warning${warnings.length === 1 ? '' : 's'}`}
        </span>
        <button
          onClick={onClose}
          className="ml-auto grid size-6 place-items-center rounded-sm text-ink-muted hover:bg-canvas"
          aria-label="Dismiss"
        >
          <X className="size-3.5" />
        </button>
      </div>

      <ul>
        {report.issues.map((issue, index) => {
          const target = issue.node_key ?? issue.edge?.source ?? null
          return (
            <li
              key={index}
              onClick={() => target && onSelect(target)}
              className={cn(
                'flex items-start gap-2.5 border-b border-line px-3 py-2 last:border-0',
                target && 'cursor-pointer hover:bg-canvas',
              )}
            >
              <span
                className={cn(
                  'mt-1 size-1.5 shrink-0 rounded-full',
                  issue.severity === 'warning'
                    ? 'bg-status-suspended'
                    : 'bg-status-failed',
                )}
              />
              <div className="min-w-0">
                <p className="text-[12.5px] text-ink">{issue.message}</p>
                <p className="mt-0.5 font-mono text-[11px] text-ink-muted">
                  {issue.code}
                  {target && ` · ${target}`}
                  {issue.field && ` · ${issue.field}`}
                </p>
              </div>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
