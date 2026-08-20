import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { CheckCircle2, FileText, Info } from 'lucide-react'
import { documentsApi } from '@/api/documents'
import { messageOf } from '@/api/client'
import { PageHeader } from '@/components/AppShell'
import { Button, Card, Field, Input, Textarea } from '@/components/ui/primitives'
import type { IngestedDocument } from '@/types/api'

/**
 * Add knowledge an AI agent can retrieve.
 *
 * **Ingestion only, deliberately.** The backend exposes `POST /documents` and
 * nothing else — no listing, no deletion — so there is no corpus browser here.
 * Inventing one would mean showing a client-side list that no server endpoint
 * could confirm, which is worse than showing nothing.
 */
export function KnowledgePage() {
  const [externalId, setExternalId] = useState('')
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [result, setResult] = useState<IngestedDocument | null>(null)
  const [error, setError] = useState<string | null>(null)

  const ingest = useMutation({
    mutationFn: () =>
      documentsApi.ingest({
        external_id: externalId.trim(),
        content,
        title: title.trim() || null,
        // No organization field: the backend derives the tenant from the caller
        // and refuses a body that names one.
      }),
    onSuccess: (document) => {
      setResult(document)
      setError(null)
      setContent('')
    },
    onError: (caught) => {
      setResult(null)
      setError(messageOf(caught, 'Could not add this document.'))
    },
  })

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Knowledge"
        description="Documents your AI agents can retrieve from"
      />

      <div className="min-h-0 flex-1 overflow-auto p-5">
        <div className="mx-auto max-w-2xl space-y-4">
          <Card className="p-4">
            <form
              onSubmit={(event) => { event.preventDefault(); ingest.mutate() }}
              className="space-y-3"
            >
              <Field
                label="Reference"
                hint="your own identifier — re-using it replaces that document"
              >
                <Input
                  value={externalId}
                  onChange={(event) => setExternalId(event.target.value)}
                  placeholder="handbook/expenses"
                  required
                />
              </Field>

              <Field label="Title" hint="optional">
                <Input
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                  placeholder="Expense policy"
                />
              </Field>

              <Field label="Content" hint="plain text">
                <Textarea
                  rows={12}
                  value={content}
                  onChange={(event) => setContent(event.target.value)}
                  placeholder="Paste the text an agent should be able to draw on…"
                  required
                  className="font-mono text-[12.5px]"
                />
              </Field>

              <div className="flex items-center justify-between gap-3 pt-1">
                <p className="text-[11.5px] text-ink-muted">
                  Chunked and embedded by Orqent. Scoped to your workspace.
                </p>
                <Button
                  type="submit"
                  variant="primary"
                  loading={ingest.isPending}
                  disabled={!externalId.trim() || !content.trim()}
                >
                  Add to knowledge
                </Button>
              </div>
            </form>
          </Card>

          {result && (
            <Card className="flex items-start gap-3 border-green-200 bg-green-50 p-3.5">
              <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-status-succeeded" />
              <div className="min-w-0">
                <p className="text-[13px] font-medium text-status-succeeded">
                  {result.unchanged ? 'Already up to date' : 'Indexed'}
                </p>
                <p className="mt-0.5 text-[12px] text-ink-muted">
                  {result.unchanged
                    ? 'The content matched what was already stored, so nothing was re-embedded.'
                    : `${result.chunk_count} chunk${result.chunk_count === 1 ? '' : 's'} indexed and available to retrieval.`}
                </p>
                <p className="mt-1 font-mono text-[11px] text-ink-muted">
                  {result.external_id}
                </p>
              </div>
            </Card>
          )}

          {error && (
            <Card className="border-orange-200 bg-orange-50 p-3.5">
              <p className="text-[13px] font-medium text-status-failed">
                Could not add this document
              </p>
              <p className="mt-0.5 text-[12px] text-ink-muted">{error}</p>
            </Card>
          )}

          <div className="flex items-start gap-2.5 px-1 text-[12px] text-ink-muted">
            <Info className="mt-0.5 size-3.5 shrink-0" />
            <p>
              To use this, add an <strong className="font-medium text-ink">AI Agent</strong>{' '}
              node to a workflow and turn on retrieval. It searches only your
              workspace&rsquo;s documents.
            </p>
          </div>

          <div className="flex items-start gap-2.5 px-1 text-[12px] text-ink-muted">
            <FileText className="mt-0.5 size-3.5 shrink-0" />
            <p>Plain text only for now — file upload and parsing are not supported yet.</p>
          </div>
        </div>
      </div>
    </div>
  )
}
