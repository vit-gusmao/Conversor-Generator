import { useEffect, useRef } from 'react';
import type { ConversionEvent } from '@pdf2x/contracts';
import { Badge } from '@/components/ui/badge';
import { EmptyBlock } from '@/components/layout/states';
import type { ConnectionState } from '@/hooks/use-conversion-events';
import { formatTime } from '@/lib/format';
import { STAGE_LABELS } from '@/lib/labels';
import { cn } from '@/lib/utils';

const EVENT_TONE: Record<string, string> = {
  issue: 'text-[#ffe08a]',
  failed: 'text-[#ffb4b4]',
  decision: 'text-[#c4b5fd]',
  completed: 'text-[#8ef0cd]',
};

export function EventLog({
  events,
  connection,
  className,
}: {
  events: ConversionEvent[];
  connection: ConnectionState;
  className?: string;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const lastCount = useRef(0);

  useEffect(() => {
    if (events.length !== lastCount.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
      lastCount.current = events.length;
    }
  }, [events.length]);

  const statusLabel: Record<ConnectionState, string> = {
    idle: 'Aguardando',
    connecting: 'Conectando',
    open: 'Ao vivo',
    reconnecting: 'Reconectando',
    closed: 'Encerrado',
    error: 'Sem conexão',
  };

  return (
    <div className={cn('flex min-h-0 flex-col', className)}>
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-foreground">Registro de eventos</h3>
        <span
          className="inline-flex items-center gap-1.5 text-[11px] text-muted-2"
          aria-live="polite"
        >
          <span
            aria-hidden="true"
            className={cn(
              'size-1.5 rounded-full',
              connection === 'open' && 'bg-success',
              connection === 'connecting' && 'bg-warning',
              connection === 'reconnecting' && 'bg-warning animate-pulse motion-reduce:animate-none',
              connection === 'error' && 'bg-danger',
              (connection === 'idle' || connection === 'closed') && 'bg-muted-2',
            )}
          />
          {statusLabel[connection]}
        </span>
      </div>
      <div
        ref={scrollRef}
        role="log"
        aria-label="Eventos da conversão"
        className="max-h-72 min-h-32 overflow-y-auto rounded-xl border border-border bg-background/60 p-2 scrollbar-thin"
      >
        {events.length === 0 ? (
          <EmptyBlock
            title="Nenhum evento recebido ainda"
            description="Os eventos aparecem aqui assim que o motor começa a processar o arquivo."
            className="border-0 bg-transparent py-6"
          />
        ) : (
          <ul className="space-y-1">
            {events.map((event) => (
              <li
                key={`${event.sequence}-${event.event_id}`}
                className="flex items-start gap-2 rounded-lg px-2 py-1.5 text-xs hover:bg-surface-2/60"
              >
                <time
                  dateTime={event.at}
                  className="w-16 shrink-0 pt-0.5 font-mono text-[10px] text-muted-2 tabular-nums"
                >
                  {formatTime(event.at)}
                </time>
                <Badge variant="secondary" className="shrink-0 px-1.5 py-0 text-[10px]">
                  {STAGE_LABELS[event.stage] ?? event.stage}
                </Badge>
                <span className={cn('min-w-0 break-words text-foreground/80', EVENT_TONE[event.type])}>
                  {event.message || event.type}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
