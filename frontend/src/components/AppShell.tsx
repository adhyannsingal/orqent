/**
 * The authenticated shell: a dark charcoal rail against a near-white workspace.
 *
 * Three destinations, matching the three things this backend actually does —
 * author workflows, watch them run, and give them knowledge. No settings page,
 * because there is no backend behaviour behind one.
 */
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { FileText, LogOut, Play, Workflow } from 'lucide-react'
import { useAuth } from '@/stores/auth'
import { cn } from '@/lib/utils'

const NAV = [
  { to: '/workflows', label: 'Workflows', icon: Workflow },
  { to: '/runs', label: 'Runs', icon: Play },
  { to: '/knowledge', label: 'Knowledge', icon: FileText },
]

export function AppShell() {
  const user = useAuth((s) => s.user)
  const logout = useAuth((s) => s.logout)
  const navigate = useNavigate()

  return (
    <div className="flex h-full">
      <aside className="flex w-[228px] shrink-0 flex-col bg-nav">
        <div className="flex h-12 items-center gap-2 px-4">
          <div className="grid size-5 place-items-center rounded-[3px] bg-white">
            <span className="text-[11px] font-bold leading-none text-nav">O</span>
          </div>
          <span className="text-[13.5px] font-semibold tracking-tight text-white">Orqent</span>
        </div>

        <nav className="mt-2 flex-1 px-2">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn(
                  'mb-0.5 flex items-center gap-2.5 rounded-sm px-2.5 py-1.5',
                  'text-[13px] transition-colors',
                  isActive
                    ? 'bg-nav-active text-nav-ink-active'
                    : 'text-nav-ink hover:bg-nav-hover hover:text-white',
                )
              }
            >
              <Icon className="size-4" />
              {label}
            </NavLink>
          ))}
        </nav>

        <div className="border-t border-white/8 p-2">
          <div className="px-2.5 py-1.5">
            <p className="truncate text-[12px] font-medium text-white/90">
              {user?.public_id.slice(0, 12) ?? 'Signed in'}
            </p>
            {/* Identity, not a selector: tenancy is decided by the backend. */}
            <p className="truncate font-mono text-[10.5px] text-nav-ink">
              org {user?.organization_id.slice(0, 10) ?? '—'}
            </p>
          </div>
          <button
            onClick={async () => { await logout(); navigate('/login', { replace: true }) }}
            className={cn(
              'mt-0.5 flex w-full items-center gap-2.5 rounded-sm px-2.5 py-1.5',
              'text-[13px] text-nav-ink transition-colors',
              'hover:bg-nav-hover hover:text-white',
            )}
          >
            <LogOut className="size-4" />
            Sign out
          </button>
        </div>
      </aside>

      <main className="min-w-0 flex-1 overflow-hidden">
        <Outlet />
      </main>
    </div>
  )
}

/** Page chrome shared by the list pages. The builder draws its own. */
export function PageHeader({
  title, description, actions,
}: { title: string; description?: string; actions?: React.ReactNode }) {
  return (
    <header className="flex h-14 shrink-0 items-center justify-between border-b border-line bg-surface px-5">
      <div>
        <h1 className="text-[15px] font-semibold tracking-tight text-ink">{title}</h1>
        {description && <p className="text-[12px] text-ink-muted">{description}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </header>
  )
}
