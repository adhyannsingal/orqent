import { lazy, Suspense, useEffect } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { AppShell } from '@/components/AppShell'
import { Spinner } from '@/components/ui/primitives'
import { RequireAuth } from '@/routes/RequireAuth'
import { useAuth } from '@/stores/auth'

const LoginPage = lazy(() => import('@/features/auth/LoginPage').then((module) => ({ default: module.LoginPage })))
const LandingPage = lazy(() => import('@/features/landing/LandingPage').then((module) => ({ default: module.LandingPage })))
const WorkflowListPage = lazy(() => import('@/features/workflows/WorkflowListPage').then((module) => ({ default: module.WorkflowListPage })))
const BuilderPage = lazy(() => import('@/features/builder/BuilderPage').then((module) => ({ default: module.BuilderPage })))
const RunListPage = lazy(() => import('@/features/runs/RunListPage').then((module) => ({ default: module.RunListPage })))
const RunDetailPage = lazy(() => import('@/features/runs/RunDetailPage').then((module) => ({ default: module.RunDetailPage })))
const KnowledgePage = lazy(() => import('@/features/knowledge/KnowledgePage').then((module) => ({ default: module.KnowledgePage })))

export function App() {
  const restore = useAuth((s) => s.restore)
  useEffect(() => { void restore() }, [restore])

  return (
    <Suspense fallback={<Spinner label="Loading" />}>
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<LoginPage />} />
        <Route
          element={
            <RequireAuth>
              <AppShell />
            </RequireAuth>
          }
        >
          <Route path="/workflows" element={<WorkflowListPage />} />
          <Route path="/workflows/:id" element={<BuilderPage />} />
          <Route path="/runs" element={<RunListPage />} />
          <Route path="/runs/:id" element={<RunDetailPage />} />
          <Route path="/knowledge" element={<KnowledgePage />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Suspense>
  )
}
