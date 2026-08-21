import { Link } from 'react-router-dom'
import { ArrowRight } from 'lucide-react'
import { cn } from '@/lib/utils'

export function LandingPage() {
  return (
    <main className="min-h-full bg-[#f5f4f0] text-[#171717] dark:bg-[#151619] dark:text-white">
      <div className="mx-auto flex min-h-screen max-w-6xl flex-col px-5 py-5 sm:px-8 lg:px-10">
        <header className="flex h-12 items-center justify-between">
          <Link to="/" className="flex items-center gap-2.5" aria-label="Orqent home">
            <span className="grid size-8 place-items-center rounded-md bg-[#171717] text-[17px] font-semibold leading-none text-white dark:bg-white dark:text-[#151619]">
              Ø
            </span>
            <span className="text-[18px] font-semibold tracking-tight">Orqent</span>
          </Link>

          <nav className="flex items-center gap-2.5">
            <Link
              to="/login"
              className="inline-flex h-10 items-center whitespace-nowrap rounded-md px-3.5 text-[14px] font-medium text-[#333333] transition-colors hover:bg-black/[0.04] dark:text-white/78 dark:hover:bg-white/8"
            >
              Sign In
            </Link>
            <LandingButton to="/register">Get Started</LandingButton>
          </nav>
        </header>

        <section className="flex flex-1 items-center py-16 sm:py-20 lg:py-24">
          <div className="max-w-[720px]">
            <h1 className="max-w-[680px] text-[40px] font-semibold leading-[1.08] tracking-tight text-[#111111] dark:text-white sm:text-[48px] lg:text-[52px]">
              Build workflows that actually run.
            </h1>
            <p className="mt-5 max-w-[640px] text-[17px] leading-7 text-[#565656] dark:text-white/66">
              Orqent lets you build, automate, and run AI workflows with triggers,
              retrieval, and tools.
            </p>
            <div className="mt-8 flex flex-wrap items-center gap-3">
              <LandingButton to="/register">Get Started</LandingButton>
              <Link
                to="/login"
                className="inline-flex h-10 items-center whitespace-nowrap rounded-md border border-black/12 bg-transparent px-4 text-[14px] font-medium text-[#252525] transition-colors hover:bg-black/[0.04] dark:border-white/14 dark:text-white/82 dark:hover:bg-white/8"
              >
                Sign In
              </Link>
            </div>
          </div>
        </section>
      </div>
    </main>
  )
}

function LandingButton({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <Link
      to={to}
      className={cn(
        'inline-flex h-10 items-center justify-center gap-2 whitespace-nowrap rounded-md',
        'border border-[#2563eb] bg-[#2563eb] px-4 text-[14px] font-medium text-white',
        'transition-colors hover:border-[#1d4ed8] hover:bg-[#1d4ed8]',
      )}
    >
      {children}
      <ArrowRight className="size-4" strokeWidth={2.2} />
    </Link>
  )
}
