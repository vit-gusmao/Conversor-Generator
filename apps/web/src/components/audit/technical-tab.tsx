import { useState } from 'react';
import { Copy, Hash, Loader2 } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { EmptyBlock, ErrorBlock, LoadingBlock } from '@/components/layout/states';
import { useAuditEvents } from '@/hooks/use-audit';
import { copyText } from '@/lib/download';
import { explainError } from '@/lib/errors';
import { formatDateTime, formatNumber } from '@/lib/format';
import { toast } from '@/stores/ui';

export function TechnicalTab({ conversionId }: { conversionId: string }) {
  const events = useAuditEvents(conversionId, { limit: 100 });
  const [expanded, setExpanded] = useState<string | null>(null);

  const items = (events.data?.pages ?? []).flatMap((page) => page.items);

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2">
            <Hash className="size-4 text-accent" aria-hidden="true" />
            Auditoria técnica
          </CardTitle>
          <span className="text-xs text-muted-2">
            {formatNumber(items.length)} eventos carregados · sequência e hashes
          </span>
        </div>
      </CardHeader>
      <CardContent className="space-y-3 pt-0">
        {events.isPending ? (
          <LoadingBlock label="Carregando eventos técnicos" />
        ) : events.isError ? (
          <ErrorBlock message={explainError(events.error)} onRetry={() => void events.refetch()} />
        ) : items.length === 0 ? (
          <EmptyBlock title="Nenhum evento" description="A API não retornou eventos auditáveis." />
        ) : (
          <>
            <ul className="space-y-1.5">
              {items.map((event) => {
                const key = `${event.event_id}-${event.sequence}`;
                const open = expanded === key;
                return (
                  <li key={key} className="rounded-xl border border-border bg-surface-2/30">
                    <div className="flex flex-wrap items-center gap-2 px-3 py-2">
                      <span className="font-mono text-[11px] text-muted-2 tabular-nums">
                        #{event.sequence}
                      </span>
                      <Badge variant="secondary" className="px-1.5 py-0 text-[10px]">
                        {event.stage}
                      </Badge>
                      <span className="text-xs text-foreground/85">{event.event_type}</span>
                      <span className="font-mono text-[10px] text-muted-2">
                        {event.event_hash.slice(0, 16)}…
                      </span>
                      <span className="ml-auto text-[10px] text-muted-2">
                        {formatDateTime(event.created_at)}
                      </span>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setExpanded(open ? null : key)}
                        aria-expanded={open}
                      >
                        {open ? 'Ocultar JSON' : 'Ver JSON'}
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Copiar evento ${event.sequence}`}
                        onClick={() => {
                          void copyText(JSON.stringify(event, null, 2)).then((ok) =>
                            toast({
                              title: ok ? 'Evento copiado' : 'Não foi possível copiar',
                              variant: ok ? 'success' : 'error',
                            }),
                          );
                        }}
                      >
                        <Copy aria-hidden="true" />
                      </Button>
                    </div>
                    {open ? (
                      <div className="border-t border-border px-3 py-2">
                        <dl className="mb-2 grid gap-1 text-[10px] text-muted-2 sm:grid-cols-2">
                          <div className="flex gap-1">
                            <dt>event_id:</dt>
                            <dd className="font-mono text-foreground/70">{event.event_id}</dd>
                          </div>
                          <div className="flex gap-1">
                            <dt>run_id:</dt>
                            <dd className="font-mono text-foreground/70">{event.run_id}</dd>
                          </div>
                          <div className="flex min-w-0 gap-1">
                            <dt>event_hash:</dt>
                            <dd className="truncate font-mono text-foreground/70">{event.event_hash}</dd>
                          </div>
                          <div className="flex min-w-0 gap-1">
                            <dt>previous_hash:</dt>
                            <dd className="truncate font-mono text-foreground/70">
                              {event.previous_hash ?? '∅'}
                            </dd>
                          </div>
                        </dl>
                        <pre className="max-h-72 overflow-auto rounded-lg bg-background/70 p-3 text-[11px] leading-relaxed text-muted scrollbar-thin">
                          {JSON.stringify(
                            {
                              message: event.message,
                              severity: event.severity,
                              page_number: event.page_number,
                              region_id: event.region_id,
                              extraction_method: event.extraction_method,
                              payload: event.payload,
                            },
                            null,
                            2,
                          )}
                        </pre>
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ul>
            {events.hasNextPage ? (
              <div className="flex justify-center">
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
          </>
        )}
      </CardContent>
    </Card>
  );
}
