import type { ConversionStatus } from '@pdf2x/contracts';
import { PIPELINE_STAGES } from '@pdf2x/contracts';
import { Check, Loader2, X } from 'lucide-react';
import { stageRange, stepStateFor } from '@/lib/status';
import { cn } from '@/lib/utils';

export function PipelineStepper({
  status,
  stage,
}: {
  status: ConversionStatus;
  stage?: string | null;
}) {
  return (
    <ol aria-label="Etapas da conversão" className="space-y-1">
      {PIPELINE_STAGES.map((item, index) => {
        const state = stepStateFor(index, status, stage);
        const range = stageRange(index);
        return (
          <li
            key={item.key}
            aria-current={state === 'current' ? 'step' : undefined}
            data-state={state}
            className={cn(
              'flex items-start gap-3 rounded-xl border px-3 py-2.5 transition-colors',
              state === 'current' && 'border-accent/40 bg-accent-soft/30',
              state === 'failed' && 'border-danger/40 bg-danger-soft/25',
              state === 'done' && 'border-transparent',
              state === 'pending' && 'border-transparent opacity-60',
            )}
          >
            <span
              aria-hidden="true"
              className={cn(
                'mt-0.5 grid size-6 shrink-0 place-items-center rounded-full border text-[10px] font-semibold',
                state === 'done' && 'border-success/40 bg-success-soft text-success',
                state === 'current' && 'border-accent/50 bg-accent/15 text-accent',
                state === 'failed' && 'border-danger/50 bg-danger-soft text-danger',
                state === 'pending' && 'border-border-strong text-muted-2',
              )}
            >
              {state === 'done' ? (
                <Check className="size-3.5" />
              ) : state === 'current' ? (
                <Loader2 className="size-3.5 animate-spin motion-reduce:animate-none" />
              ) : state === 'failed' ? (
                <X className="size-3.5" />
              ) : (
                index + 1
              )}
            </span>
            <div className="min-w-0 flex-1">
              <p
                className={cn(
                  'text-sm font-medium',
                  state === 'pending' ? 'text-muted-2' : 'text-foreground',
                )}
              >
                {item.label}
              </p>
              <p className="mt-0.5 text-[11px] text-muted-2">
                Etapa {index + 1} de {PIPELINE_STAGES.length} · peso {Math.round(range.weight * 100)}%
              </p>
            </div>
            <span className="sr-only">
              {state === 'done'
                ? 'concluída'
                : state === 'current'
                  ? 'em andamento'
                  : state === 'failed'
                    ? 'falhou'
                    : 'pendente'}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
