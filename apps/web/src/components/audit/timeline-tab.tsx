import { useMemo, useState } from 'react';
import type { Severity } from '@pdf2x/contracts';
import { SEVERITIES } from '@pdf2x/contracts';
import { Loader2 } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { EmptyBlock, ErrorBlock, LoadingBlock } from '@/components/layout/states';
import { SeverityBadge } from '@/components/status-badge';
import { useAuditEvents } from '@/hooks/use-audit';
import { useDebouncedValue } from '@/hooks/use-debounced-value';
import { explainError } from '@/lib/errors';
import { formatNumber, formatTime } from '@/lib/format';
import { STAGE_LABELS } from '@/lib/labels';

const STAGE_OPTIONS = [
  'receive',
  'validate',
  'inspect',
  'extract',
  'field_resolution',
  'table_detection',
  'structure',
  'normalize',
  'reconcile',
  'generate',
  'audit',
] as const;

const EVENT_TYPES = [
  'status',
  'stage',
  'progress',
  'log',
  'issue',
  'decision',
  'metrics',
  'completed',
  'failed',
] as const;

export function TimelineTab({ conversionId }: { conversionId: string }) {
  const [stage, setStage] = useState<string>('all');
  const [eventType, setEventType] = useState<string>('all');
  const [severity, setSeverity] = useState<string>('all');
  const [pageInput, setPageInput] = useState('');
  const pageFilter = useDebouncedValue(pageInput, 400);

  const parsedPage = Number.parseInt(pageFilter, 10);
  const filters = useMemo(
    () => ({
      stage: stage === 'all' ? null : stage,
      event_type: eventType === 'all' ? null : eventType,
      severity: severity === 'all' ? null : severity,
      page: Number.isFinite(parsedPage) && parsedPage > 0 ? parsedPage : null,
      limit: 50,
    }),
    [stage, eventType, severity, parsedPage],
  );

  const events = useAuditEvents(conversionId, filters);
  const items = (events.data?.pages ?? [])
    .flatMap((page) => page.items)
    .slice()
    .sort((a, b) => a.sequence - b.sequence);
  const total = events.data?.pages[0]?.total ?? items.length;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-2">
          <CardTitle>Filtros</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3 pt-0 sm:grid-cols-2 lg:grid-cols-4">
          <div className="space-y-1">
            <label className="text-[11px] uppercase tracking-wide text-muted-2" htmlFor="timeline-stage">
              Etapa
            </label>
            <Select value={stage} onValueChange={setStage}>
              <SelectTrigger id="timeline-stage">
                <SelectValue placeholder="Todas" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Todas as etapas</SelectItem>
                {STAGE_OPTIONS.map((item) => (
                  <SelectItem key={item} value={item}>
                    {STAGE_LABELS[item] ?? item}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <label className="text-[11px] uppercase tracking-wide text-muted-2" htmlFor="timeline-type">
              Tipo de evento
            </label>
            <Select value={eventType} onValueChange={setEventType}>
              <SelectTrigger id="timeline-type">
                <SelectValue placeholder="Todos" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Todos os tipos</SelectItem>
                {EVENT_TYPES.map((item) => (
                  <SelectItem key={item} value={item}>
                    {item}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <label className="text-[11px] uppercase tracking-wide text-muted-2" htmlFor="timeline-severity">
              Gravidade
            </label>
            <Select value={severity} onValueChange={setSeverity}>
              <SelectTrigger id="timeline-severity">
                <SelectValue placeholder="Todas" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Todas as gravidades</SelectItem>
                {SEVERITIES.map((item) => (
                  <SelectItem key={item} value={item}>
                    {item}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <label className="text-[11px] uppercase tracking-wide text-muted-2" htmlFor="timeline-page">
              Página
            </label>
            <Input
              id="timeline-page"
              inputMode="numeric"
              placeholder="Todas"
              value={pageInput}
              onChange={(event) => setPageInput(event.target.value.replace(/[^0-9]/g, ''))}
            />
          </div>
        </CardContent>
      </Card>

      {events.isPending ? (
        <LoadingBlock label="Carregando linha do tempo" />
      ) : events.isError ? (
        <ErrorBlock message={explainError(events.error)} onRetry={() => void events.refetch()} />
      ) : items.length === 0 ? (
        <EmptyBlock
          title="Nenhum evento com estes filtros"
          description="Ajuste etapa, tipo, gravidade ou página para ampliar a busca."
        />
      ) : (
        <Card>
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between gap-2">
              <CardTitle>Linha do tempo</CardTitle>
              <span className="text-xs text-muted-2">
                {formatNumber(items.length)} de {formatNumber(total)} eventos
              </span>
            </div>
          </CardHeader>
          <CardContent className="pt-0">
            <ol className="relative space-y-1 border-l border-border pl-4">
              {items.map((event) => (
                <li key={`${event.event_id}-${event.sequence}`} className="relative py-1.5">
                  <span
                    aria-hidden="true"
                    className="absolute -left-[21px] top-2.5 size-2 rounded-full border border-border bg-surface-3"
                  />
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-[10px] text-muted-2 tabular-nums">#{event.sequence}</span>
                    <time dateTime={event.created_at} className="font-mono text-[10px] text-muted-2">
                      {formatTime(event.created_at)}
                    </time>
                    <Badge variant="secondary" className="px-1.5 py-0 text-[10px]">
                      {STAGE_LABELS[event.stage] ?? event.stage}
                    </Badge>
                    <SeverityBadge severity={event.severity as Severity} />
                    {event.page_number !== null ? (
                      <Badge variant="outline" className="px-1.5 py-0 text-[10px]">
                        pág. {event.page_number}
                      </Badge>
                    ) : null}
                    <span className="text-[10px] text-muted-2">{event.event_type}</span>
                  </div>
                  <p className="mt-1 break-words text-sm text-foreground/85">{event.message}</p>
                </li>
              ))}
            </ol>

            {events.hasNextPage ? (
              <div className="mt-3 flex justify-center">
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => void events.fetchNextPage()}
                  disabled={events.isFetchingNextPage}
                >
                  {events.isFetchingNextPage ? (
                    <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
                  ) : null}
                  Carregar mais eventos
                </Button>
              </div>
            ) : null}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
