/**
 * Types transcribed from the backend's OpenAPI schema.
 *
 * Written by hand rather than generated, because the surface is small (27
 * routes) and hand-written types let each field carry the note that explains
 * it. Every shape here was read out of `app.openapi()` — none is guessed.
 */

// --- Errors ------------------------------------------------------------------

export interface ErrorDetail {
  code: string
  message: string
  field?: string | null
}

/** The one envelope every backend failure arrives in. */
export interface ErrorBody {
  code: string
  message: string
  correlation_id?: string | null
  details?: ErrorDetail[]
}

export interface ErrorResponse {
  error: ErrorBody
}

// --- Auth --------------------------------------------------------------------

export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type?: string
}

export interface CurrentUser {
  public_id: string
  /** The organization's **public** id. Identity only — never a selector: the
   *  backend derives tenancy from the caller's own row. */
  organization_id: string
  roles: string[]
}

// --- Workflows ---------------------------------------------------------------

export interface WorkflowSummary {
  public_id: string
  name: string
  description: string | null
  active_version_no: number | null
  has_unpublished_changes: boolean
  created_at: string
  updated_at: string
}

export interface Workflow extends WorkflowSummary {
  created_by: string | null
  can_publish: boolean
}

export interface UiPosition {
  x: number
  y: number
}

export interface GraphNode {
  key: string
  type: string
  version: number
  label: string | null
  config: Record<string, unknown>
  ui: UiPosition
}

export interface GraphEdge {
  source: string
  source_handle: string
  target: string
  target_handle: string
}

export interface Graph {
  /** Optimistic lock. A save echoes the revision it read; a stale one is 409. */
  revision: number
  version_no: number | null
  status: string
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface ValidationIssueEdge {
  source: string
  source_handle: string
  target: string
  target_handle: string
}

export interface ValidationIssue {
  code: string
  severity: string
  message: string
  node_key?: string | null
  edge?: ValidationIssueEdge | null
  field?: string | null
}

export interface ValidationReport {
  is_valid: boolean
  issues: ValidationIssue[]
}

export interface PublishResult {
  version_no: number | null
  status: string
  revision: number
  notes: string | null
  published_at: string | null
  created_at: string
  /** **A bearer credential, returned only when a webhook registration is first
   *  created.** Held in memory for one dialog and never persisted. */
  webhook_token?: string | null
}

export interface WorkflowVersion {
  version_no: number | null
  status: string
  revision: number
  notes: string | null
  published_at: string | null
  created_at: string
}

// --- Node catalogue ----------------------------------------------------------

export interface NodeDisplay {
  label: string
  description: string
  icon: string | null
  color: string | null
}

export interface NodeHandle {
  name: string
  type: string
  /** Present on inputs only; outputs are always produced. */
  required?: boolean
  arity?: string
  join?: string
}

export interface NodeType {
  type: string
  version: number
  qualified_name: string
  category: string
  deprecated: boolean
  display: NodeDisplay
  /** JSON Schema generated from the node's Pydantic config model. The backend
   *  is authoritative: the inspector renders known fields and falls back to a
   *  generic form for the rest. */
  config_schema: Record<string, unknown>
  inputs: NodeHandle[]
  outputs: NodeHandle[]
}

export interface NodeCatalog {
  items: NodeType[]
}

// --- Runs --------------------------------------------------------------------

export type RunStatus = 'PENDING' | 'RUNNING' | 'SUSPENDED' | 'COMPLETED' | 'FAILED'

export type NodeExecutionStatus =
  | 'PENDING' | 'RUNNING' | 'WAITING' | 'SUCCEEDED' | 'FAILED' | 'SKIPPED'

export interface RunSummary {
  public_id: string
  workflow_id: string
  version_no: number | null
  status: RunStatus
  error: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string
  updated_at: string
}

export interface NodeExecution {
  public_id: string
  node_key: string
  status: NodeExecutionStatus
  attempt: number
  output: Record<string, unknown> | null
  error: string | null
  /** Non-null while a node waits. The handle a resume call needs. */
  resume_token: string | null
  started_at: string | null
  finished_at: string | null
}

export interface RunDetail extends RunSummary {
  node_executions: NodeExecution[]
}

export interface RunEvent {
  seq: number
  event_type: string
  payload: Record<string, unknown> | null
  created_at: string
}

// --- Knowledge ---------------------------------------------------------------

export interface IngestDocumentRequest {
  external_id: string
  content: string
  title?: string | null
  metadata?: Record<string, string | number | boolean> | null
  // **No organization field, deliberately.** The backend derives the tenant
  // from the caller and rejects the body outright if one is supplied.
}

export interface IngestedDocument {
  document_id: string
  external_id: string
  chunk_count: number
  /** True when the content matched what was already indexed and nothing was
   *  re-embedded. */
  unchanged: boolean
}

// --- Pagination --------------------------------------------------------------

export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}
