import { memo } from 'react'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import { cn } from '@/lib/utils'
import { iconFor, tintFor } from './nodeIcons'
import { toneOf } from '@/components/ui/status'
import type { FlowNode } from '@/stores/builder'

const STATUS_DOT: Record<string, string> = {
  pending: 'bg-status-pending',
  running: 'bg-status-running',
  succeeded: 'bg-status-succeeded',
  failed: 'bg-status-failed',
  skipped: 'bg-status-skipped',
  suspended: 'bg-status-suspended',
}

/**
 * One node on the canvas.
 *
 * Compact (~200px) and information-dense: label, qualified type, and the real
 * handle names from the catalogue. Handles are laid out from the descriptor
 * rather than assumed, which is what makes a two-output Condition and a
 * two-input Merge render correctly without special-casing either.
 *
 * The same component draws editor and run states — a run simply supplies
 * `runStatus`, and the border and dot follow it.
 */
export const OrqentNode = memo(function OrqentNode({ data, selected }: NodeProps<FlowNode>) {
  const descriptor = data.descriptor
  const category = descriptor?.category ?? 'action'
  const Icon = iconFor(data.type, category)
  const inputs = descriptor?.inputs ?? []
  const outputs = descriptor?.outputs ?? []
  const status = data.runStatus
  const tone = status ? toneOf(status) : null

  return (
    <div
      className={cn(
        'w-[204px] rounded-md border bg-surface transition-shadow',
        selected
          ? 'border-accent ring-2 ring-accent/20'
          : 'border-line-strong',
        status === 'RUNNING' && 'border-status-running ring-2 ring-blue-200',
        status === 'FAILED' && 'border-status-failed',
        status === 'SUCCEEDED' && 'border-green-300',
        status === 'SKIPPED' && 'opacity-55',
        status === 'WAITING' && 'border-status-suspended',
      )}
    >
      {inputs.map((handle, index) => (
        <Handle
          key={handle.name}
          type="target"
          position={Position.Left}
          id={handle.name}
          style={{ top: 34 + index * 16 }}
        />
      ))}

      <div className="flex items-start gap-2.5 px-2.5 py-2">
        <div className={cn('mt-0.5 grid size-6 shrink-0 place-items-center rounded-sm', tintFor(category))}>
          <Icon className="size-3.5" strokeWidth={2} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span className="truncate text-[12.5px] font-medium leading-tight text-ink">
              {data.label || descriptor?.display.label || data.type}
            </span>
            {tone && (
              <span
                className={cn(
                  'ml-auto size-1.5 shrink-0 rounded-full',
                  // Looked up rather than interpolated: Tailwind scans source
                  // statically, so a class name built at runtime is never
                  // generated and the dot would simply be invisible.
                  STATUS_DOT[tone],
                  status === 'RUNNING' && 'animate-pulse',
                )}
              />
            )}
          </div>
          <span className="block truncate font-mono text-[10.5px] text-ink-muted">
            {data.nodeKey}
          </span>
        </div>
      </div>

      {outputs.length > 0 && (
        <div className="flex items-center justify-end gap-2 border-t border-line px-2.5 py-1">
          {outputs.map((handle) => (
            <span key={handle.name} className="font-mono text-[10px] text-ink-muted">
              {handle.name}
            </span>
          ))}
        </div>
      )}

      {outputs.map((handle, index) => (
        <Handle
          key={handle.name}
          type="source"
          position={Position.Right}
          id={handle.name}
          style={{
            // Multiple outputs (Condition's true/false) spread down the edge;
            // a single output sits at the node's midpoint.
            top: outputs.length === 1 ? 34 : 28 + index * 20,
          }}
        />
      ))}
    </div>
  )
})
