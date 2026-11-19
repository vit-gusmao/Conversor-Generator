import { useParams } from 'react-router-dom';
import { Ban, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { PageHeader } from '@/components/layout/page-header';
import { ErrorBlock } from '@/components/layout/states';
import { useEnsureSession } from '@/hooks/use-session';
import { useCancelRun, useRun, useRunEvents } from '@/hooks/use-schedules';
import { explainError } from '@/lib/errors';
import { formatDateTime, formatNumber } from '@/lib/format';
import { toast } from '@/stores/ui';

const STATE_LABELS: Record<string, string> = {
  queued: 'na fila',
  running: 'executando',
  waiting_children: 'aguardando conversões',
  cancelling: 'cancelando',
  succeeded: 'sucesso',
  partial: 'parcial',
  no_changes: 'sem novidades',
  failed: 'falhou',
  cancelled: 'cancelada',
  skipped: 'ignorada',
};

const PHASE_LABELS: Record<string, string> = {
  scan: 'varredura',
  snapshot: 'captura',
  submit: 'envio',
  convert: 'conversão',
  index: 'indexação',
  finalize: 'finalização',
};

export function ScheduleRunPage() {
  useEnsureSession();
  const { id } = useParams<{ id: string }>();
  const run = useRun(id ?? null);
  const events = useRunEvents(id ?? null);
  const cancel = useCancelRun();

  const terminal = run.data
    ? ['succeeded', 'partial', 'no_changes', 'failed', 'cancelled', 'skipped'].includes(run.data.state)
    : false;

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

      {run.isError ? <ErrorBlock message={explainError(run.error)} /> : null}

      {run.data ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex flex-wrap items-center gap-2 text-base">
              <RefreshCw className="h-4 w-4" aria-hidden />
              <Badge variant={run.data.state === 'failed' ? 'danger' : 'info'}>
                {STATE_LABELS[run.data.state] ?? run.data.state}
              </Badge>
              {run.data.phase ? (
                <span className="text-sm text-muted">
                  fase: {PHASE_LABELS[run.data.phase] ?? run.data.phase}
                </span>
              ) : null}
            </CardTitle>
          </CardHeader>
          <CardContent className="grid gap-3 text-sm sm:grid-cols-2">
            <Field label="Prevista para" value={formatDateTime(run.data.scheduled_for)} />
            <Field label="Disparo" value={run.data.trigger} />
            <Field label="Revisão da agenda" value={`v${run.data.schedule_revision}`} />
            <Field label="Tentativa" value={formatNumber(run.data.attempt)} />
            <Field label="Início" value={run.data.started_at ? formatDateTime(run.data.started_at) : '—'} />
            <Field label="Fim" value={run.data.finished_at ? formatDateTime(run.data.finished_at) : '—'} />
            <Field label="Importação" value={run.data.import_id ?? '—'} />
            <Field label="Indexação" value={run.data.index_job_id ?? '—'} />
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
        <CardHeader>
          <CardTitle className="text-base">Eventos</CardTitle>
        </CardHeader>
        <CardContent className="max-h-96 space-y-1 overflow-auto text-sm">
          {(events.data ?? []).length === 0 ? (
            <p className="text-muted">Nenhum evento registrado ainda.</p>
          ) : (
            (events.data ?? []).map((event) => (
              <div key={`${event.run_id}-${event.sequence}`} className="flex items-start gap-2 py-1">
                <span className="w-10 shrink-0 text-xs text-muted">#{event.sequence}</span>
                <div className="min-w-0">
                  <p className="font-medium text-foreground">{event.type}</p>
                  <p className="truncate text-xs text-muted">{formatDateTime(event.created_at)}</p>
                </div>
              </div>
            ))
          )}
        </CardContent>
      </Card>
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
