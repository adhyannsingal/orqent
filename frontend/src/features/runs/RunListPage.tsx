import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Play } from 'lucide-react'
import { runsApi } from '@/api/runs'
import { workflowsApi } from '@/api/workflows'
import { messageOf } from '@/api/client'
import { PageHeader } from '@/components/AppShell'
import { Button, EmptyState, Spinner } from '@/components/ui/primitives'
import { StatusPill } from '@/components/ui/status'
import { formatDuration, formatRelative } from '@/lib/utils'

export function RunListPage() {
  const navigate = useNavigate()

  const runs = useQuery({
    queryKey: ['runs'],
    queryFn: () => runsApi.list(),
    // The list is a dashboard: keep it live, cheaply.
    refetchInterval: 5000,
  })

  // Names, so the table reads as workflows rather than opaque ids.
  const workflows = useQuery({ queryKey: ['workflows'], queryFn: () => workflowsApi.list() })
  const nameOf = (id: string) =>
    workflows.data?.items.find((workflow) => workflow.public_id === id)?.name ?? id.slice(0, 10)

  const items = runs.data?.items ?? []

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Runs" description="Every execution, newest first" />

      <div className="min-h-0 flex-1 overflow-auto">
        {runs.isLoading && <Spinner label="Loading runs" />}

        {runs.isError && (
          <EmptyState
            title="Could not load runs"
            description={messageOf(runs.error)}
            action={<Button onClick={() => void runs.refetch()}>Try again</Button>}
          />
        )}

        {runs.isSuccess && items.length === 0 && (
          <EmptyState
            icon={<Play className="size-7" strokeWidth={1.5} />}
            title="No runs yet"
            description="Publish a workflow and start it to see executions here."
            action={<Button onClick={() => navigate('/workflows')}>Go to workflows</Button>}
          />
        )}

        {items.length > 0 && (
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b border-line text-left">
                <th className="px-5 py-2 text-[11.5px] font-medium uppercase tracking-wide text-ink-muted">Run</th>
                <th className="px-5 py-2 text-[11.5px] font-medium uppercase tracking-wide text-ink-muted">Workflow</th>
                <th className="w-[130px] px-5 py-2 text-[11.5px] font-medium uppercase tracking-wide text-ink-muted">Status</th>
                <th className="w-[100px] px-5 py-2 text-[11.5px] font-medium uppercase tracking-wide text-ink-muted">Duration</th>
                <th className="w-[120px] px-5 py-2 text-[11.5px] font-medium uppercase tracking-wide text-ink-muted">Started</th>
              </tr>
            </thead>
            <tbody>
              {items.map((run) => (
                <tr
                  key={run.public_id}
                  onClick={() => navigate(`/runs/${run.public_id}`)}
                  className="cursor-pointer border-b border-line hover:bg-surface"
                >
                  <td className="px-5 py-2.5 font-mono text-[12px] text-ink-muted">
                    {run.public_id.slice(0, 12)}
                  </td>
                  <td className="px-5 py-2.5">
                    <span className="text-[13px] text-ink">{nameOf(run.workflow_id)}</span>
                    {run.version_no && (
                      <span className="ml-1.5 tnum text-[11.5px] text-ink-muted">
                        v{run.version_no}
                      </span>
                    )}
                  </td>
                  <td className="px-5 py-2.5"><StatusPill status={run.status} /></td>
                  <td className="px-5 py-2.5 tnum text-[12.5px] text-ink-muted">
                    {formatDuration(run.started_at, run.finished_at)}
                  </td>
                  <td className="px-5 py-2.5 text-[12.5px] text-ink-muted">
                    {formatRelative(run.started_at ?? run.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
