import { request } from './client'
import type { IngestDocumentRequest, IngestedDocument } from '@/types/api'

export const documentsApi = {
  /**
   * Add or replace a document in the caller's organization corpus.
   *
   * The tenant is **not** a parameter: the backend derives it from the caller
   * and rejects a body that names one. Re-sending the same `external_id`
   * replaces that document rather than creating a second copy.
   */
  ingest: (payload: IngestDocumentRequest) =>
    request<IngestedDocument>('/api/v1/documents', { method: 'POST', body: payload }),
}
