import { create } from 'zustand'
import type { Edge, Node } from '@xyflow/react'
import { addEdge, applyEdgeChanges, applyNodeChanges } from '@xyflow/react'
import type { EdgeChange, NodeChange, Connection } from '@xyflow/react'
import type { Graph, GraphEdge, GraphNode, NodeType } from '@/types/api'

/**
 * Editor state for the builder canvas.
 *
 * **Only editor state.** The workflow itself, its draft, and the node catalogue
 * are server state and live in TanStack Query; duplicating them here would
 * create two sources of truth that drift the moment a save returns a new
 * revision. What this holds is the things the server has never heard of: what
 * is on the canvas right now, what is selected, and whether there is anything
 * worth saving.
 */

export interface OrqentNodeData extends Record<string, unknown> {
  nodeKey: string
  type: string
  version: number
  label: string | null
  config: Record<string, unknown>
  descriptor: NodeType | null
  /** Set only while inspecting a run; absent in the editor. */
  runStatus?: string
}

export type FlowNode = Node<OrqentNodeData, 'orqent'>

interface BuilderState {
  nodes: FlowNode[]
  edges: Edge[]
  /** The optimistic-lock revision the draft was loaded at. */
  revision: number
  selectedKey: string | null
  dirty: boolean

  load: (graph: Graph, catalogue: NodeType[]) => void
  onNodesChange: (changes: NodeChange<FlowNode>[]) => void
  onEdgesChange: (changes: EdgeChange[]) => void
  connect: (connection: Connection) => void
  addNode: (descriptor: NodeType, position: { x: number; y: number }) => void
  updateConfig: (nodeKey: string, config: Record<string, unknown>) => void
  select: (nodeKey: string | null) => void
  setRevision: (revision: number) => void
  markClean: () => void
  toGraph: () => { nodes: GraphNode[]; edges: GraphEdge[] }
}

/**
 * Node keys are the graph's identity and are referenced by every edge, every
 * validation issue, and every node execution. They are generated from the type
 * so they read meaningfully in a run inspector (`agent_1`, not `n_7f3a`).
 */
function nextKey(type: string, existing: FlowNode[]): string {
  const base = type.split('.').pop()?.replace(/[^a-z0-9]/gi, '_').toLowerCase() || 'node'
  let index = 1
  const taken = new Set(existing.map((node) => node.data.nodeKey))
  while (taken.has(`${base}_${index}`)) index += 1
  return `${base}_${index}`
}

const edgeId = (edge: GraphEdge) =>
  `${edge.source}:${edge.source_handle}->${edge.target}:${edge.target_handle}`

export const useBuilder = create<BuilderState>((set, get) => ({
  nodes: [],
  edges: [],
  revision: 0,
  selectedKey: null,
  dirty: false,

  load: (graph, catalogue) => {
    const byQualified = new Map(catalogue.map((type) => [`${type.type}@${type.version}`, type]))
    set({
      revision: graph.revision,
      dirty: false,
      selectedKey: null,
      nodes: graph.nodes.map((node) => ({
        id: node.key,
        type: 'orqent' as const,
        position: { x: node.ui?.x ?? 0, y: node.ui?.y ?? 0 },
        data: {
          nodeKey: node.key,
          type: node.type,
          version: node.version,
          label: node.label,
          config: node.config ?? {},
          descriptor: byQualified.get(`${node.type}@${node.version}`) ?? null,
        },
      })),
      edges: graph.edges.map((edge) => ({
        id: edgeId(edge),
        source: edge.source,
        target: edge.target,
        sourceHandle: edge.source_handle,
        targetHandle: edge.target_handle,
        // Condition branches are labelled, because which one is which is the
        // whole point of a branch.
        label: edge.source_handle === 'true' || edge.source_handle === 'false'
          ? edge.source_handle
          : undefined,
      })),
    })
  },

  onNodesChange: (changes) => {
    const meaningful = changes.some(
      (change) => change.type !== 'select' && change.type !== 'dimensions',
    )
    set((state) => ({
      nodes: applyNodeChanges(changes, state.nodes),
      dirty: state.dirty || meaningful,
    }))
  },

  onEdgesChange: (changes) => {
    const meaningful = changes.some((change) => change.type !== 'select')
    set((state) => ({
      edges: applyEdgeChanges(changes, state.edges),
      dirty: state.dirty || meaningful,
    }))
  },

  connect: (connection) => {
    if (!connection.source || !connection.target) return
    set((state) => ({
      edges: addEdge(
        {
          ...connection,
          id: `${connection.source}:${connection.sourceHandle}->${connection.target}:${connection.targetHandle}`,
          label: connection.sourceHandle === 'true' || connection.sourceHandle === 'false'
            ? connection.sourceHandle
            : undefined,
        },
        state.edges,
      ),
      dirty: true,
    }))
  },

  addNode: (descriptor, position) => {
    const key = nextKey(descriptor.type, get().nodes)
    set((state) => ({
      dirty: true,
      selectedKey: key,
      nodes: [
        ...state.nodes,
        {
          id: key,
          type: 'orqent' as const,
          position,
          data: {
            nodeKey: key,
            type: descriptor.type,
            version: descriptor.version,
            label: null,
            // Empty config: the backend fills defaults from the Pydantic model,
            // and every node type is constructible with no arguments.
            config: {},
            descriptor,
          },
        },
      ],
    }))
  },

  updateConfig: (nodeKey, config) =>
    set((state) => ({
      dirty: true,
      nodes: state.nodes.map((node) =>
        node.data.nodeKey === nodeKey ? { ...node, data: { ...node.data, config } } : node,
      ),
    })),

  select: (nodeKey) => set({ selectedKey: nodeKey }),
  setRevision: (revision) => set({ revision }),
  markClean: () => set({ dirty: false }),

  toGraph: () => {
    const { nodes, edges } = get()
    return {
      nodes: nodes.map((node) => ({
        key: node.data.nodeKey,
        type: node.data.type,
        version: node.data.version,
        label: node.data.label,
        config: node.data.config,
        ui: { x: Math.round(node.position.x), y: Math.round(node.position.y) },
      })),
      edges: edges.map((edge) => ({
        source: edge.source,
        source_handle: edge.sourceHandle ?? 'main',
        target: edge.target,
        target_handle: edge.targetHandle ?? 'main',
      })),
    }
  },
}))
