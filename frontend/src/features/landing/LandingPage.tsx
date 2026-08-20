import { Link } from 'react-router-dom'
import {
  ArrowRight, Bot, Braces, CalendarClock, GitBranch, Play, ShieldCheck,
  Workflow,
} from 'lucide-react'
import { ThemeToggle } from '@/components/ThemeToggle'
import { cn } from '@/lib/utils'

const FEATURES = [
  {
    icon: Workflow,
    title: 'Visual orchestration',
    text: 'Author typed DAGs with triggers, branches, merge points, and durable node history.',
  },
  {
    icon: ShieldCheck,
    title: 'Durable execution',
    text: 'Runs are queued, inspectable, resumable, and pinned to the exact version they executed.',
  },
  {
    icon: Bot,
    title: 'AI + tenant RAG',
    text: 'Gemini agents retrieve tenant-scoped knowledge and call tools without exposing provider keys.',
  },
  {
    icon: CalendarClock,
    title: 'Triggers and tools',
    text: 'Start workflows manually, by webhook, or by UTC cron; give agents safe built-in tools.',
  },
]

const NODES = [
  { label: 'Manual trigger', icon: Play, x: '22%', y: '38%' },
  { label: 'AI agent', icon: Bot, x: '45%', y: '24%' },
  { label: 'Condition', icon: GitBranch, x: '64%', y: '49%' },
  { label: 'Log output', icon: Braces, x: '84%', y: '31%' },
]

export function LandingPage() {
  return (
    <div className="min-h-full bg-canvas text-ink">
      <header className="mx-auto flex h-20 max-w-[1500px] items-center gap-8 px-7 sm:px-10">
        <Link to="/" className="flex items-center gap-3" aria-label="Orqent home">
          <span className="grid size-10 place-items-center rounded-[10px] bg-ink text-[22px] font-semibold leading-none text-white">
            Ø
          </span>
          <span className="text-[25px] font-semibold tracking-tight">Orqent</span>
        </Link>

        <nav className="mx-auto hidden items-center gap-10 text-[15px] text-ink-muted md:flex">
          <a href="#product" className="hover:text-ink">Product</a>
          <a href="#features" className="hover:text-ink">Features</a>
          <a
            href="https://github.com/adhyannsingal/orqent"
            className="inline-flex items-center gap-1.5 hover:text-ink"
          >
            <GitBranch className="size-3.5" />
            GitHub
          </a>
        </nav>

        <div className="ml-auto flex items-center gap-2">
          <ThemeToggle className="hidden sm:inline-flex" />
          <Link
            to="/login"
            className="hidden h-10 items-center rounded-full px-4 text-[14px] font-medium text-ink-muted hover:text-ink sm:inline-flex"
          >
            Sign in
          </Link>
          <Link
            to="/register"
            className="inline-flex h-11 items-center gap-2 rounded-full bg-ink px-5 text-[14px] font-medium text-white hover:bg-black dark:hover:bg-white/85 dark:hover:text-black"
          >
            Get started
            <ArrowRight className="size-4" />
          </Link>
        </div>
      </header>

      <main>
        <section className="px-4 pb-10 sm:px-7">
          <div
            id="product"
            className={cn(
              'mx-auto min-h-[calc(100vh-7rem)] max-w-[1500px] overflow-hidden rounded-[28px]',
              'border border-line bg-surface shadow-[0_22px_80px_rgba(0,0,0,0.08)]',
              'dark:shadow-[0_22px_80px_rgba(0,0,0,0.35)]',
            )}
          >
            <div className="relative grid min-h-[700px] content-start p-8 sm:p-12 lg:grid-cols-[minmax(0,0.95fr)_minmax(420px,0.85fr)] lg:gap-x-10 lg:p-16">
              <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_72%_26%,rgba(91,87,209,0.16),transparent_34%),linear-gradient(180deg,rgba(255,255,255,0.86),rgba(250,250,250,0.55))] dark:bg-[radial-gradient(circle_at_72%_26%,rgba(143,140,255,0.16),transparent_34%),linear-gradient(180deg,rgba(16,17,24,0.96),rgba(9,10,15,0.78))]" />
              <div className="pointer-events-none absolute inset-x-0 bottom-0 h-[46%] bg-[linear-gradient(180deg,transparent,rgba(120,126,140,0.16))] dark:bg-[linear-gradient(180deg,transparent,rgba(255,255,255,0.05))]" />

              <div className="relative z-10 max-w-4xl pt-12 lg:col-start-1 lg:row-start-1">
                <p className="mb-5 inline-flex rounded-full border border-line-strong bg-surface/80 px-3 py-1 text-[12px] font-medium text-ink-muted backdrop-blur">
                  Workflow automation for AI operations
                </p>
                <h1 className="max-w-4xl text-[clamp(54px,7vw,112px)] font-medium leading-[0.96] tracking-tight">
                  Agents that
                  <br />
                  keep working
                </h1>
                <p className="mt-8 max-w-[560px] text-[clamp(18px,2vw,25px)] leading-relaxed text-ink-muted">
                  Build visual workflows that combine triggers, branching, durable execution,
                  tenant-scoped RAG, and AI tool calling.
                </p>

                <div className="mt-10 flex flex-wrap items-center gap-3">
                  <Link
                    to="/register"
                    className="inline-flex h-14 items-center gap-4 rounded-full bg-ink py-1 pl-7 pr-1.5 text-[17px] font-medium text-white hover:bg-black dark:hover:bg-white/85 dark:hover:text-black"
                  >
                    Get started
                    <span className="grid size-11 place-items-center rounded-full bg-white text-ink dark:bg-black dark:text-white">
                      <ArrowRight className="size-5" />
                    </span>
                  </Link>
                  <Link
                    to="/login"
                    className="inline-flex h-14 items-center rounded-full border border-line-strong bg-surface/70 px-6 text-[15px] font-medium text-ink hover:bg-canvas"
                  >
                    Sign in
                  </Link>
                </div>
              </div>

              <WorkflowVisual />

              <div className="relative z-10 mt-12 grid max-w-4xl grid-cols-2 gap-5 pb-3 text-[14px] text-ink-muted sm:grid-cols-4 lg:col-span-2">
                {['Draft', 'Validate', 'Publish', 'Inspect'].map((item) => (
                  <div key={item} className="border-t border-line pt-3">
                    <span className="font-medium text-ink">{item}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </section>

        <section id="features" className="mx-auto max-w-[1500px] px-7 pb-16 pt-4 sm:px-10">
          <div className="grid gap-3 md:grid-cols-4">
            {FEATURES.map(({ icon: Icon, title, text }) => (
              <article key={title} className="rounded-md border border-line bg-surface p-5">
                <Icon className="size-5 text-ink" />
                <h2 className="mt-5 text-[15px] font-semibold">{title}</h2>
                <p className="mt-2 text-[13px] leading-relaxed text-ink-muted">{text}</p>
              </article>
            ))}
          </div>

          <div className="mt-5 flex flex-col items-start justify-between gap-4 rounded-md border border-line bg-surface p-5 sm:flex-row sm:items-center">
            <div>
              <p className="text-[17px] font-semibold">Ready for the demo path.</p>
              <p className="mt-1 text-[13px] text-ink-muted">
                Register, ingest knowledge, publish an AI workflow, and inspect every node output.
              </p>
            </div>
            <div className="flex gap-2">
              <Link
                to="/register"
                className="inline-flex h-10 items-center gap-2 rounded-full bg-ink px-4 text-[13px] font-medium text-white hover:bg-black dark:hover:bg-white/85 dark:hover:text-black"
              >
                Get started
                <ArrowRight className="size-4" />
              </Link>
              <Link
                to="/login"
                className="inline-flex h-10 items-center rounded-full border border-line-strong px-4 text-[13px] font-medium text-ink hover:bg-surface"
              >
                Sign in
              </Link>
            </div>
          </div>
        </section>
      </main>
    </div>
  )
}

function WorkflowVisual() {
  return (
    <div className="relative z-10 mt-14 h-[300px] max-w-[960px] lg:col-start-2 lg:row-start-1 lg:mt-40 lg:max-w-none">
      <div className="absolute inset-0 overflow-hidden rounded-[18px] border border-line bg-canvas/80 shadow-[0_24px_80px_rgba(0,0,0,0.12)] backdrop-blur dark:shadow-[0_24px_80px_rgba(0,0,0,0.42)]">
        <div className="flex h-10 items-center gap-2 border-b border-line px-3">
          <span className="size-2 rounded-full bg-status-failed" />
          <span className="size-2 rounded-full bg-status-suspended" />
          <span className="size-2 rounded-full bg-status-succeeded" />
          <span className="ml-2 text-[11px] font-medium text-ink-muted">production workflow</span>
        </div>
        <svg className="absolute inset-0 h-full w-full pt-10" aria-hidden>
          <line x1="20%" y1="48%" x2="41%" y2="34%" stroke="var(--color-line-strong)" strokeWidth="1.5" />
          <line x1="48%" y1="39%" x2="62%" y2="56%" stroke="var(--color-line-strong)" strokeWidth="1.5" />
          <line x1="68%" y1="53%" x2="82%" y2="39%" stroke="var(--color-line-strong)" strokeWidth="1.5" />
        </svg>
        {NODES.map(({ label, icon: Icon, x, y }) => (
          <div
            key={label}
            className="absolute w-[128px] rounded-md border border-line-strong bg-surface px-3 py-2 sm:w-[150px]"
            style={{ left: x, top: y, transform: 'translateX(-50%)' }}
          >
            <div className="flex items-center gap-2">
              <span className="grid size-6 place-items-center rounded-sm bg-accent-soft text-accent">
                <Icon className="size-3.5" />
              </span>
              <span className="truncate text-[12px] font-medium">{label}</span>
            </div>
            <div className="mt-2 h-1.5 rounded-full bg-canvas">
              <div className="h-full w-2/3 rounded-full bg-accent" />
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
