import { request } from './client'
import type { NodeCatalog } from '@/types/api'

export const nodeTypesApi = {
  /** The catalogue is the contract between backend and builder. The frontend
   *  invents no node types and renders only what this returns. */
  list: () => request<NodeCatalog>('/api/v1/node-types'),
}
