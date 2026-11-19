import { useState } from 'react';
import { Link } from 'react-router-dom';
import { CalendarClock, Pause, Play, Plus, Trash2, Zap } from 'lucide-react';
import type { ScheduleRecord } from '@pdf2x/contracts';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { PageHeader } from '@/components/layout/page-header';
import { EmptyBlock, ErrorBlock } from '@/components/layout/states';
import { ConfirmDialog } from '@/components/confirm-dialog';
import { useEnsureSession } from '@/hooks/use-session';
import { useScheduleActions, useSchedules } from '@/hooks/use-schedules';
import { useSources } from '@/hooks/use-sources';
import { explainError } from '@/lib/errors';
import { formatDateTime } from '@/lib/format';
import { MISFIRE_LABELS, OVERLAP_LABELS, RUN_STATE_LABELS, ruleLabel } from '@/lib/schedule-labels';
import { createIntentKeys } from '@/lib/intent-keys';
import { toast } from '@/stores/ui';

export function SchedulesPage() {
  useEnsureSession();
  const schedules = useSchedules();
  const sources = useSources();
  const actions = useScheduleActions();
  const [pendingArchive, setPendingArchive] = useState<ScheduleRecord | null>(null);
  const [runKeys] = useState(() => createIntentKeys());

  const sourceName = (sourceId: string) =>
    (sources.data ?? []).find((source) => source.id === sourceId)?.name ?? sourceId;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Agendamentos"
        description="Importações recorrentes executadas no computador/servidor que roda o conversor."
        actions={
          <Button asChild size="sm">
            <Link to="/schedules/new">
              <Plus className="mr-1 h-4 w-4" aria-hidden /> Nova agenda
            </Link>
          </Button>
        }
      />

      {schedules.isError ? (
        <ErrorBlock
          message={explainError(schedules.error)}
          onRetry={() => void schedules.refetch()}
        />
      ) : null}

      {schedules.isLoading ? <p className="text-sm text-muted">Carregando…</p> : null}
      {schedules.data && schedules.data.length === 0 ? (
        <EmptyBlock
          title="Nenhuma agenda"
          description="Crie uma agenda para importar uma fonte diariamente ou em intervalos."
          action={
            <Button asChild size="sm">
              <Link to="/schedules/new">Criar agenda</Link>
            </Button>
          }
        />
      ) : null}

      <div className="space-y-3">
        {(schedules.data ?? []).map((schedule) => (
          <Card key={schedule.id}>
            <CardHeader className="flex-row items-center justify-between space-y-0">
              <CardTitle className="flex items-center gap-2 text-base">
                <CalendarClock className="h-4 w-4" aria-hidden />
                <Link to={`/schedules/${schedule.id}`} className="hover:text-accent">
                  {schedule.name}
                </Link>
                <Badge variant={schedule.enabled ? 'success' : 'warning'}>
                  {schedule.enabled ? 'ativa' : 'pausada'}
                </Badge>
              </CardTitle>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={actions.runNow.isPending}
                  onClick={() =>
                    actions.runNow.mutate(
                      { scheduleId: schedule.id, idempotencyKey: runKeys.keyFor(schedule.id) },
                      {
                        onSuccess: (result) => {
                          runKeys.settle(schedule.id);
                          if (result.executor_active === false) {
                            toast({
                              title: 'Agendado; será executado quando o executor estiver ativo',
                              variant: 'success',
                            });
                            return;
                          }
                          toast({
                            title: 'Execução iniciada',
                            description: `Ocorrência ${result.state}.`,
                            variant: 'success',
                          });
                        },
                        onError: (error) =>
                          toast({ title: 'Falha', description: explainError(error), variant: 'error' }),
                      },
                    )
                  }
                >
                  <Zap className="mr-1 h-4 w-4" aria-hidden /> Executar agora
                </Button>
                {schedule.enabled ? (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() =>
                      actions.pause.mutate(schedule.id, {
                        onSuccess: () => toast({ title: 'Agenda pausada', variant: 'success' }),
                        onError: (error) =>
                          toast({ title: 'Falha', description: explainError(error), variant: 'error' }),
                      })
                    }
                  >
                    <Pause className="mr-1 h-4 w-4" aria-hidden /> Pausar
                  </Button>
                ) : (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() =>
                      actions.resume.mutate(schedule.id, {
                        onSuccess: () =>
                          toast({
                            title: 'Agenda retomada',
                            description: 'O próximo horário futuro foi recalculado.',
                            variant: 'success',
                          }),
                        onError: (error) =>
                          toast({ title: 'Falha', description: explainError(error), variant: 'error' }),
                      })
                    }
                  >
                    <Play className="mr-1 h-4 w-4" aria-hidden /> Retomar
                  </Button>
                )}
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Arquivar ${schedule.name}`}
                  onClick={() => setPendingArchive(schedule)}
                >
                  <Trash2 className="h-4 w-4" aria-hidden />
                </Button>
              </div>
            </CardHeader>
            <CardContent className="grid gap-2 text-sm sm:grid-cols-2">
              <p className="text-muted">
                Fonte: <span className="text-foreground">{sourceName(schedule.source_id)}</span>
              </p>
              <p className="text-muted">
                Regra: <span className="text-foreground">{ruleLabel(schedule)}</span> ·{' '}
                {schedule.timezone}
              </p>
              <p className="text-muted">
                Próxima:{' '}
                <span className="text-foreground">
                  {schedule.next_run_at ? formatDateTime(schedule.next_run_at) : '—'}
                </span>
              </p>
              <p className="text-muted">
                Políticas:{' '}
                <span className="text-foreground">{MISFIRE_LABELS[schedule.misfire_policy]}</span> ·{' '}
                <span className="text-foreground">{OVERLAP_LABELS[schedule.overlap_policy]}</span>
              </p>
              <p className="flex flex-wrap gap-4 text-xs sm:col-span-2">
                <Link to={`/schedules/${schedule.id}`} className="text-accent hover:underline">
                  Ver agenda e histórico de execuções
                </Link>
                <Link to={`/schedules/${schedule.id}/edit`} className="text-accent hover:underline">
                  Editar
                </Link>
              </p>
            </CardContent>
          </Card>
        ))}
      </div>

      <p className="text-xs text-muted">
        Agendas só executam com o runtime responsável ligado. Fechar a aba não interrompe o
        backend; no desktop, manter na bandeja preserva a execução.
      </p>

      <ConfirmDialog
        open={pendingArchive !== null}
        onOpenChange={(open) => {
          if (!open) setPendingArchive(null);
        }}
        title="Arquivar agenda?"
        description={
          pendingArchive
            ? `“${pendingArchive.name}” será pausada e arquivada. O histórico permanece.`
            : ''
        }
        confirmLabel="Arquivar"
        onConfirm={() => {
          if (!pendingArchive) return;
          actions.archive.mutate(pendingArchive.id, {
            onSuccess: () => toast({ title: 'Agenda arquivada', variant: 'success' }),
            onError: (error) =>
              toast({ title: 'Falha', description: explainError(error), variant: 'error' }),
          });
          setPendingArchive(null);
        }}
      />
      <p className="text-xs text-muted">
        Estados conhecidos: {Object.values(RUN_STATE_LABELS).join(' · ')}
      </p>
    </div>
  );
}
