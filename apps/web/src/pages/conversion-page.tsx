import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import type { Conversion } from '@pdf2x/contracts';
import { isTerminal } from '@pdf2x/contracts';
import {
  ArrowRight,
  Clock,
  FileText,
  Loader2,
  RotateCcw,
  ShieldCheck,
  StopCircle,
  TriangleAlert,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import { Alert } from '@/components/ui/alert';
import { Separator } from '@/components/ui/separator';
import { PageHeader } from '@/components/layout/page-header';
import { ErrorBlock, LoadingBlock } from '@/components/layout/states';
import { StatusBadge, QualityBadge } from '@/components/status-badge';
import { PipelineStepper } from '@/components/pipeline-stepper';
import { EventLog } from '@/components/event-log';
import { ConfirmDialog } from '@/components/confirm-dialog';
import { useConversion, useCancelConversion, useRetryConversion } from '@/hooks/use-conversions';
import { useEnsureSession } from '@/hooks/use-session';
import { useConversionEvents } from '@/hooks/use-conversion-events';
import { useElapsed } from '@/hooks/use-elapsed';
import { explainError } from '@/lib/errors';
import { formatBytes, formatDuration, formatNumber } from '@/lib/format';
import { currentStageLabel, formatPercent, isSuccessful, overallProgress } from '@/lib/status';
import { stageIndexForStatus } from '@pdf2x/contracts';
import { queryKeys } from '@/lib/query-keys';
import { cn } from '@/lib/utils';
import { toast } from '@/stores/ui';

export function ConversionPage() {
  const { id = '' } = useParams<{ id: string }>();
  useEnsureSession();
  const conversionQuery = useConversion(id);

  if (!id) {
    return <ErrorBlock title="Conversão inválida" message="O identificador da conversão não foi informado." />;
  }

  if (conversionQuery.isPending) {
    return (
      <div className="space-y-4">
        <LoadingBlock label="Carregando conversão" />
        <LoadingBlock label="Carregando etapas" />
      </div>
    );
  }

  if (conversionQuery.isError || !conversionQuery.data) {
    return (
      <div className="space-y-4">
        <ErrorBlock
          title="Conversão não encontrada"
          message={explainError(conversionQuery.error)}
          onRetry={() => void conversionQuery.refetch()}
        />
        <Button asChild variant="secondary">
          <Link to="/history">Voltar ao histórico</Link>
        </Button>
      </div>
    );
  }

  const conversion = conversionQuery.data;
  // A retry creates a new run: remount to start a fresh event stream.
  return <LiveConversion key={conversion.run.run_id} conversion={conversion} />;
}

function LiveConversion({ conversion }: { conversion: Conversion }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [cancelOpen, setCancelOpen] = useState(false);

  const conversionUrl = `/conversions/${conversion.conversion_id}`;
  const active = !isTerminal(conversion.status);

  const stream = useConversionEvents(conversion.conversion_id, {
    enabled: active,
    onReconnect: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.conversions.detail(conversion.conversion_id) });
    },
    onEvent: (event) => {
      if (isTerminal(event.status)) {
        void queryClient.invalidateQueries({
          queryKey: queryKeys.conversions.detail(conversion.conversion_id),
        });
      }
    },
  });

  const status = stream.lastEvent?.status ?? conversion.status;
  const stage = stream.lastEvent?.stage ?? conversion.stage;
  const indeterminate = stream.lastEvent
    ? stream.lastEvent.progress_indeterminate
    : conversion.progress_indeterminate;
  const reportedProgress = stream.lastEvent?.progress ?? conversion.progress;
  const percent = overallProgress({ status, progress: reportedProgress }) * 100;
  const success = isSuccessful(status);

  const startedAt = conversion.run.started_at ?? conversion.created_at;
  const finishedAt =
    isTerminal(status) ? (conversion.run.finished_at ?? conversion.updated_at) : conversion.run.finished_at;
  const elapsed = useElapsed(startedAt, finishedAt);

  const cancelMutation = useCancelConversion(conversion.conversion_id);
  const retryMutation = useRetryConversion(conversion.conversion_id);

  useEffect(() => {
    if (!success) return undefined;
    const timer = window.setTimeout(() => {
      navigate(`${conversionUrl}/result`, { replace: true });
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [success, conversionUrl, navigate]);

  const error = conversion.error;
  const failed = status === 'failed' || status === 'cancelled';
  const stageIndex = stageIndexForStatus(status);

  async function handleCancel() {
    try {
      await cancelMutation.mutateAsync(undefined);
      toast({
        title: 'Cancelamento solicitado',
        description: 'A conversão será interrompida com segurança.',
        variant: 'warning',
      });
      await queryClient.invalidateQueries({
        queryKey: queryKeys.conversions.detail(conversion.conversion_id),
      });
    } catch (cause) {
      toast({ title: 'Não foi possível cancelar', description: explainError(cause), variant: 'error' });
    }
  }

  function handleRetry() {
    retryMutation.mutate(undefined, {
      onSuccess: (response) => {
        toast({ title: 'Nova tentativa iniciada', description: 'Um novo run foi criado para este documento.', variant: 'info' });
        if (response.conversion_id && response.conversion_id !== conversion.conversion_id) {
          navigate(`/conversions/${response.conversion_id}`);
        } else {
          void queryClient.invalidateQueries({
            queryKey: queryKeys.conversions.detail(conversion.conversion_id),
          });
        }
      },
      onError: (cause) => {
        toast({ title: 'Não foi possível repetir', description: explainError(cause), variant: 'error' });
      },
    });
  }

  const stats = conversion.stats;

  return (
    <div>
      <PageHeader
        backTo={{ to: '/history', label: 'Histórico' }}
        eyebrow="Processamento"
        title={conversion.document.filename}
        description={
          <span className="flex flex-wrap items-center gap-2 text-xs text-muted-2">
            <span>{formatBytes(conversion.document.size_bytes)}</span>
            {conversion.document.page_count !== null ? (
              <span>· {formatNumber(conversion.document.page_count)} páginas</span>
            ) : null}
            <span>· enviado {new Date(conversion.created_at).toLocaleString('pt-BR')}</span>
          </span>
        }
        actions={
          <>
            <StatusBadge status={status} showDot />
            {conversion.quality !== 'pending' ? <QualityBadge quality={conversion.quality} /> : null}
            {active ? (
              <Button
                variant="outline"
                size="sm"
                onClick={() => setCancelOpen(true)}
                disabled={cancelMutation.isPending}
              >
                <StopCircle aria-hidden="true" />
                Cancelar
              </Button>
            ) : (
              <>
                <Button asChild variant="secondary" size="sm">
                  <Link to={`${conversionUrl}/audit`}>
                    <ShieldCheck aria-hidden="true" />
                    Ver auditoria
                  </Link>
                </Button>
                {success ? (
                  <Button asChild size="sm">
                    <Link to={`${conversionUrl}/result`}>
                      Ver resultado
                      <ArrowRight aria-hidden="true" />
                    </Link>
                  </Button>
                ) : null}
              </>
            )}
          </>
        }
      />

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <div className="space-y-4">
          <Card>
            <CardContent className="space-y-4 p-5 pt-5 sm:p-5">
              <div className="flex flex-wrap items-end justify-between gap-2">
                <div>
                  <p className="text-xs font-medium uppercase tracking-wide text-muted-2">Etapa atual</p>
                  <p className="mt-1 text-lg font-semibold text-foreground">
                    {currentStageLabel(status, stage)}
                  </p>
                </div>
                <div className="text-right">
                  <p className="text-xs text-muted-2">Progresso</p>
                  <p
                    className="text-2xl font-semibold tabular-nums text-foreground"
                    aria-live="polite"
                  >
                    {indeterminate && active ? '—' : formatPercent(percent / 100)}
                  </p>
                </div>
              </div>

              <Progress value={percent} indeterminate={Boolean(indeterminate) && active} />

              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-2">
                <span className="inline-flex items-center gap-1">
                  <Clock className="size-3.5" aria-hidden="true" />
                  {formatDuration(elapsed)} decorridos
                </span>
                <span>
                  {stageIndex >= 0 ? `Etapa ${Math.min(stageIndex + 1, 8)} de 8` : 'Pipeline finalizado'}
                </span>
                {conversion.run.attempt > 1 ? (
                  <span>Tentativa {conversion.run.attempt}</span>
                ) : null}
                {indeterminate && active ? <span>Duração estimada indisponível nesta etapa</span> : null}
              </div>
            </CardContent>
          </Card>

          {failed ? (
            <Card className="border-danger/40">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-[#ffd0d0]">
                  <TriangleAlert className="size-4 text-danger" aria-hidden="true" />
                  {status === 'cancelled' ? 'Conversão cancelada' : 'A conversão falhou'}
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                {error ? (
                  <Alert variant="error" title={`Código: ${error.code}`}>
                    {error.message}
                    {error.detail ? <span className="mt-1 block opacity-80">{error.detail}</span> : null}
                  </Alert>
                ) : (
                  <Alert variant="error">
                    O processamento terminou sem produzir resultado. A auditoria permanece disponível.
                  </Alert>
                )}
                <div className="flex flex-wrap gap-2">
                  <Button onClick={handleRetry} disabled={retryMutation.isPending}>
                    {retryMutation.isPending ? (
                      <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
                    ) : (
                      <RotateCcw aria-hidden="true" />
                    )}
                    Tentar novamente
                  </Button>
                  <Button asChild variant="secondary">
                    <Link to={`${conversionUrl}/audit`}>
                      <ShieldCheck aria-hidden="true" />
                      Investigar na auditoria
                    </Link>
                  </Button>
                </div>
                <p className="text-[11px] text-muted-2">
                  Uma nova tentativa cria outro run e preserva os eventos da tentativa anterior.
                </p>
              </CardContent>
            </Card>
          ) : null}

          {success ? (
            <Alert variant="success" title={status === 'completed_with_warnings' ? 'Concluído com avisos' : 'Conversão concluída'}>
              <div className="flex flex-wrap items-center gap-2">
                <span>Abrindo o resultado…</span>
                <Button asChild size="sm" variant="secondary">
                  <Link to={`${conversionUrl}/result`}>Ver resultado agora</Link>
                </Button>
              </div>
            </Alert>
          ) : null}

          <Card>
            <CardHeader className="pb-0">
              <CardTitle>Etapas do pipeline</CardTitle>
            </CardHeader>
            <CardContent className="pt-3">
              <PipelineStepper status={status} stage={stage} />
            </CardContent>
          </Card>
        </div>

        <div className="space-y-4">
          <Card>
            <CardContent className="space-y-3 p-5 pt-5 sm:p-5">
              <h2 className="text-sm font-semibold text-foreground">Documento e execução</h2>
              <dl className="space-y-2 text-xs">
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-muted-2">Conversão</dt>
                  <dd className="font-mono text-foreground/80">{conversion.conversion_id.slice(0, 8)}…</dd>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-muted-2">Run</dt>
                  <dd className="font-mono text-foreground/80">{conversion.run.run_id.slice(0, 8)}…</dd>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-muted-2">Páginas processadas</dt>
                  <dd className="tabular-nums text-foreground/80">
                    {stats ? `${formatNumber(stats.pages_processed)}/${formatNumber(stats.pages_total)}` : '—'}
                  </dd>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-muted-2">Tabelas</dt>
                  <dd className="tabular-nums text-foreground/80">{stats ? formatNumber(stats.tables) : '—'}</dd>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-muted-2">Células</dt>
                  <dd className="tabular-nums text-foreground/80">{stats ? formatNumber(stats.cells) : '—'}</dd>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-muted-2">Cobertura textual</dt>
                  <dd className="tabular-nums text-foreground/80">
                    {stats ? `${stats.coverage_percent.toFixed(1)}%` : '—'}
                  </dd>
                </div>
              </dl>
              <Separator />
              <div className="flex items-start gap-2 text-[11px] text-muted-2">
                <FileText className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
                <span>
                  Os eventos são persistidos antes de qualquer publicação. A interface apenas acompanha o
                  que já foi registrado.
                </span>
              </div>
            </CardContent>
          </Card>

          <Card className={cn('p-0')}>
            <CardContent className="p-4 pt-4 sm:p-4">
              <EventLog events={stream.events} connection={stream.connection} />
              {stream.connection === 'reconnecting' || stream.error ? (
                <p className="mt-2 text-[11px] text-warning" aria-live="polite">
                  {stream.reconnectAttempts > 0
                    ? `Reconectando (tentativa ${stream.reconnectAttempts})… `
                    : ''}
                  {stream.error ?? ''}
                </p>
              ) : null}
            </CardContent>
          </Card>
        </div>
      </div>

      <ConfirmDialog
        open={cancelOpen}
        onOpenChange={setCancelOpen}
        title="Cancelar esta conversão?"
        description="O motor interrompe o processamento na próxima verificação. Os eventos já registrados permanecem na auditoria."
        confirmLabel="Cancelar conversão"
        cancelLabel="Manter processando"
        destructive
        onConfirm={handleCancel}
      />
    </div>
  );
}
