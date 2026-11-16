import type { AuditSummary } from '@pdf2x/contracts';
import { CheckCircle2, ShieldAlert } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { QualityBadge } from '@/components/status-badge';
import { EmptyBlock } from '@/components/layout/states';
import { formatDuration, formatNumber } from '@/lib/format';
import { formatPercent } from '@/lib/status';

const CHECK_TONE: Record<string, 'success' | 'danger' | 'warning' | 'muted'> = {
  pass: 'success',
  fail: 'danger',
  warn: 'warning',
  skip: 'muted',
};

export function OverviewTab({ summary }: { summary: AuditSummary }) {
  const m = summary.metrics;
  const metrics = [
    { label: 'Páginas analisadas', value: `${formatNumber(m.pages_analyzed)} / ${formatNumber(m.pages_total)}` },
    { label: 'Blocos atribuídos', value: `${formatNumber(m.blocks_assigned)} / ${formatNumber(m.blocks_total)}` },
    { label: 'Blocos preservados', value: formatNumber(m.blocks_preserved) },
    { label: 'Blocos não classificados', value: formatNumber(m.blocks_unclassified) },
    { label: 'Células geradas', value: formatNumber(m.cells_generated) },
    { label: 'Tabelas detectadas', value: formatNumber(m.tables_detected) },
    { label: 'Campos detectados', value: formatNumber(m.fields_detected) },
    { label: 'Registros detectados', value: formatNumber(m.records_detected) },
    { label: 'Regiões de OCR', value: formatNumber(m.ocr_regions) },
    { label: 'Cobertura textual', value: formatPercent(m.coverage_percent / 100, 1) },
    { label: 'Atribuição', value: formatPercent(m.assignment_percent / 100, 1) },
    { label: 'Eventos auditados', value: formatNumber(m.events_total) },
  ];

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle>Resumo da execução</CardTitle>
            <QualityBadge quality={summary.quality} />
          </div>
        </CardHeader>
        <CardContent className="space-y-4 pt-0">
          <div
            className={
              summary.hash_chain_valid
                ? 'flex items-center gap-2 rounded-xl border border-success/30 bg-success-soft/40 px-3 py-2 text-sm text-[#c8f7e7]'
                : 'flex items-center gap-2 rounded-xl border border-danger/30 bg-danger-soft/40 px-3 py-2 text-sm text-[#ffd0d0]'
            }
            role="status"
          >
            {summary.hash_chain_valid ? (
              <CheckCircle2 className="size-4 text-success" aria-hidden="true" />
            ) : (
              <ShieldAlert className="size-4 text-danger" aria-hidden="true" />
            )}
            {summary.hash_chain_valid
              ? 'Cadeia de hashes válida — os eventos não foram alterados desde o registro.'
              : 'Cadeia de hashes inválida — a sequência de eventos pode ter sido alterada.'}
          </div>

          <dl className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
            {metrics.map((metric) => (
              <div key={metric.label} className="rounded-xl border border-border bg-surface-2/40 p-3">
                <dt className="text-[11px] uppercase tracking-wide text-muted-2">{metric.label}</dt>
                <dd className="mt-1 text-lg font-semibold tabular-nums text-foreground">{metric.value}</dd>
              </div>
            ))}
          </dl>

          <div className="flex flex-wrap gap-2 text-xs">
            <Badge variant="danger">{formatNumber(m.errors)} erros</Badge>
            <Badge variant="warning">{formatNumber(m.warnings)} avisos</Badge>
            <Badge variant="secondary">{formatNumber(m.blocks_dropped)} blocos descartados</Badge>
            <Badge variant="secondary">{formatNumber(m.issues_total)} inconsistências</Badge>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle>Motores utilizados</CardTitle>
        </CardHeader>
        <CardContent className="pt-0">
          {summary.engines.length === 0 ? (
            <EmptyBlock title="Nenhum motor registrado" description="A execução não publicou uso de motores." />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Motor</TableHead>
                  <TableHead>Tipo</TableHead>
                  <TableHead className="text-right">Regiões</TableHead>
                  <TableHead className="text-right">Páginas</TableHead>
                  <TableHead className="text-right">Aceitos</TableHead>
                  <TableHead className="text-right">Rejeitados</TableHead>
                  <TableHead className="text-right">Duração</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {summary.engines.map((engine) => (
                  <TableRow key={`${engine.engine}-${engine.kind}`}>
                    <TableCell className="font-mono text-xs">{engine.engine}</TableCell>
                    <TableCell>
                      <Badge variant="outline">{engine.kind}</Badge>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{formatNumber(engine.regions)}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatNumber(engine.pages)}</TableCell>
                    <TableCell className="text-right tabular-nums text-[#8ef0cd]">
                      {formatNumber(engine.accepted)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums text-[#ffb4b4]">
                      {formatNumber(engine.rejected)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{formatDuration(engine.duration_ms)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {summary.validations.length > 0 ? (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle>Validações finais</CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            <ul className="divide-y divide-border">
              {summary.validations.map((validation) => (
                <li key={validation.check} className="flex flex-wrap items-start gap-3 py-2.5">
                  <Badge variant={CHECK_TONE[validation.status] ?? 'muted'}>{validation.status}</Badge>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm text-foreground">{validation.check}</p>
                    <p className="text-xs text-muted-2">{validation.details}</p>
                  </div>
                  {validation.measured || validation.expected ? (
                    <span className="font-mono text-[11px] text-muted-2">
                      {validation.measured ?? '—'} / {validation.expected ?? '—'}
                    </span>
                  ) : null}
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : null}

      <Separator />
      <p className="text-[11px] text-muted-2">
        Gerado em {new Date(summary.generated_at).toLocaleString('pt-BR')} · run{' '}
        <span className="font-mono">{summary.run_id}</span>
      </p>
    </div>
  );
}
