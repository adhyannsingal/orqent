import {
  Bot, Braces, CircleDot, Clock, FileOutput, GitBranch, Merge, MousePointerClick,
  Pause, Webhook, type LucideIcon,
} from 'lucide-react'

/**
 * Icon and accent per node type.
 *
 * Keyed by the backend's `type` string, with a per-category fallback, so a node
 * type the frontend has never heard of still renders sensibly instead of
 * blanking. The catalogue remains authoritative — this only decides how a known
 * name is drawn.
 */
const BY_TYPE: Record<string, LucideIcon> = {
  'trigger.manual': MousePointerClick,
  'trigger.webhook': Webhook,
  'trigger.schedule': Clock,
  'core.constant': Braces,
  'core.noop': CircleDot,
  'core.log': FileOutput,
  'core.wait': Pause,
  'core.condition': GitBranch,
  'core.merge': Merge,
  'ai.agent': Bot,
}

const BY_CATEGORY: Record<string, LucideIcon> = {
  trigger: MousePointerClick,
  control: GitBranch,
  transform: Braces,
  output: FileOutput,
  action: CircleDot,
  ai: Bot,
}

export function iconFor(type: string, category: string): LucideIcon {
  return BY_TYPE[type] ?? BY_CATEGORY[category] ?? CircleDot
}

/** Category tint. Restrained: a wash behind the icon, never a filled node. */
export function tintFor(category: string): string {
  switch (category) {
    case 'trigger': return 'bg-violet-50 text-violet-700'
    case 'ai': return 'bg-indigo-50 text-indigo-700'
    case 'control': return 'bg-amber-50 text-amber-700'
    case 'output': return 'bg-sky-50 text-sky-700'
    default: return 'bg-canvas text-ink-muted'
  }
}

export const CATEGORY_LABELS: Record<string, string> = {
  trigger: 'Triggers',
  control: 'Control flow',
  transform: 'Transform',
  action: 'Actions',
  output: 'Output',
  ai: 'AI',
}

/** Palette order: what a workflow starts with, then what it does. */
export const CATEGORY_ORDER = ['trigger', 'ai', 'control', 'transform', 'action', 'output']
