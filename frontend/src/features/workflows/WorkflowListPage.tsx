import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Workflow as WorkflowIcon } from 'lucide-react'
import { toast } from 'sonner'
import { workflowsApi } from '@/api/workflows'
import { messageOf } from '@/api/client'
import { PageHeader } from '@/components/AppShell'
import { Button, EmptyState, Field, Input, Modal, Spinner, Textarea } from '@/components/ui/primitives'
import { formatRelative } from '@/lib/utils'

export function WorkflowListPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')

  const workflows = useQuery({
    queryKey: ['workflows'],
    queryFn: () => workflowsApi.list(),
  })

  const create = useMutation({
    mutationFn: () => workflowsApi.create(name.trim(), description.trim()),
    onSuccess: (workflow) => {
      void queryClient.invalidateQueries({ queryKey: ['workflows'] })
      setCreating(false)
      setName('')
      setDescription('')
      // Straight into the builder: creating a workflow is never the goal.
      navigate(`/workflows/${workflow.public_id}`)
    },
    onError: (error) => toast.error(messageOf(error, 'Could not create the workflow.')),
  })

  const items = workflows.data?.items ?? []

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Workflows"
        actions={
          <Button variant="primary" onClick={() => setCreating(true)}>
            <Plus className="size-3.5" />
            New workflow
          </Button>
        }
      />

      <div className="min-h-0 flex-1 overflow-auto">
        {workflows.isLoading && <Spinner label="Loading workflows" />}

        {workflows.isError && (
          <EmptyState
            title="Could not load workflows"
            description={messageOf(workflows.error)}
            action={<Button onClick={() => void workflows.refetch()}>Try again</Button>}
          />
        )}

        {workflows.isSuccess && items.length === 0 && (
          <EmptyState
            icon={<WorkflowIcon className="size-7" strokeWidth={1.5} />}
            title="No workflows yet"
            description="A workflow is a graph of typed nodes — a trigger, some work, and whatever comes next."
            action={
              <Button variant="primary" onClick={() => setCreating(true)}>
                <Plus className="size-3.5" />
                New workflow
              </Button>
            }
          />
        )}

        {items.length > 0 && (
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b border-line text-left">
                {['Name', 'Version', 'State', 'Updated'].map((column, index) => (
                  <th
                    key={column}
                    className={`px-5 py-2 text-[11.5px] font-medium uppercase tracking-wide text-ink-muted ${
                      index > 0 ? 'w-[140px]' : ''
                    }`}
                  >
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((workflow) => (
                <tr
                  key={workflow.public_id}
                  onClick={() => navigate(`/workflows/${workflow.public_id}`)}
                  className="cursor-pointer border-b border-line hover:bg-surface"
                >
                  <td className="px-5 py-2.5">
                    <div className="text-[13px] font-medium text-ink">{workflow.name}</div>
                    {workflow.description && (
                      <div className="truncate text-[12px] text-ink-muted">
                        {workflow.description}
                      </div>
                    )}
                  </td>
                  <td className="px-5 py-2.5 tnum text-[12.5px] text-ink-muted">
                    {workflow.active_version_no ? `v${workflow.active_version_no}` : '—'}
                  </td>
                  <td className="px-5 py-2.5">
                    <WorkflowState
                      versionNo={workflow.active_version_no}
                      dirty={workflow.has_unpublished_changes}
                    />
                  </td>
                  <td className="px-5 py-2.5 text-[12.5px] text-ink-muted">
                    {formatRelative(workflow.updated_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <Modal
        open={creating}
        onClose={() => setCreating(false)}
        title="New workflow"
        description="You can rename it later."
      >
        <form
          onSubmit={(event) => { event.preventDefault(); create.mutate() }}
          className="space-y-3"
        >
          <Field label="Name">
            <Input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Support triage"
              required
              autoFocus
            />
          </Field>
          <Field label="Description" hint="optional">
            <Textarea
              rows={2}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="What this workflow does"
            />
          </Field>
          <div className="flex justify-end gap-2 pt-1">
            <Button type="button" onClick={() => setCreating(false)}>Cancel</Button>
            <Button
              type="submit"
              variant="primary"
              loading={create.isPending}
              disabled={!name.trim()}
            >
              Create
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  )
}

/** Draft / published / unpublished-changes, from the two fields the API gives. */
function WorkflowState({ versionNo, dirty }: { versionNo: number | null; dirty: boolean }) {
  if (!versionNo) {
    return <span className="text-[12px] text-ink-muted">Draft</span>
  }
  if (dirty) {
    return (
      <span className="inline-flex items-center gap-1.5 text-[12px] text-status-suspended">
        <span className="size-1.5 rounded-full bg-status-suspended" />
        Unpublished changes
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-[12px] text-status-succeeded">
      <span className="size-1.5 rounded-full bg-status-succeeded" />
      Published
    </span>
  )
}
