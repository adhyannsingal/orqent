import { useMemo } from 'react'
import { Plus } from 'lucide-react'
import { cn } from '@/lib/utils'
import { CATEGORY_LABELS, CATEGORY_ORDER, iconFor, tintFor } from './nodeIcons'
import type { NodeType } from '@/types/api'

/**
 * The palette, rendered entirely from `GET /node-types`.
 *
 * The frontend defines no node types of its own — it groups what the backend
 * ships. A node type added to the catalogue appears here with no frontend
 * change, which is the property the backend's code-only registry exists to
 * give.
 *
 * Both drag-and-drop and click-to-add are wired: dragging is the natural
 * gesture, clicking is the one that always works.
 */
export function NodeLibrary({
  catalogue, onAdd,
}: { catalogue: NodeType[]; onAdd: (descriptor: NodeType) => void }) {
  const grouped = useMemo(() => {
    const byCategory = new Map<string, NodeType[]>()
    for (const type of catalogue) {
      if (type.deprecated) continue
      const list = byCategory.get(type.category) ?? []
      list.push(type)
      byCategory.set(type.category, list)
    }
    const ordered = [...byCategory.entries()].sort(
      ([a], [b]) => CATEGORY_ORDER.indexOf(a) - CATEGORY_ORDER.indexOf(b),
    )
    return ordered
  }, [catalogue])

  return (
    <aside className="flex w-[232px] shrink-0 flex-col border-r border-line bg-surface">
      <div className="flex h-9 items-center border-b border-line px-3">
        <span className="text-[11.5px] font-medium uppercase tracking-wide text-ink-muted">
          Nodes
        </span>
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-2 py-2">
        {grouped.map(([category, types]) => (
          <div key={category} className="mb-3">
            <p className="px-1.5 pb-1 text-[11px] font-medium text-ink-muted">
              {CATEGORY_LABELS[category] ?? category}
            </p>
            {types.map((type) => {
              const Icon = iconFor(type.type, type.category)
              return (
                <button
                  key={type.qualified_name}
                  draggable
                  onDragStart={(event) => {
                    event.dataTransfer.setData('application/orqent-node', type.qualified_name)
                    event.dataTransfer.effectAllowed = 'move'
                  }}
                  onClick={() => onAdd(type)}
                  title={type.display.description}
                  className={cn(
                    'group mb-0.5 flex w-full items-center gap-2 rounded-sm px-1.5 py-1.5',
                    'text-left transition-colors hover:bg-canvas',
                  )}
                >
                  <span className={cn('grid size-6 shrink-0 place-items-center rounded-sm', tintFor(type.category))}>
                    <Icon className="size-3.5" strokeWidth={2} />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[12.5px] font-medium text-ink">
                      {type.display.label}
                    </span>
                    <span className="block truncate text-[11px] text-ink-muted">
                      {type.display.description}
                    </span>
                  </span>
                  <Plus className="size-3 shrink-0 text-ink-muted opacity-0 transition-opacity group-hover:opacity-100" />
                </button>
              )
            })}
          </div>
        ))}
      </div>

      <p className="border-t border-line px-3 py-2 text-[11px] text-ink-muted">
        Drag onto the canvas, or click to add.
      </p>
    </aside>
  )
}
