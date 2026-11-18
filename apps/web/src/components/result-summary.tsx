import type { ResultOverview } from '@pdf2x/contracts';
import { AlertTriangle, FileSpreadsheet, LayoutGrid, Rows3, Table2 } from 'lucide-react';
import { Link } from 'react-router-dom';

import { Badge } from '@/components/ui/badge';
import { formatNumber } from '@/lib/format';

export interface ResultSummaryLineProps {
  conversionId: string;
  overview: ResultOverview;
}

const numberFormat = new Intl.NumberFormat('pt-BR');

function metric(value: number, singular: string, plural: string): string {
  return `${numberFormat.format(value)} ${value === 1 ? singular : plural}`;
}

/**
 * One line of metrics instead of four large cards.
 *
 * Hashing, engines and per-stage counts moved to the "Detalhes" section: they
 * are available, just not competing with the document name and the actions.
 */
export function ResultSummaryLine({ conversionId, overview }: ResultSummaryLineProps) {
  const { counts, warning_counts: warnings } = overview;
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-sm text-muted">
      <span className="inline-flex items-center gap-1.5">
        <LayoutGrid className="size-3.5 text-muted-2" aria-hidden="true" />
        {metric(counts.pages, 'página', 'páginas')}
      </span>
      <span className="inline-flex items-center gap-1.5">
        <Table2 className="size-3.5 text-muted-2" aria-hidden="true" />
        {metric(counts.tables, 'tabela', 'tabelas')}
      </span>
      <span className="inline-flex items-center gap-1.5">
        <FileSpreadsheet className="size-3.5 text-muted-2" aria-hidden="true" />
        {metric(counts.cells, 'célula', 'células')}
      </span>
      <span className="inline-flex items-center gap-1.5">
        <Rows3 className="size-3.5 text-muted-2" aria-hidden="true" />
        {metric(counts.records, 'registro', 'registros')}
      </span>
      {overview.partial ? (
        <Badge variant="warning">
          <AlertTriangle aria-hidden="true" />
          {overview.pages_failed > 0
            ? `${formatNumber(overview.pages_failed)} página(s) falharam`
            : 'Resultado parcial'}
        </Badge>
      ) : null}
      {warnings.total > 0 ? (
        <Link
          to={`/conversions/${conversionId}/audit?tab=issues`}
          className="text-xs font-medium text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          {formatNumber(warnings.total)} aviso(s)
        </Link>
      ) : null}
    </div>
  );
}
