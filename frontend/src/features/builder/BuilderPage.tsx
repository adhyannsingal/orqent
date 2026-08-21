import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Background, BackgroundVariant, Controls, ReactFlow, ReactFlowProvider,
  useReactFlow, type ReactFlowInstance,
} from '@xyflow/react'
import { ArrowLeft, CheckCircle2, Play, Rocket, Save, ShieldCheck } from 'lucide-react'
import { toast } from 'sonner'
import { workflowsApi } from '@/api/workflows'
import { nodeTypesApi } from '@/api/nodeTypes'
import { runsApi } from '@/api/runs'
import { messageOf } from '@/api/client'
import { Button, Field, Modal, Spinner, Textarea } from '@/components/ui/primitives'
import { NodeLibrary } from '@/components/workflow/NodeLibrary'
import { Inspector } from '@/components/workflow/Inspector'
import { OrqentNode } from '@/components/workflow/OrqentNode'
import { PublishDialog } from '@/features/builder/PublishDialog'
import { ValidationPanel } from '@/features/builder/ValidationPanel'
import { useBuilder, type FlowNode } from '@/stores/builder'
import { cn } from '@/lib/utils'
import type { ErrorDetail, NodeType, PublishResult, ValidationReport } from '@/types/api'

const NODE_TYPES = { orqent: OrqentNode }

export function BuilderPage() {
  return (
    <ReactFlowProvider>
      <Builder />
    </ReactFlowProvider>
  )
}

function Builder() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const canvasRef = useRef<HTMLDivElement>(null)
  const flow = useReactFlow()
  const [instance, setInstance] = useState<ReactFlowInstance<FlowNode> | null>(null)

  const [report, setReport] = useState<ValidationReport | null>(null)
  const [published, setPublished] = useState<PublishResult | null>(null)
  const [runOpen, setRunOpen] = useState(false)
  const [runPayload, setRunPayload] = useState('')
  const [runPayloadError, setRunPayloadError] = useState<string | null>(null)

  const state = useBuilder()

  const workflow = useQuery({ queryKey: ['workflow', id], queryFn: () => workflowsApi.get(id) })
  const catalogue = useQuery({
    queryKey: ['node-types'],
    queryFn: () => nodeTypesApi.list(),
    // The catalogue is a property of the release, not of the session.
    staleTime: Infinity,
  })
  const draft = useQuery({ queryKey: ['draft', id], queryFn: () => workflowsApi.draft(id) })

  const types = useMemo(() => catalogue.data?.items ?? [], [catalogue.data])

  // Load the draft into the editor once both it and the catalogue have arrived,
  // so every node can resolve its descriptor (handles, config schema).
  const loaded = useRef<string | null>(null)
  useEffect(() => {
    if (!draft.data || types.length === 0) return
    const stamp = `${id}:${draft.data.revision}`
    if (loaded.current === stamp) return
    loaded.current = stamp
    state.load(draft.data, types)
  }, [draft.data, types, id, state])

  const save = useMutation({
    mutationFn: async () => {
      const graph = state.toGraph()
      return workflowsApi.saveDraft(id, { revision: state.revision, ...graph })
    },
    onSuccess: (saved) => {
      // The response carries the next revision; the editor keeps its geometry.
      state.setRevision(saved.revision)
      state.markClean()
      void queryClient.invalidateQueries({ queryKey: ['workflow', id] })
      toast.success('Draft saved')
    },
    onError: (error) => toast.error(messageOf(error, 'Could not save the draft.')),
  })

  const validate = useMutation({
    mutationFn: async () => {
      if (state.dirty) await save.mutateAsync()
      return workflowsApi.validate(id)
    },
    onSuccess: (result) => {
      setReport(result)
      if (result.is_valid) toast.success('Graph is valid')
    },
    onError: (error) => toast.error(messageOf(error, 'Could not validate.')),
  })

  const publish = useMutation({
    mutationFn: async () => {
      if (state.dirty) await save.mutateAsync()
      return workflowsApi.publish(id)
    },
    onSuccess: (result) => {
      setPublished(result)
      setReport(null)
      void queryClient.invalidateQueries({ queryKey: ['workflow', id] })
      void queryClient.invalidateQueries({ queryKey: ['draft', id] })
    },
    onError: (error) => {
      // Publish refuses an invalid graph with node-anchored details; surfacing
      // them in the panel is more useful than a toast that vanishes.
      const details = (error as { details?: ErrorDetail[] }).details
      if (details?.length) {
        setReport({
          is_valid: false,
          issues: details.map((detail) => ({
            code: detail.code,
            severity: 'error',
            message: detail.message,
            node_key: detail.field?.split('.')[1] ?? null,
          })),
        })
      }
      toast.error(messageOf(error, 'Could not publish.'))
    },
  })

  const run = useMutation({
    mutationFn: (payload: Record<string, unknown> | null) => runsApi.create(id, payload),
    onSuccess: (created) => {
      setRunOpen(false)
      setRunPayloadError(null)
      navigate(`/runs/${created.public_id}`)
    },
    onError: (error) => toast.error(messageOf(error, 'Could not start a run.')),
  })

  const addNode = useCallback(
    (descriptor: NodeType, position?: { x: number; y: number }) => {
      if (position) {
        state.addNode(descriptor, { x: position.x - 100, y: position.y - 20 })
        return
      }

      // Click-add lays nodes into the visible canvas. Pick the desired screen
      // point first, then convert it through the current React Flow transform.
      const rect = canvasRef.current?.getBoundingClientRect()
      const index = state.nodes.length
      const target = flow.screenToFlowPosition({
        x: (rect?.left ?? 0) + (rect?.width ?? 800) / 2,
        y: index === 0
          ? (rect?.top ?? 0) + 160
          : (rect?.top ?? 0) + 580 + (index - 1) * 120,
      })
      state.addNode(descriptor, {
        x: target.x - 100,
        y: target.y - 40,
      })
    },
    [flow, state],
  )

  const selected = state.nodes.find((node) => node.data.nodeKey === state.selectedKey) ?? null
  const issues = report?.issues ?? []

  if (workflow.isLoading || draft.isLoading || catalogue.isLoading) {
    return <Spinner label="Loading workflow" />
  }
  if (workflow.isError) {
    return (
      <div className="grid h-full place-items-center px-6 text-center">
        <div>
          <p className="text-[14px] font-medium">Could not open this workflow</p>
          <p className="mt-1 text-[12.5px] text-ink-muted">{messageOf(workflow.error)}</p>
          <Button className="mt-4" onClick={() => navigate('/workflows')}>Back to workflows</Button>
        </div>
      </div>
    )
  }

  const busy = save.isPending || validate.isPending || publish.isPending

  return (
    <div className="flex h-full flex-col">
      <header className="flex h-12 shrink-0 items-center gap-3 border-b border-line bg-surface px-3">
        <button
          onClick={() => navigate('/workflows')}
          className="grid size-7 place-items-center rounded-sm text-ink-muted hover:bg-canvas hover:text-ink"
          aria-label="Back to workflows"
        >
          <ArrowLeft className="size-4" />
        </button>

        <div className="min-w-0">
          <h1 className="truncate text-[13.5px] font-semibold tracking-tight">
            {workflow.data?.name}
          </h1>
        </div>

        <StateBadge
          dirty={state.dirty}
          versionNo={workflow.data?.active_version_no ?? null}
          hasUnpublished={workflow.data?.has_unpublished_changes ?? false}
        />

        <div className="ml-auto flex items-center gap-1.5">
          <Button size="sm" variant="ghost" onClick={() => save.mutate()} loading={save.isPending} disabled={!state.dirty}>
            <Save className="size-3.5" />
            Save
          </Button>
          <Button size="sm" onClick={() => validate.mutate()} loading={validate.isPending} disabled={busy}>
            <ShieldCheck className="size-3.5" />
            Validate
          </Button>
          <Button size="sm" variant="primary" onClick={() => publish.mutate()} loading={publish.isPending} disabled={busy}>
            <Rocket className="size-3.5" />
            Publish
          </Button>
          <Button
            size="sm"
            variant="primary"
            onClick={() => setRunOpen(true)}
            loading={run.isPending}
            disabled={!workflow.data?.active_version_no}
            title={workflow.data?.active_version_no ? 'Start a run' : 'Publish before running'}
          >
            <Play className="size-3.5" />
            Run
          </Button>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <NodeLibrary catalogue={types} onAdd={(descriptor) => addNode(descriptor)} />

        <div className="relative min-w-0 flex-1" ref={canvasRef}>
          <ReactFlow<FlowNode>
            nodes={state.nodes}
            edges={state.edges}
            nodeTypes={NODE_TYPES}
            onInit={setInstance}
            onNodesChange={state.onNodesChange}
            onEdgesChange={state.onEdgesChange}
            onConnect={state.connect}
            onNodeClick={(_, node) => state.select((node as FlowNode).data.nodeKey)}
            onPaneClick={() => state.select(null)}
            onDragOver={(event) => {
              event.preventDefault()
              event.dataTransfer.dropEffect = 'move'
            }}
            onDrop={(event) => {
              event.preventDefault()
              const qualified = event.dataTransfer.getData('application/orqent-node')
              const descriptor = types.find((type) => type.qualified_name === qualified)
              if (!descriptor || !instance) return
              addNode(
                descriptor,
                instance.screenToFlowPosition({ x: event.clientX, y: event.clientY }),
              )
            }}
            defaultEdgeOptions={{ animated: false }}
            proOptions={{ hideAttribution: true }}
            fitView
            minZoom={0.25}
            maxZoom={1.75}
            className="bg-canvas"
          >
            <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="var(--color-flow-grid)" />
            <Controls showInteractive={false} className="!shadow-none" />
          </ReactFlow>

          {state.nodes.length === 0 && (
            <div className="pointer-events-none absolute inset-0 grid place-items-center">
              <div className="text-center">
                <p className="text-[13.5px] font-medium">Start with a trigger</p>
                <p className="mt-1 text-[12px] text-ink-muted">
                  Drag a node from the left, or click one to add it.
                </p>
              </div>
            </div>
          )}

          {report && !report.is_valid && (
            <ValidationPanel
              report={report}
              onClose={() => setReport(null)}
              onSelect={(nodeKey) => state.select(nodeKey)}
            />
          )}

          {report?.is_valid && (
            <div className="absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center gap-2 rounded-md border border-green-200 bg-green-50 px-3 py-1.5">
              <CheckCircle2 className="size-3.5 text-status-succeeded" />
              <span className="text-[12.5px] text-status-succeeded">Graph is valid</span>
            </div>
          )}
        </div>

        <Inspector
          node={selected}
          issues={issues}
          onConfigChange={(config) => selected && state.updateConfig(selected.data.nodeKey, config)}
          onDelete={() => {
            if (!selected) return
            state.onNodesChange([{ type: 'remove', id: selected.id }])
            state.select(null)
          }}
        />
      </div>

      <PublishDialog result={published} onClose={() => setPublished(null)} />
      <Modal
        open={runOpen}
        onClose={() => {
          if (!run.isPending) {
            setRunOpen(false)
            setRunPayloadError(null)
          }
        }}
        title="Start run"
        description="Optional JSON object for the manual trigger."
      >
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault()
            setRunPayloadError(null)
            const text = runPayload.trim()
            if (!text) {
              run.mutate(null)
              return
            }
            try {
              const parsed = JSON.parse(text) as unknown
              if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
                setRunPayloadError('Payload must be a JSON object.')
                return
              }
              run.mutate(parsed as Record<string, unknown>)
            } catch {
              setRunPayloadError('Payload must be valid JSON.')
            }
          }}
        >
          <Field label="Trigger payload" hint="optional">
            <Textarea
              rows={5}
              value={runPayload}
              onChange={(event) => setRunPayload(event.target.value)}
              placeholder={'{ "prompt": "Return the word READY." }'}
              className="font-mono"
            />
          </Field>
          {runPayloadError && (
            <p className="rounded-sm border border-orange-200 bg-orange-50 px-2.5 py-2 text-[12px] text-status-failed">
              {runPayloadError}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button type="button" onClick={() => setRunOpen(false)} disabled={run.isPending}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={run.isPending}>
              <Play className="size-3.5" />
              Run
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  )
}

function StateBadge({
  dirty, versionNo, hasUnpublished,
}: { dirty: boolean; versionNo: number | null; hasUnpublished: boolean }) {
  const [label, tone] = dirty
    ? ['Unsaved changes', 'text-status-suspended']
    : !versionNo
      ? ['Draft', 'text-ink-muted']
      : hasUnpublished
        ? ['Unpublished changes', 'text-status-suspended']
        : [`Published v${versionNo}`, 'text-status-succeeded']

  return (
    <span className={cn('flex items-center gap-1.5 text-[12px]', tone)}>
      <span className="size-1.5 rounded-full bg-current" />
      {label}
    </span>
  )
}
