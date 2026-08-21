import { useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Background, BackgroundVariant, Controls, ReactFlow, ReactFlowProvider,
} from '@xyflow/react'
import { ArrowLeft, PlayCircle, RefreshCw } from 'lucide-react'
import { toast } from 'sonner'
import { runsApi } from '@/api/runs'
import { workflowsApi } from '@/api/workflows'
import { nodeTypesApi } from '@/api/nodeTypes'
import { messageOf } from '@/api/client'
import { Button, Card, Spinner } from '@/components/ui/primitives'
import { StatusPill } from '@/components/ui/status'
import { OrqentNode } from '@/components/workflow/OrqentNode'
import { EventTimeline } from '@/features/runs/EventTimeline'
import { NodeExecutionPanel } from '@/features/runs/NodeExecutionPanel'
import { formatDuration, formatTime } from '@/lib/utils'
import type { FlowNode } from '@/stores/builder'
import type { RunDetail } from '@/types/api'

const NODE_TYPES = { orqent: OrqentNode }
const TERMINAL = new Set(['COMPLETED', 'FAILED'])

export function RunDetailPage() {
  return (
    <ReactFlowProvider>
      <RunInspector />
    </ReactFlowProvider>
  )
}

function RunInspector() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [chosenKey, setChosenKey] = useState<string | null>(null)

  const run = useQuery({
    queryKey: ['run', id],
    queryFn: () => runsApi.get(id),
    /**
     * Poll while the run is live, stop the moment it is terminal.
     *
     * Polling rather than SSE because the backend exposes no stream; stopping
     * on a terminal state matters because a finished run is immutable and a
     * dashboard left open would otherwise poll it forever.
     */
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status && TERMINAL.has(status) ? false : 1500
    },
  })

  const catalogue = useQuery({
    queryKey: ['node-types'],
    queryFn: () => nodeTypesApi.list(),
    staleTime: Infinity,
  })

  // The graph as published, so the canvas shows the shape that actually ran.
  const versionNo = run.data?.version_no ?? null
  const graph = useQuery({
    queryKey: ['version-graph', run.data?.workflow_id, versionNo],
    queryFn: () => workflowsApi.versionGraph(run.data!.workflow_id, versionNo!),
    enabled: Boolean(run.data?.workflow_id && versionNo),
  })

  const workflow = useQuery({
    queryKey: ['workflow', run.data?.workflow_id],
    queryFn: () => workflowsApi.get(run.data!.workflow_id),
    enabled: Boolean(run.data?.workflow_id),
  })

  const events = useQuery({
    queryKey: ['run-events', id],
    queryFn: () => runsApi.events(id),
    refetchInterval: run.data && TERMINAL.has(run.data.status) ? false : 2500,
  })

  const advance = useMutation({
    mutationFn: () => runsApi.advance(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['run', id] }),
    onError: (error) => toast.error(messageOf(error, 'Could not advance the run.')),
  })

  const resume = useMutation({
    mutationFn: (token: string) => runsApi.resume(id, token),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['run', id] })
      toast.success('Run resumed')
    },
    onError: (error) => toast.error(messageOf(error, 'Could not resume the run.')),
  })

  // Default to the node someone most likely opened this page to inspect:
  // failures first, then suspended work, then the last node that produced output.
  // Derived during render so an explicit click is never overwritten by an effect.
  const selectedKey =
    chosenKey ??
    run.data?.node_executions.find((item) => item.status === 'FAILED')?.node_key ??
    run.data?.node_executions.find((item) => item.resume_token)?.node_key ??
    [...(run.data?.node_executions ?? [])].reverse().find((item) => item.output != null)?.node_key ??
    null

  // Overlay execution state onto the published graph.
  const nodes: FlowNode[] = useMemo(() => {
    if (!graph.data || !catalogue.data) return []
    const byQualified = new Map(
      catalogue.data.items.map((type) => [`${type.type}@${type.version}`, type]),
    )
    const executionByKey = new Map(
      (run.data?.node_executions ?? []).map((execution) => [execution.node_key, execution]),
    )
    return graph.data.nodes.map((node) => ({
      id: node.key,
      type: 'orqent' as const,
      position: { x: node.ui?.x ?? 0, y: node.ui?.y ?? 0 },
      selected: node.key === selectedKey,
      data: {
        nodeKey: node.key,
        type: node.type,
        version: node.version,
        label: node.label,
        config: node.config ?? {},
        descriptor: byQualified.get(`${node.type}@${node.version}`) ?? null,
        runStatus: executionByKey.get(node.key)?.status,
      },
    }))
  }, [graph.data, catalogue.data, run.data, selectedKey])

  const edges = useMemo(
    () =>
      (graph.data?.edges ?? []).map((edge) => ({
        id: `${edge.source}:${edge.source_handle}->${edge.target}:${edge.target_handle}`,
        source: edge.source,
        target: edge.target,
        sourceHandle: edge.source_handle,
        targetHandle: edge.target_handle,
        label: edge.source_handle === 'true' || edge.source_handle === 'false'
          ? edge.source_handle
          : undefined,
      })),
    [graph.data],
  )

  // A suspended run exposes the waiting node's token; that is what resume needs.
  const waiting = run.data?.node_executions.find((execution) => execution.resume_token)

  if (run.isLoading) return <Spinner label="Loading run" />
  if (run.isError) {
    return (
      <div className="grid h-full place-items-center px-6 text-center">
        <div>
          <p className="text-[14px] font-medium">Could not open this run</p>
          <p className="mt-1 text-[12.5px] text-ink-muted">{messageOf(run.error)}</p>
          <Button className="mt-4" onClick={() => navigate('/runs')}>Back to runs</Button>
        </div>
      </div>
    )
  }

  const detail = run.data as RunDetail
  const execution = detail.node_executions.find((item) => item.node_key === selectedKey) ?? null
  const live = !TERMINAL.has(detail.status)

  return (
    <div className="flex h-full flex-col">
      <header className="flex h-12 shrink-0 items-center gap-3 border-b border-line bg-surface px-3">
        <button
          onClick={() => navigate('/runs')}
          className="grid size-7 place-items-center rounded-sm text-ink-muted hover:bg-canvas hover:text-ink"
          aria-label="Back to runs"
        >
          <ArrowLeft className="size-4" />
        </button>

        <div className="min-w-0">
          <h1 className="truncate text-[13.5px] font-semibold tracking-tight">
            {workflow.data?.name ?? 'Run'}
          </h1>
          <p className="truncate font-mono text-[11px] text-ink-muted">
            {detail.version_no ? `Published v${detail.version_no}` : 'Execution'}
          </p>
        </div>

        <StatusPill status={detail.status} className="ml-1" />
        {live && (
          <span className="flex items-center gap-1.5 text-[11.5px] text-ink-muted">
            <RefreshCw className="size-3 animate-spin" />
            live
          </span>
        )}

        <div className="ml-auto flex items-center gap-1.5">
          <span className="tnum mr-1 text-[12px] text-ink-muted">
            {formatDuration(detail.started_at, detail.finished_at)}
          </span>
          {waiting && (
            <Button
              size="sm"
              variant="primary"
              loading={resume.isPending}
              onClick={() => resume.mutate(waiting.resume_token!)}
            >
              <PlayCircle className="size-3.5" />
              Resume
            </Button>
          )}
          {detail.status === 'PENDING' && (
            <Button size="sm" loading={advance.isPending} onClick={() => advance.mutate()}>
              Advance
            </Button>
          )}
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <div className="relative min-w-0 flex-1">
          {graph.isLoading && <Spinner label="Loading graph" />}
          {nodes.length > 0 && (
            <ReactFlow<FlowNode>
              nodes={nodes}
              edges={edges}
              nodeTypes={NODE_TYPES}
              onNodeClick={(_, node) => setChosenKey((node as FlowNode).data.nodeKey)}
              onPaneClick={() => setChosenKey(null)}
              nodesDraggable={false}
              nodesConnectable={false}
              edgesFocusable={false}
              fitView
              minZoom={0.25}
              maxZoom={1.75}
              proOptions={{ hideAttribution: true }}
              className="bg-canvas"
            >
              <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="var(--color-flow-grid)" />
              <Controls showInteractive={false} className="!shadow-none" />
            </ReactFlow>
          )}

          {detail.error && (
            <Card className="absolute bottom-3 left-3 right-3 border-orange-200 bg-orange-50 p-3">
              <p className="text-[12.5px] font-medium text-status-failed">Run failed</p>
              <p className="mt-0.5 text-[12px] text-ink">{detail.error}</p>
            </Card>
          )}
        </div>

        <aside className="flex w-[360px] shrink-0 flex-col border-l border-line bg-surface">
          <NodeExecutionPanel execution={execution} />
          <div className="min-h-0 flex-1 border-t border-line">
            <EventTimeline events={events.data?.items ?? []} />
          </div>
          <div className="border-t border-line px-3 py-2">
            <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11.5px]">
              <dt className="text-ink-muted">Started</dt>
              <dd className="tnum text-right">{formatTime(detail.started_at)}</dd>
              <dt className="text-ink-muted">Finished</dt>
              <dd className="tnum text-right">{formatTime(detail.finished_at)}</dd>
            </dl>
          </div>
        </aside>
      </div>
    </div>
  )
}
