import { Link, useParams } from 'react-router-dom';
import { Ban, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { PageHeader } from '@/components/layout/page-header';
import { ErrorBlock } from '@/components/layout/states';
import { useEnsureSession } from '@/hooks/use-session';
import { useCancelRun, useRun } from '@/hooks/use-schedules';
import { useScheduleRunEvents, type RunStreamMode } from '@/hooks/use-schedule-run-events';
import { explainError } from '@/lib/errors';
import { formatDateTime, formatNumber } from '@/lib/format';
import {
  RUN_PHASE_LABELS,
  RUN_TRIGGER_LABELS,
  eventLabel,
  isRunTerminal,
  runStateLabel,
  runStateVariant,
} from '@/lib/schedule-labels';
import { toast } from '@/stores/ui';

export function ScheduleRunPage() {
  useEnsureSession();
  const { id } = useParams<{ id: string }>();
  // The stream is the primary source for the timeline. The run row is polled
  // only while the stream is not healthy (see useRun's `poll`).
  const stream = useScheduleRunEvents(id ?? null);
  const run = useRun(id ?? null, {
    poll: stream.mode !== 'sse' && stream.mode !== 'closed',
  });
  const cancel = useCancelRun();

  const terminal = isRunTerminal(run.data?.state);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Execução"
        description="Horário previsto, fases, arquivos e eventos desta ocorrência."
        backTo={{ to: '/schedules', label: 'Agendamentos' }}
        actions={
          run.data && !terminal ? (
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                cancel.mutate(run.data!.id, {
                  onSuccess: () =>
                    toast({
                      title: 'Cancelamento solicitado',
                      description: 'Resultados já publicados permanecem no histórico.',
                      variant: 'info',
                    }),
                  onError: (error) =>
                    toast({ title: 'Falha', description: explainError(error), variant: 'error' }),
                })
              }
            >
              <Ban className="mr-1 h-4 w-4" aria-hidden /> Cancelar
            </Button>
          ) : null
        }
      />

      {run.isError ? <ErrorBlock message={explainError(run.error)} onRetry={() => void run.refetch()} /> : null}

      {run.data ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex flex-wrap items-center gap-2 text-base">
              <RefreshCw className="h-4 w-4" aria-hidden />
              <Badge variant={runStateVariant(run.data.state)}>{runStateLabel(run.data.state)}</Badge>
              {run.data.phase ? (
                <span className="text-sm text-muted">
                  fase: {RUN_PHASE_LABELS[run.data.phase] ?? run.data.phase}
                </span>
              ) : null}
            </CardTitle>
          </CardHeader>
          <CardContent className="grid gap-3 text-sm sm:grid-cols-2">
            <Field label="Prevista para" value={formatDateTime(run.data.scheduled_for)} />
            <Field
              label="Disparo"
              value={RUN_TRIGGER_LABELS[run.data.trigger] ?? run.data.trigger}
            />
            <Field label="Revisão da agenda" value={`v${run.data.schedule_revision}`} />
            <Field label="Tentativa" value={formatNumber(run.data.attempt)} />
            <Field label="Início" value={run.data.started_at ? formatDateTime(run.data.started_at) : '—'} />
            <Field label="Fim" value={run.data.finished_at ? formatDateTime(run.data.finished_at) : '—'} />
            <Field label="Importação" value={run.data.import_id ?? '—'} />
            <Field label="Indexação" value={run.data.index_job_id ?? '—'} />
            <div className="space-y-0.5">
              <p className="text-xs uppercase tracking-wide text-muted">Histórico da agenda</p>
              <Link
                to={`/schedules/${run.data.schedule_id}`}
                className="text-accent hover:underline"
              >
                Ver agenda e outras execuções
              </Link>
            </div>
            {run.data.error ? (
              <div className="sm:col-span-2">
                <p className="text-xs uppercase tracking-wide text-muted">Erro</p>
                <p className="text-danger">
                  {run.data.error.code}: {run.data.error.message}
                </p>
              </div>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-2 space-y-0">
          <CardTitle className="text-base">Linha do tempo</CardTitle>
          <TransportBadge mode={stream.mode} />
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          {stream.error ? (
            <p className="text-xs text-danger" role="status">
              Não foi possível sincronizar eventos: {stream.error}
            </p>
          ) : null}
          {stream.events.length === 0 ? (
            <p className="text-muted">Nenhum evento registrado ainda.</p>
          ) : (
            <ol className="max-h-96 space-y-1 overflow-auto" aria-label="Eventos da execução">
              {stream.events.map((event) => (
                <li
                  key={`${event.run_id}-${event.sequence}`}
                  data-testid="run-event"
                  className="flex items-start gap-2 py-1"
                >
                  <span className="w-12 shrink-0 text-xs text-muted">#{event.sequence}</span>
                  <div className="min-w-0">
                    <p className="font-medium text-foreground">{eventLabel(event.type)}</p>
                    {event.created_at ? (
                      <p className="truncate text-xs text-muted">{formatDateTime(event.created_at)}</p>
                    ) : null}
                  </div>
                </li>
              ))}
            </ol>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function TransportBadge({ mode }: { mode: RunStreamMode }) {
  switch (mode) {
    case 'sse':
      return <Badge variant="success">ao vivo</Badge>;
    case 'connecting':
      return <Badge variant="muted">conectando…</Badge>;
    case 'reconnecting':
      return <Badge variant="warning">reconectando…</Badge>;
    case 'polling':
      return <Badge variant="warning">atualizando a cada 2 s (sem stream)</Badge>;
    case 'closed':
      return <Badge variant="muted">stream encerrado</Badge>;
    default:
      return null;
  }
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="space-y-0.5">
      <p className="text-xs uppercase tracking-wide text-muted">{label}</p>
      <p className="break-words text-foreground">{value}</p>
    </div>
  );
}
