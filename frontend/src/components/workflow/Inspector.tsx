import { Trash2 } from 'lucide-react'
import { Button, Field, Input, Textarea } from '@/components/ui/primitives'
import { cn } from '@/lib/utils'
import { iconFor, tintFor } from './nodeIcons'
import type { NodeType, ValidationIssue } from '@/types/api'
import type { FlowNode } from '@/stores/builder'

/**
 * The right-hand properties panel.
 *
 * Node editing happens **here, never in a modal** — a dialog would hide the
 * graph at exactly the moment the graph is the context you need.
 *
 * Purpose-built forms exist for the nodes that carry real configuration; every
 * other node type falls back to a form generated from the catalogue's JSON
 * Schema. The backend's schema stays authoritative either way: a hand-written
 * form renders the same field names the schema declares, and adding a node type
 * never leaves the inspector blank.
 */
export function Inspector({
  node, issues, onConfigChange, onDelete,
}: {
  node: FlowNode | null
  issues: ValidationIssue[]
  onConfigChange: (config: Record<string, unknown>) => void
  onDelete: () => void
}) {
  if (!node) {
    return (
      <aside className="flex w-[320px] shrink-0 flex-col border-l border-line bg-surface">
        <PanelHeader title="Properties" />
        <div className="flex flex-1 items-center justify-center px-6 text-center">
          <p className="text-[12.5px] text-ink-muted">
            Select a node to configure it.
          </p>
        </div>
      </aside>
    )
  }

  const descriptor = node.data.descriptor
  const config = node.data.config
  const Icon = iconFor(node.data.type, descriptor?.category ?? 'action')
  const set = (patch: Record<string, unknown>) => onConfigChange({ ...config, ...patch })
  const nodeIssues = issues.filter((issue) => issue.node_key === node.data.nodeKey)

  return (
    <aside className="flex w-[320px] shrink-0 flex-col border-l border-line bg-surface">
      <PanelHeader title="Properties" />

      <div className="min-h-0 flex-1 overflow-auto">
        <div className="flex items-start gap-2.5 border-b border-line p-3">
          <span className={cn('grid size-7 shrink-0 place-items-center rounded-sm', tintFor(descriptor?.category ?? 'action'))}>
            <Icon className="size-4" strokeWidth={2} />
          </span>
          <div className="min-w-0">
            <p className="truncate text-[13px] font-medium">
              {descriptor?.display.label ?? node.data.type}
            </p>
            <p className="truncate font-mono text-[11px] text-ink-muted">
              {node.data.type}@{node.data.version} · {node.data.nodeKey}
            </p>
          </div>
        </div>

        {nodeIssues.length > 0 && (
          <div className="border-b border-orange-200 bg-orange-50 px-3 py-2.5">
            {nodeIssues.map((issue, index) => (
              <p key={index} className="text-[12px] text-status-failed">
                {issue.message}
              </p>
            ))}
          </div>
        )}

        <div className="space-y-3.5 p-3">
          <NodeConfigForm type={node.data.type} descriptor={descriptor} config={config} set={set} />
        </div>
      </div>

      <div className="border-t border-line p-2">
        <Button variant="ghost" onClick={onDelete} className="w-full justify-start text-status-failed">
          <Trash2 className="size-3.5" />
          Delete node
        </Button>
      </div>
    </aside>
  )
}

function PanelHeader({ title }: { title: string }) {
  return (
    <div className="flex h-9 items-center border-b border-line px-3">
      <span className="text-[11.5px] font-medium uppercase tracking-wide text-ink-muted">
        {title}
      </span>
    </div>
  )
}

// --- Per-type forms ----------------------------------------------------------

function NodeConfigForm({
  type, descriptor, config, set,
}: {
  type: string
  descriptor: NodeType | null
  config: Record<string, unknown>
  set: (patch: Record<string, unknown>) => void
}) {
  switch (type) {
    case 'ai.agent': return <AgentForm config={config} set={set} />
    case 'core.condition': return <ConditionForm config={config} set={set} />
    case 'trigger.schedule': return <ScheduleForm config={config} set={set} />
    case 'core.constant':
      return (
        <Field label="Value" hint="emitted as Text">
          <Textarea
            rows={4}
            value={String(config.value ?? '')}
            onChange={(event) => set({ value: event.target.value })}
            placeholder="Text this node emits"
          />
        </Field>
      )
    case 'core.log':
      return (
        <Field label="Level">
          <Select
            value={String(config.level ?? 'info')}
            onChange={(value) => set({ level: value })}
            options={[
              { value: 'debug', label: 'Debug' },
              { value: 'info', label: 'Info' },
              { value: 'warning', label: 'Warning' },
              { value: 'error', label: 'Error' },
            ]}
          />
        </Field>
      )
    case 'trigger.webhook':
      return (
        <Note>
          Publishing this workflow creates its webhook address. The URL is shown
          once, when it is created.
        </Note>
      )
    case 'trigger.manual':
      return <Note>Started by hand, or by <span className="font-medium">Run</span> in the toolbar.</Note>
    default:
      return <GenericForm descriptor={descriptor} config={config} set={set} />
  }
}

/**
 * The AI agent form.
 *
 * Every field maps to `AgentConfig`. What is deliberately **absent** is as
 * important: no Gemini model id, no API key, no embedding model, no Chroma
 * collection, and no organization. A workflow names a model *profile*, and
 * tenancy is the backend's to decide.
 */
function AgentForm({
  config, set,
}: { config: Record<string, unknown>; set: (patch: Record<string, unknown>) => void }) {
  const retrieval = config.retrieval as { top_k?: number } | null | undefined
  const tools = Array.isArray(config.tools) ? (config.tools as string[]) : []
  const temperature = typeof config.temperature === 'number' ? config.temperature : 0

  return (
    <>
      <Field label="Instructions" hint="system prompt">
        <Textarea
          rows={6}
          value={String(config.instructions ?? '')}
          onChange={(event) => set({ instructions: event.target.value })}
          placeholder="You are a support assistant. Answer briefly and cite the policy."
        />
      </Field>

      <Field label="Model" hint="resolved by the deployment">
        <Select
          value={String(config.model ?? 'default')}
          onChange={(value) => set({ model: value })}
          options={[{ value: 'default', label: 'Default' }]}
        />
      </Field>

      <Field label="Temperature" hint={temperature.toFixed(1)}>
        <input
          type="range"
          min={0}
          max={2}
          step={0.1}
          value={temperature}
          onChange={(event) => set({ temperature: Number(event.target.value) })}
          className="w-full accent-accent"
        />
        <p className="mt-0.5 text-[11px] text-ink-muted">
          0 is as reproducible as the provider offers.
        </p>
      </Field>

      <div className="border-t border-line pt-3">
        <Toggle
          label="Retrieval"
          description="Ground answers in your workspace's knowledge."
          checked={retrieval != null}
          onChange={(on) => set({ retrieval: on ? { top_k: 5 } : null })}
        />
        {retrieval != null && (
          <div className="mt-2.5 pl-0.5">
            <Field label="Documents to retrieve" hint="1–20">
              <Input
                type="number"
                min={1}
                max={20}
                value={retrieval.top_k ?? 5}
                onChange={(event) => {
                  // Bounded to the backend's own range, so an out-of-range
                  // value is impossible rather than merely rejected later.
                  const raw = Number(event.target.value)
                  const bounded = Math.max(1, Math.min(20, Number.isFinite(raw) ? raw : 5))
                  set({ retrieval: { top_k: bounded } })
                }}
              />
            </Field>
          </div>
        )}
      </div>

      <div className="border-t border-line pt-3">
        <p className="mb-1.5 text-[12px] font-medium">Tools</p>
        <Toggle
          label="Calculator"
          description="Exact arithmetic, on request."
          checked={tools.includes('calculator')}
          onChange={(on) =>
            set({ tools: on ? [...new Set([...tools, 'calculator'])] : tools.filter((t) => t !== 'calculator') })
          }
        />
      </div>
    </>
  )
}

function ConditionForm({
  config, set,
}: { config: Record<string, unknown>; set: (patch: Record<string, unknown>) => void }) {
  const operator = String(config.operator ?? 'is_empty')
  return (
    <>
      <Field label="Path" hint="dotted, empty = whole value">
        <Input
          value={String(config.path ?? '')}
          onChange={(event) => set({ path: event.target.value })}
          placeholder="customer.tier"
          className="font-mono"
        />
      </Field>
      <Field label="Operator">
        <Select
          value={operator}
          onChange={(value) => set({ operator: value })}
          options={[
            { value: 'equals', label: 'equals' },
            { value: 'not_equals', label: 'does not equal' },
            { value: 'greater_than', label: 'greater than' },
            { value: 'less_than', label: 'less than' },
            { value: 'contains', label: 'contains' },
            { value: 'is_empty', label: 'is empty' },
          ]}
        />
      </Field>
      {operator !== 'is_empty' && (
        <Field label="Value">
          <Input
            value={String(config.value ?? '')}
            onChange={(event) => set({ value: event.target.value })}
            placeholder="gold"
          />
        </Field>
      )}
      <Note>
        Exactly one branch carries a value. The other is pruned, and everything
        downstream of it is skipped.
      </Note>
    </>
  )
}

const CRON_EXAMPLES = [
  { cron: '*/5 * * * *', label: 'Every 5 minutes' },
  { cron: '0 * * * *', label: 'Hourly' },
  { cron: '0 9 * * *', label: 'Daily at 09:00' },
  { cron: '0 9 * * 1', label: 'Mondays at 09:00' },
]

function ScheduleForm({
  config, set,
}: { config: Record<string, unknown>; set: (patch: Record<string, unknown>) => void }) {
  return (
    <>
      <Field label="Cron" hint="UTC">
        <Input
          value={String(config.cron ?? '')}
          onChange={(event) => set({ cron: event.target.value })}
          placeholder="0 9 * * *"
          className="font-mono"
        />
      </Field>
      <div className="flex flex-wrap gap-1">
        {CRON_EXAMPLES.map((example) => (
          <button
            key={example.cron}
            onClick={() => set({ cron: example.cron })}
            className={cn(
              'rounded-xs border border-line-strong px-1.5 py-0.5',
              'text-[11px] text-ink-muted hover:bg-canvas hover:text-ink',
            )}
          >
            {example.label}
          </button>
        ))}
      </div>
      <Note>Schedules are evaluated in UTC. The dispatcher fires the next occurrence.</Note>
    </>
  )
}

/**
 * Fallback for node types with no purpose-built form.
 *
 * Rendered from the catalogue's JSON Schema, so a node type this frontend has
 * never seen is still configurable rather than a dead end.
 */
function GenericForm({
  descriptor, config, set,
}: {
  descriptor: NodeType | null
  config: Record<string, unknown>
  set: (patch: Record<string, unknown>) => void
}) {
  const properties = (descriptor?.config_schema?.properties ?? {}) as Record<
    string, { type?: string; description?: string; enum?: string[] }
  >
  const fields = Object.entries(properties)

  if (fields.length === 0) {
    return <Note>This node has no configuration.</Note>
  }

  return (
    <>
      {fields.map(([name, schema]) => (
        <Field key={name} label={name} hint={schema.type}>
          {schema.enum ? (
            <Select
              value={String(config[name] ?? schema.enum[0])}
              onChange={(value) => set({ [name]: value })}
              options={schema.enum.map((option) => ({ value: option, label: option }))}
            />
          ) : (
            <Input
              type={schema.type === 'number' || schema.type === 'integer' ? 'number' : 'text'}
              value={String(config[name] ?? '')}
              onChange={(event) =>
                set({
                  [name]: schema.type === 'number' || schema.type === 'integer'
                    ? Number(event.target.value)
                    : event.target.value,
                })
              }
            />
          )}
        </Field>
      ))}
    </>
  )
}

// --- Small controls ----------------------------------------------------------

function Select({
  value, onChange, options,
}: {
  value: string
  onChange: (value: string) => void
  options: { value: string; label: string }[]
}) {
  return (
    <select
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className={cn(
        'h-8 w-full rounded-sm border border-line-strong bg-surface',
        'px-2 text-[13px] focus:border-accent focus:outline-none',
        'focus:ring-2 focus:ring-accent/15',
      )}
    >
      {options.map((option) => (
        <option key={option.value} value={option.value}>{option.label}</option>
      ))}
    </select>
  )
}

function Toggle({
  label, description, checked, onChange,
}: { label: string; description?: string; checked: boolean; onChange: (checked: boolean) => void }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      className="flex w-full items-start gap-2.5 text-left"
    >
      <span
        className={cn(
          'mt-0.5 flex h-4 w-7 shrink-0 items-center rounded-full p-0.5 transition-colors',
          checked ? 'bg-accent' : 'bg-line-strong',
        )}
      >
        <span
          className={cn(
            'size-3 rounded-full bg-white transition-transform',
            checked && 'translate-x-3',
          )}
        />
      </span>
      <span className="min-w-0">
        <span className="block text-[12.5px] font-medium text-ink">{label}</span>
        {description && (
          <span className="block text-[11px] text-ink-muted">{description}</span>
        )}
      </span>
    </button>
  )
}

function Note({ children }: { children: React.ReactNode }) {
  return (
    <p className="rounded-sm bg-canvas px-2.5 py-2 text-[11.5px] leading-relaxed text-ink-muted">
      {children}
    </p>
  )
}
