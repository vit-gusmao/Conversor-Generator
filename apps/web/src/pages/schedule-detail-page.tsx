import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { CalendarClock, Pause, Pencil, Play, Trash2, Zap } from 'lucide-react';
import type { AutomationRunRecord } from '@pdf2x/contracts';
import { Button } from '@/components/ui/button';
import { Alert } from '@/components/ui/alert';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { PageHeader } from '@/components/layout/page-header';
import { EmptyBlock, ErrorBlock } from '@/components/layout/states';
import { ConfirmDialog } from '@/components/confirm-dialog';
import { useEnsureSession } from '@/hooks/use-session';
import {
  useSchedule,
  useScheduleActions,
  useScheduleOccurrences,
  useScheduleRuns,
} from '@/hooks/use-schedules';
import { useSources } from '@/hooks/use-sources';
import { explainError } from '@/lib/errors';
import { formatDateTime } from '@/lib/format';
import { createIntentKeys } from '@/lib/intent-keys';
import {
  MISFIRE_LABELS,
  OVERLAP_LABELS,
  RUN_TRIGGER_LABELS,
  ruleLabel,
  runStateLabel,
  runStateVariant,
} from '@/lib/schedule-labels';
import { toast } from '@/stores/ui';

/**
 * Agenda: resumo, próximas ocorrências (mesma regra do disparo, via prévia do
 * servidor) e histórico de execuções. Nenhum timer aqui executa nada: o
 * histórico é apenas lido e o servidor é a única fonte de disparo.
 */
export function ScheduleDetailPage() {
  useEnsureSession();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const schedule = useSchedule(id ?? null);
  const sources = useSources();
  const runs = useScheduleRuns(id ?? null);
  const occurrences = useScheduleOccurrences(schedule.data);
  const actions = useScheduleActions();
  const [confirmArchive, setConfirmArchive] = useState(false);
  const [runKeys] = useState(() => createIntentKeys());

  const data = schedule.data;
  const archived = Boolean(data?.archived);
  const source = (sources.data ?? []).find((item) => item.id === data?.source_id);
  // Only judge availability once the source list has loaded; otherwise we would
  // flash a warning on every visit.
  const sourceUnavailable = Boolean(data && sources.data && (!source || !source.enabled));
  const sortedRuns: AutomationRunRecord[] = [...(runs.data ?? [])].sort((a, b) =>
    b.scheduled_for.localeCompare(a.scheduled_for),
  );

  const reportError = (error: unknown) =>
    toast({ title: 'Falha', description: explainError(error), variant: 'error' });

  return (
    <div className="space-y-6">
      <PageHeader
        title={data?.name ?? 'Agenda'}
        description="Regra, fuso, próximas ocorrências e histórico de execuções."
        backTo={{ to: '/schedules', label: 'Agendamentos' }}
        actions={
          data && !archived ? (
            <>
              <Button asChild variant="outline" size="sm">
                <Link to={`/schedules/${data.id}/edit`}>
                  <Pencil className="mr-1 h-4 w-4" aria-hidden /> Editar
                </Link>
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={actions.runNow.isPending}
                onClick={() =>
                  actions.runNow.mutate(
                    { scheduleId: data.id, idempotencyKey: runKeys.keyFor(data.id) },
                    {
                      onSuccess: (result) => {
                        runKeys.settle(data.id);
                        if (result.executor_active === false) {
                          toast({
                            title: 'Agendado; será executado quando o executor estiver ativo',
                            variant: 'success',
                          });
                          return;
                        }
                        toast({
                          title: 'Execução iniciada',
                          description: `Ocorrência ${runStateLabel(result.state).toLowerCase()}. Acompanhe no histórico.`,
                          variant: 'success',
                        });
                      },
                      onError: reportError,
                    },
                  )
                }
              >
                <Zap className="mr-1 h-4 w-4" aria-hidden /> Executar agora
              </Button>
              {data.enabled ? (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    actions.pause.mutate(data.id, {
                      onSuccess: () => toast({ title: 'Agenda pausada', variant: 'success' }),
                      onError: reportError,
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
                    actions.resume.mutate(data.id, {
                      onSuccess: () =>
                        toast({
                          title: 'Agenda retomada',
                          description: 'O próximo horário futuro foi recalculado.',
                          variant: 'success',
                        }),
                      onError: reportError,
                    })
                  }
                >
                  <Play className="mr-1 h-4 w-4" aria-hidden /> Retomar
                </Button>
              )}
              <Button
                variant="ghost"
                size="sm"
                aria-label={`Arquivar ${data.name}`}
                onClick={() => setConfirmArchive(true)}
              >
                <Trash2 className="h-4 w-4" aria-hidden />
              </Button>
            </>
          ) : null
        }
      />

      {schedule.isError ? (
        <ErrorBlock message={explainError(schedule.error)} onRetry={() => void schedule.refetch()} />
      ) : null}
      {schedule.isLoading ? <p className="text-sm text-muted">Carregando…</p> : null}
      {!schedule.isLoading && !schedule.isError && !data ? (
        <EmptyBlock title="Agenda não encontrada" description="Ela pode ter sido arquivada ou pertence a outra sessão." />
      ) : null}

      {data ? (
        <>
          {sourceUnavailable ? (
            <Alert variant="warning" title="Fonte indisponível">
              A fonte desta agenda foi desativada, arquivada ou não existe mais. As próximas execuções
              falharão até que a fonte seja corrigida ou a agenda seja editada.
            </Alert>
          ) : null}

          <Card>
            <CardHeader>
              <CardTitle className="flex flex-wrap items-center gap-2 text-base">
                <CalendarClock className="h-4 w-4" aria-hidden />
                {archived ? (
                  <Badge variant="muted">arquivada</Badge>
                ) : data.enabled ? (
                  <Badge variant="success">ativa</Badge>
                ) : (
                  <Badge variant="warning">pausada</Badge>
                )}
                <Badge variant="muted">revisão {data.revision}</Badge>
              </CardTitle>
            </CardHeader>
            <CardContent className="grid gap-3 text-sm sm:grid-cols-2">
              <Field label="Regra" value={ruleLabel(data)} />
              <Field label="Fuso horário" value={data.timezone} />
              <Field
                label="Próxima execução"
                value={data.next_run_at && !archived ? formatDateTime(data.next_run_at) : '—'}
              />
              <div className="space-y-0.5">
                <p className="text-xs uppercase tracking-wide text-muted">Fonte</p>
                <Link to={`/sources/${data.source_id}`} className="break-words text-accent hover:underline">
                  {source?.name ?? data.source_id}
                </Link>
              </div>
              <Field label="Horários perdidos" value={MISFIRE_LABELS[data.misfire_policy] ?? data.misfire_policy} />
              <Field label="Sobreposição" value={OVERLAP_LABELS[data.overlap_policy] ?? data.overlap_policy} />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Próximas 5 ocorrências</CardTitle>
            </CardHeader>
            <CardContent className="text-sm">
              {archived ? (
                <p className="text-muted">Agenda arquivada não tem próximas ocorrências.</p>
              ) : occurrences.isLoading ? (
                <p className="text-muted">Calculando…</p>
              ) : occurrences.isError ? (
                <p className="text-danger">{explainError(occurrences.error)}</p>
              ) : (occurrences.data?.next_occurrences ?? []).length === 0 ? (
                <p className="text-muted">Nenhuma ocorrência futura.</p>
              ) : (
                <ul className="grid gap-1">
                  {(occurrences.data?.next_occurrences ?? []).map((instant) => (
                    <li key={instant} className="text-foreground">
                      {formatDateTime(instant)}{' '}
                      <span className="text-xs text-muted">({occurrences.data?.timezone})</span>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Histórico de execuções</CardTitle>
            </CardHeader>
            <CardContent className="text-sm">
              {runs.isLoading ? <p className="text-muted">Carregando…</p> : null}
              {runs.isError ? (
                <ErrorBlock message={explainError(runs.error)} onRetry={() => void runs.refetch()} />
              ) : null}
              {!runs.isLoading && !runs.isError && sortedRuns.length === 0 ? (
                <p className="text-muted">Nenhuma execução registrada ainda.</p>
              ) : null}
              {sortedRuns.length > 0 ? (
                <ul className="divide-y divide-border/60">
                  {sortedRuns.map((run) => (
                    <li key={run.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                      <Link to={`/schedule-runs/${run.id}`} className="text-accent hover:underline">
                        {formatDateTime(run.scheduled_for)}
                      </Link>
                      <span className="flex flex-wrap items-center gap-2 text-xs text-muted">
                        <Badge variant={runStateVariant(run.state)}>{runStateLabel(run.state)}</Badge>
                        <span>{RUN_TRIGGER_LABELS[run.trigger] ?? run.trigger}</span>
                        {run.error ? <span className="text-danger">{run.error.code}</span> : null}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : null}
            </CardContent>
          </Card>
        </>
      ) : null}

      <ConfirmDialog
        open={confirmArchive}
        onOpenChange={setConfirmArchive}
        title="Arquivar agenda?"
        description={data ? `“${data.name}” será pausada e arquivada. O histórico permanece.` : ''}
        confirmLabel="Arquivar"
        onConfirm={() => {
          if (!data) return;
          setConfirmArchive(false);
          actions.archive.mutate(data.id, {
            onSuccess: () => {
              toast({ title: 'Agenda arquivada', variant: 'success' });
              navigate('/schedules');
            },
            onError: reportError,
          });
        }}
      />
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="space-y-0.5">
      <p className="text-xs uppercase tracking-wide text-muted">{label}</p>
      <p className="break-words text-foreground">{value}</p>
    </div>
  );
}
