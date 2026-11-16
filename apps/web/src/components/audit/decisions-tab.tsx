import type { AuditSummary } from '@pdf2x/contracts';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { EmptyBlock } from '@/components/layout/states';
import { formatDateTime, formatNumber } from '@/lib/format';
import { STAGE_LABELS } from '@/lib/labels';

export function DecisionsTab({ summary }: { summary: AuditSummary }) {
  const decisions = summary.decisions;

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>Decisões registradas</CardTitle>
          <span className="text-xs text-muted-2">{formatNumber(decisions.length)} decisões</span>
        </div>
      </CardHeader>
      <CardContent className="pt-0">
        {decisions.length === 0 ? (
          <EmptyBlock
            title="Nenhuma decisão registrada"
            description="Esta execução não publicou decisões de resolução de campos, tabelas ou motores."
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Etapa</TableHead>
                <TableHead>Assunto</TableHead>
                <TableHead>Algoritmo</TableHead>
                <TableHead>Motivos</TableHead>
                <TableHead>Resultado</TableHead>
                <TableHead className="text-right">Alternativas</TableHead>
                <TableHead className="text-right">Página</TableHead>
                <TableHead>Quando</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {decisions.map((decision) => (
                <TableRow key={decision.decision_id}>
                  <TableCell>
                    <Badge variant="secondary">{STAGE_LABELS[decision.stage] ?? decision.stage}</Badge>
                  </TableCell>
                  <TableCell className="max-w-64 text-sm">{decision.subject}</TableCell>
                  <TableCell className="font-mono text-[11px] text-muted">{decision.algorithm}</TableCell>
                  <TableCell>
                    <div className="flex max-w-72 flex-wrap gap-1">
                      {decision.reason_codes.length === 0 ? (
                        <span className="text-xs text-muted-2">—</span>
                      ) : (
                        decision.reason_codes.map((code) => (
                          <Badge key={code} variant="outline" className="text-[10px]">
                            {code}
                          </Badge>
                        ))
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge variant={decision.accepted ? 'success' : 'muted'}>
                      {decision.accepted ? 'aceita' : 'rejeitada'}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{formatNumber(decision.alternatives)}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {decision.page_number ?? '—'}
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-muted-2">
                    {formatDateTime(decision.at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
