import { Check, Copy, X } from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import { EmptyBlock, ErrorBlock, LoadingBlock } from '@/components/layout/states';
import { useCellEvidence } from '@/hooks/use-audit';
import { copyText } from '@/lib/download';
import { explainError } from '@/lib/errors';
import { formatNumber } from '@/lib/format';
import { ORIGIN_LABELS, VALUE_TYPE_LABELS } from '@/lib/labels';

export interface ProvenanceDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  conversionId: string;
  sheet: string | null;
  cell: string | null;
  previewValue?: string | null;
}

function ValidationFlag({ label, value }: { label: string; value: boolean }) {
  return (
    <span className="inline-flex items-center gap-1 text-xs text-muted">
      {value ? (
        <Check className="size-3.5 text-success" aria-hidden="true" />
      ) : (
        <X className="size-3.5 text-danger" aria-hidden="true" />
      )}
      {label}
    </span>
  );
}

export function ProvenanceDrawer({
  open,
  onOpenChange,
  conversionId,
  sheet,
  cell,
  previewValue,
}: ProvenanceDrawerProps) {
  const evidence = useCellEvidence(conversionId, { sheet, cell }, open);
  const item = evidence.data?.[0] ?? null;

  const copyPayload = () => {
    if (!item) return;
    void copyText(JSON.stringify(item, null, 2));
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto scrollbar-thin">
        <DialogHeader>
          <DialogTitle>
            Proveniência da célula{' '}
            <span className="font-mono text-accent">
              {sheet ?? '—'}!{cell ?? '—'}
            </span>
          </DialogTitle>
          <DialogDescription>
            Origem, transformações e validações registradas para este valor.
          </DialogDescription>
        </DialogHeader>

        {!sheet || !cell ? (
          <EmptyBlock
            title="Nenhuma célula selecionada"
            description="Clique em uma célula da aba Excel gerado para investigar a origem do valor."
          />
        ) : evidence.isPending ? (
          <LoadingBlock label="Carregando proveniência da célula" />
        ) : evidence.isError ? (
          <ErrorBlock message={explainError(evidence.error)} onRetry={() => void evidence.refetch()} />
        ) : !item ? (
          <div className="space-y-3">
            <EmptyBlock
              title="Sem proveniência registrada"
              description="A API não retornou evidência para esta célula. O valor da prévia é exibido abaixo."
            />
            <div className="rounded-xl border border-border bg-surface-2/60 p-3">
              <p className="text-[11px] uppercase tracking-wide text-muted-2">Valor na prévia</p>
              <p className="mt-1 break-words font-mono text-sm text-foreground">{previewValue ?? '—'}</p>
            </div>
          </div>
        ) : (
          <div className="space-y-4 text-sm">
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-xl border border-border bg-surface-2/60 p-3">
                <p className="text-[11px] uppercase tracking-wide text-muted-2">Valor gravado</p>
                <p className="mt-1 break-words font-mono text-foreground">{item.value ?? '—'}</p>
              </div>
              <div className="rounded-xl border border-border bg-surface-2/60 p-3">
                <p className="text-[11px] uppercase tracking-wide text-muted-2">Valor original</p>
                <p className="mt-1 break-words font-mono text-foreground">{item.original_value ?? '—'}</p>
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="outline">{item.header || 'sem cabeçalho'}</Badge>
              <Badge variant="secondary">{VALUE_TYPE_LABELS[item.value_type] ?? item.value_type}</Badge>
              <span className="text-xs text-muted-2">
                linha {formatNumber(item.row)} · coluna {formatNumber(item.column)}
              </span>
              {item.number_format ? (
                <span className="text-xs text-muted-2">formato {item.number_format}</span>
              ) : null}
            </div>

            <Separator />

            <section aria-labelledby="evidence-source">
              <h4 id="evidence-source" className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-2">
                Origem no PDF
              </h4>
              {item.source ? (
                <dl className="grid gap-2 text-xs sm:grid-cols-2">
                  <div className="rounded-lg border border-border p-2.5">
                    <dt className="text-muted-2">Página</dt>
                    <dd className="font-mono text-foreground">{item.source.page_number}</dd>
                  </div>
                  <div className="rounded-lg border border-border p-2.5">
                    <dt className="text-muted-2">Elemento</dt>
                    <dd className="break-all font-mono text-foreground">{item.source.element_id}</dd>
                  </div>
                  <div className="rounded-lg border border-border p-2.5">
                    <dt className="text-muted-2">bbox [x0, y0, x1, y1]</dt>
                    <dd className="font-mono text-foreground">
                      [{item.source.bbox.map((value) => Math.round(value)).join(', ')}]
                    </dd>
                  </div>
                  <div className="rounded-lg border border-border p-2.5">
                    <dt className="text-muted-2">Origem / motor</dt>
                    <dd className="text-foreground">
                      {ORIGIN_LABELS[item.source.origin] ?? item.source.origin} · {item.source.engine}
                      {item.source.confidence !== null
                        ? ` · confiança ${(item.source.confidence * 100).toFixed(0)}%`
                        : ''}
                    </dd>
                  </div>
                </dl>
              ) : (
                <p className="text-xs text-muted-2">Nenhuma fonte física registrada para esta célula.</p>
              )}
            </section>

            <section aria-labelledby="evidence-transformations">
              <h4
                id="evidence-transformations"
                className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-2"
              >
                Transformações
              </h4>
              {item.transformations.length === 0 ? (
                <p className="text-xs text-muted-2">Nenhuma normalização aplicada.</p>
              ) : (
                <ul className="space-y-2">
                  {item.transformations.map((transformation, index) => (
                    <li key={`${transformation.rule}-${index}`} className="rounded-lg border border-border p-2.5 text-xs">
                      <p className="font-medium text-foreground">{transformation.rule}</p>
                      <p className="mt-1 font-mono text-muted">
                        {transformation.from} → {transformation.to}
                      </p>
                      <p className="mt-0.5 text-muted-2">{transformation.reason}</p>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section aria-labelledby="evidence-validation">
              <h4
                id="evidence-validation"
                className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-2"
              >
                Validação
              </h4>
              <div className="flex flex-wrap gap-x-4 gap-y-2">
                <ValidationFlag label="Fonte preservada" value={item.validation.source_preserved} />
                <ValidationFlag label="Célula escrita" value={item.validation.cell_written} />
                <ValidationFlag label="Valor confere com a fonte" value={item.validation.value_matches_source} />
              </div>
            </section>

            <div className="flex flex-wrap gap-2 border-t border-border pt-3 text-xs text-muted-2">
              {item.record_id ? <span>registro {item.record_id}</span> : null}
              {item.field_label ? <span>campo {item.field_label}</span> : null}
              {item.table_id ? <span>tabela {item.table_id}</span> : null}
            </div>

            <div className="flex justify-end">
              <Button variant="secondary" size="sm" onClick={copyPayload}>
                <Copy aria-hidden="true" />
                Copiar evidência JSON
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
