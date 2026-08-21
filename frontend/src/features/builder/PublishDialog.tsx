import { useState } from 'react'
import { Check, Copy, Rocket, TriangleAlert } from 'lucide-react'
import { API_BASE_URL } from '@/api/client'
import { Button, Modal } from '@/components/ui/primitives'
import type { PublishResult } from '@/types/api'

/**
 * What publishing produced.
 *
 * **The webhook token is a bearer credential.** The backend returns it exactly
 * once — when a webhook registration is first created — and there is no
 * recovery endpoint, because a stored raw token would be a stored password.
 * So it is shown deliberately, copied on demand, and kept only in this
 * component's props: never written to storage, never logged, never re-rendered
 * from fabricated state after a reload.
 */
export function PublishDialog({
  result, onClose,
}: { result: PublishResult | null; onClose: () => void }) {
  const [copied, setCopied] = useState(false)
  if (!result) return null

  const base = API_BASE_URL || window.location.origin
  const webhookUrl = result.webhook_token ? `${base}/hooks/${result.webhook_token}` : null

  async function copy() {
    if (!webhookUrl) return
    try {
      await navigator.clipboard.writeText(webhookUrl)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      /* clipboard denied; the URL is selectable on screen */
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={`Published v${result.version_no ?? '—'}`}
      description="This version is frozen. Runs pin the exact graph they executed."
      width="max-w-lg"
    >
      <div className="space-y-3">
        <div className="flex items-center gap-2 rounded-sm bg-green-50 px-3 py-2">
          <Rocket className="size-3.5 text-status-succeeded" />
          <span className="text-[12.5px] text-status-succeeded">
            Ready to run and accept triggers.
          </span>
        </div>

        {webhookUrl && (
          <div>
            <p className="mb-1 text-[12px] font-medium">Webhook address</p>
            <div className="flex items-stretch gap-1.5">
              <code className="min-w-0 flex-1 truncate rounded-sm border border-line-strong bg-canvas px-2.5 py-1.5 font-mono text-[11.5px]">
                POST {webhookUrl}
              </code>
              <Button onClick={copy} size="sm" className="shrink-0">
                {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
                {copied ? 'Copied' : 'Copy'}
              </Button>
            </div>

            <div className="mt-2 flex items-start gap-2 rounded-sm border border-amber-200 bg-amber-50 px-2.5 py-2">
              <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-status-suspended" />
              <p className="text-[11.5px] leading-relaxed text-ink">
                This URL contains a credential and is shown{' '}
                <strong className="font-medium">only now</strong>. Copy it before closing —
                it cannot be retrieved again.
              </p>
            </div>
          </div>
        )}

        <div className="flex justify-end pt-1">
          <Button variant="primary" onClick={onClose}>Done</Button>
        </div>
      </div>
    </Modal>
  )
}
