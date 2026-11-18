import { lazy } from 'react';
import {
  RouterProvider,
  createBrowserRouter,
  isRouteErrorResponse,
  useRouteError,
} from 'react-router-dom';
import { AppShell } from '@/components/layout/app-shell';
import { Button } from '@/components/ui/button';
import { Link } from 'react-router-dom';
import { HomePage } from '@/pages/home-page';
import { NotFoundPage } from '@/pages/not-found-page';

// Route-level code splitting: the audit trail is heavy and only needed on demand.
const ConversionPage = lazy(() =>
  import('@/pages/conversion-page').then((module) => ({ default: module.ConversionPage })),
);
const ResultPage = lazy(() =>
  import('@/pages/result-page').then((module) => ({ default: module.ResultPage })),
);
const AuditPage = lazy(() =>
  import('@/pages/audit-page').then((module) => ({ default: module.AuditPage })),
);
const HistoryPage = lazy(() =>
  import('@/pages/history-page').then((module) => ({ default: module.HistoryPage })),
);
const SettingsPage = lazy(() =>
  import('@/pages/settings-page').then((module) => ({ default: module.SettingsPage })),
);
const AssistantPage = lazy(() =>
  import('@/pages/assistant-page').then((module) => ({ default: module.AssistantPage })),
);
const BatchPage = lazy(() =>
  import('@/pages/batch-page').then((module) => ({ default: module.BatchPage })),
);

function RouteErrorPage() {
  const error = useRouteError();
  const message = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : error instanceof Error
      ? error.message
      : 'Erro inesperado ao renderizar a página.';

  return (
    <div className="mx-auto flex max-w-lg flex-col items-center gap-4 py-20 text-center">
      <p className="text-xs font-semibold uppercase tracking-widest text-danger">Falha na rota</p>
      <h1 className="text-2xl font-semibold text-foreground">Algo deu errado</h1>
      <p className="text-sm text-muted">{message}</p>
      <Button asChild>
        <Link to="/">Voltar ao início</Link>
      </Button>
    </div>
  );
}

/**
 * The router uses the same base Vite used for the assets (`BASE_URL`, `/` by
 * default), so links keep working if the SPA is served from a sub-path.
 */
const routerBase = (import.meta.env.BASE_URL || '/').replace(/\/+$/, '') || '/';

export const router = createBrowserRouter(
  [
    {
      path: '/',
      element: <AppShell />,
      errorElement: <RouteErrorPage />,
      children: [
        { index: true, element: <HomePage /> },
        { path: 'conversions/:id', element: <ConversionPage /> },
        { path: 'conversions/:id/result', element: <ResultPage /> },
        { path: 'conversions/:id/audit', element: <AuditPage /> },
        { path: 'batches/:id', element: <BatchPage /> },
        { path: 'history', element: <HistoryPage /> },
        { path: 'assistant', element: <AssistantPage /> },
        { path: 'settings', element: <SettingsPage /> },
        { path: '*', element: <NotFoundPage /> },
      ],
    },
  ],
  { basename: routerBase },
);

export function App() {
  return <RouterProvider router={router} />;
}
