import { request } from './client'
import type {
  Graph, Page, PublishResult, ValidationReport, Workflow, WorkflowSummary, WorkflowVersion,
} from '@/types/api'

export const workflowsApi = {
  list: (limit = 50, offset = 0) =>
    request<Page<WorkflowSummary>>(`/api/v1/workflows?limit=${limit}&offset=${offset}`),

  get: (id: string) => request<Workflow>(`/api/v1/workflows/${id}`),

  create: (name: string, description?: string) =>
    request<Workflow>('/api/v1/workflows', {
      method: 'POST',
      body: { name, description: description || null },
    }),

  update: (id: string, patch: { name?: string; description?: string | null }) =>
    request<Workflow>(`/api/v1/workflows/${id}`, { method: 'PATCH', body: patch }),

  remove: (id: string) => request<void>(`/api/v1/workflows/${id}`, { method: 'DELETE' }),

  /** The editable draft. `revision` is the optimistic lock a save must echo. */
  draft: (id: string) => request<Graph>(`/api/v1/workflows/${id}/draft`),

  saveDraft: (id: string, graph: Pick<Graph, 'revision' | 'nodes' | 'edges'>) =>
    request<Graph>(`/api/v1/workflows/${id}/draft`, { method: 'PUT', body: graph }),

  validate: (id: string) =>
    request<ValidationReport>(`/api/v1/workflows/${id}/draft/validate`, { method: 'POST' }),

  publish: (id: string, notes?: string) =>
    request<PublishResult>(`/api/v1/workflows/${id}/publish`, {
      method: 'POST',
      body: { notes: notes || null },
    }),

  /** The frozen graph of one published version — what a run actually executed. */
  versionGraph: (id: string, versionNo: number) =>
    request<Graph>(`/api/v1/workflows/${id}/versions/${versionNo}`),

  versions: (id: string, limit = 20) =>
    request<Page<WorkflowVersion>>(`/api/v1/workflows/${id}/versions?limit=${limit}`),
}
