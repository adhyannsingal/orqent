import { request } from './client'
import type { Page, RunDetail, RunEvent, RunSummary } from '@/types/api'

export const runsApi = {
  list: (limit = 50, offset = 0) =>
    request<Page<RunSummary>>(`/api/v1/runs?limit=${limit}&offset=${offset}`),

  get: (id: string) => request<RunDetail>(`/api/v1/runs/${id}`),

  create: (workflowId: string, triggerPayload?: Record<string, unknown> | null) =>
    request<RunDetail>('/api/v1/runs', {
      method: 'POST',
      body: { workflow_id: workflowId, trigger_payload: triggerPayload ?? null },
    }),

  /**
   * Drive a run forward in the request itself.
   *
   * A deployment running the Compose worker advances runs on its own, so this
   * is only used as an explicit nudge when a run is still PENDING — useful when
   * someone is running the API alone.
   */
  advance: (id: string) => request<RunDetail>(`/api/v1/runs/${id}/advance`, { method: 'POST' }),

  resume: (id: string, resumeToken: string) =>
    request<RunDetail>(`/api/v1/runs/${id}/resume`, {
      method: 'POST',
      body: { resume_token: resumeToken },
    }),

  events: (id: string, limit = 200) =>
    request<Page<RunEvent>>(`/api/v1/runs/${id}/events?limit=${limit}`),
}
