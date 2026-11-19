import { Suspense } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import { Bot, CalendarClock, FileSpreadsheet, FolderTree, History, Settings, ShieldCheck } from 'lucide-react';
import { cn } from '@/lib/utils';
import { ToastViewport } from '@/components/ui/toast';

const navItems = [
  { to: '/', label: 'Converter', icon: FileSpreadsheet, end: true },
  { to: '/sources', label: 'Fontes', icon: FolderTree, end: false },
  { to: '/schedules', label: 'Agendamentos', icon: CalendarClock, end: false },
  { to: '/assistant', label: 'Assistente', icon: Bot, end: false },
  { to: '/history', label: 'Histórico', icon: History, end: false },
  { to: '/settings', label: 'Configurações', icon: Settings, end: false },
] as const;

export function AppShell() {
  return (
    <div className="flex min-h-dvh flex-col">
      <a
        href="#conteudo"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-accent focus:px-3 focus:py-2 focus:text-sm focus:text-accent-foreground"
      >
        Pular para o conteúdo
      </a>

      <header className="sticky top-0 z-40 border-b border-border/80 bg-background/85 backdrop-blur-md">
        <div className="mx-auto flex h-16 w-full max-w-6xl items-center justify-between gap-4 px-4 sm:px-6">
          <NavLink
            to="/"
            className="flex items-center gap-2 rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            aria-label="Conversor Generator — início"
          >
            <span className="grid size-8 place-items-center rounded-lg bg-accent/15 text-accent">
              <FileSpreadsheet className="size-4" aria-hidden="true" />
            </span>
            <span className="flex flex-col leading-none">
              <span className="text-sm font-semibold tracking-tight text-foreground">Conversor Generator</span>
              <span className="mt-0.5 hidden text-[10px] uppercase tracking-widest text-muted-2 sm:block">
                Conversor auditável
              </span>
            </span>
          </NavLink>

          <nav aria-label="Navegação principal" className="flex items-center gap-1">
            {navItems.map((item) => {
              const Icon = item.icon;
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) =>
                    cn(
                      'inline-flex items-center gap-1.5 rounded-lg px-2.5 py-2 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background sm:px-3 sm:text-sm',
                      isActive ? 'bg-surface-2 text-foreground' : 'text-muted hover:bg-surface-2/60 hover:text-foreground',
                    )
                  }
                >
                  <Icon className="size-4" aria-hidden="true" />
                  <span>{item.label}</span>
                </NavLink>
              );
            })}
          </nav>
        </div>
      </header>

      <main id="conteudo" className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-6 sm:py-10">
        <Suspense
          fallback={
            <div role="status" aria-label="Carregando página" className="space-y-3">
              <div className="h-8 w-64 animate-pulse rounded-lg bg-surface-2" />
              <div className="h-4 w-96 max-w-full animate-pulse rounded bg-surface-2" />
              <div className="h-40 animate-pulse rounded-[var(--radius-card)] border border-border bg-surface/60" />
              <span className="sr-only">Carregando página…</span>
            </div>
          }
        >
          <Outlet />
        </Suspense>
      </main>

      <footer className="border-t border-border/70">
        <div className="mx-auto flex w-full max-w-6xl flex-col gap-2 px-4 py-6 text-xs text-muted-2 sm:flex-row sm:items-center sm:justify-between sm:px-6">
          <p>
            Conversor Generator — extração de texto, campos e tabelas. Nenhuma imagem é inserida no arquivo gerado.
          </p>
          <p className="inline-flex items-center gap-1.5">
            <ShieldCheck className="size-3.5 text-accent" aria-hidden="true" />
            Trilha de auditoria com encadeamento de hashes
          </p>
        </div>
      </footer>

      <ToastViewport />
    </div>
  );
}
