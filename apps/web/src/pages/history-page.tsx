import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  ArrowLeft,
  ArrowRight,
  Ellipsis,
  FileSpreadsheet,
  FileText,
  History,
  Search,
  ShieldCheck,
  Trash2,
} from 'lucide-react';
import type { ConversionListItem, ConversionStatus } from '@pdf2x/contracts';
import { CONVERSION_STATUSES } from '@pdf2x/contracts';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { PageHeader } from '@/components/layout/page-header';
import { EmptyBlock, ErrorBlock } from '@/components/layout/states';
import { QualityBadge, StatusBadge } from '@/components/status-badge';
import { ConfirmDialog } from '@/components/confirm-dialog';
import { useConversions, useDeleteConversion } from '@/hooks/use-conversions';
import { useEnsureSession } from '@/hooks/use-session';
import { useDebouncedValue } from '@/hooks/use-debounced-value';
import { explainError } from '@/lib/errors';
import { formatDateTime, formatNumber, truncate } from '@/lib/format';
import { STATUS_LABELS } from '@/lib/labels';
import { progressForStatus } from '@/lib/status';
import { toast } from '@/stores/ui';

const PAGE_SIZE = 20;

export function HistoryPage() {
  useEnsureSession();
  const navigate = useNavigate();
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState<ConversionStatus | 'all'>('all');
  const [page, setPage] = useState(1);
  const [pendingDelete, setPendingDelete] = useState<ConversionListItem | null>(null);
  const debouncedSearch = useDebouncedValue(search, 350);
  const deleteMutation = useDeleteConversion();

  useEffect(() => {
    setPage(1);
  }, [debouncedSearch, status]);

  const history = useConversions({
    page,
    page_size: PAGE_SIZE,
    status: status === 'all' ? null : status,
    q: debouncedSearch,
  });

  const items = history.data?.items ?? [];
  const total = history.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  async function handleDelete() {
    if (!pendingDelete) return;
    try {
      await deleteMutation.mutateAsync(pendingDelete.conversion_id);
      toast({
        title: 'Conversão excluída',
        description: `${pendingDelete.filename} e seus artefatos foram removidos.`,
        variant: 'success',
      });
      setPendingDelete(null);
      if (items.length === 1 && page > 1) setPage(page - 1);
    } catch (cause) {
      toast({ title: 'Falha ao excluir', description: explainError(cause), variant: 'error' });
    }
  }

  return (
    <div>
      <PageHeader
        eyebrow="Histórico"
        title="Suas conversões"
        description="Acompanhe processamentos, baixe resultados e abra a auditoria de qualquer execução — inclusive das que falharam."
        actions={
          <Button asChild>
            <Link to="/">
              <FileText aria-hidden="true" />
              Nova conversão
            </Link>
          </Button>
        }
      />

      <Card className="mb-4">
        <CardContent className="flex flex-col gap-3 p-4 pt-4 sm:flex-row sm:items-center sm:p-4">
          <div className="relative flex-1">
            <Search
              className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-2"
              aria-hidden="true"
            />
            <Input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Buscar por nome do arquivo…"
              aria-label="Buscar conversões"
              className="pl-9"
            />
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-2" id="status-filter-label">
              Status
            </span>
            <div className="w-44">
              <Select
                value={status}
                onValueChange={(value) => setStatus(value as ConversionStatus | 'all')}
              >
                <SelectTrigger aria-labelledby="status-filter-label">
                  <SelectValue placeholder="Todos" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">Todos</SelectItem>
                  {CONVERSION_STATUSES.map((item) => (
                    <SelectItem key={item} value={item}>
                      {STATUS_LABELS[item]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <Badge variant="secondary" className="justify-center py-1.5">
            {formatNumber(total)} no total
          </Badge>
        </CardContent>
      </Card>

      {history.isError ? (
        <ErrorBlock message={explainError(history.error)} onRetry={() => void history.refetch()} />
      ) : history.isPending ? (
        <div className="space-y-2" aria-label="Carregando histórico" role="status">
          {[0, 1, 2, 3, 4].map((item) => (
            <div key={item} className="h-14 animate-pulse rounded-xl border border-border bg-surface/50" />
          ))}
          <span className="sr-only">Carregando histórico…</span>
        </div>
      ) : items.length === 0 ? (
        <EmptyBlock
          icon={<History className="size-6" aria-hidden="true" />}
          title={search || status !== 'all' ? 'Nenhuma conversão encontrada' : 'Nenhuma conversão ainda'}
          description={
            search || status !== 'all'
              ? 'Ajuste a busca ou o filtro de status para encontrar o que procura.'
              : 'Envie seu primeiro PDF para gerar um Excel com trilha de auditoria completa.'
          }
          action={
            <Button asChild size="sm">
              <Link to="/">
                Converter um PDF
                <ArrowRight aria-hidden="true" />
              </Link>
            </Button>
          }
        />
      ) : (
        <>
          <div className="rounded-[var(--radius-card)] border border-border bg-surface/60">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Arquivo</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="w-40">Progresso</TableHead>
                  <TableHead>Qualidade</TableHead>
                  <TableHead className="text-right">Tabelas</TableHead>
                  <TableHead className="text-right">Células</TableHead>
                  <TableHead>Criada em</TableHead>
                  <TableHead className="w-12 text-right">Ações</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((item) => {
                  const percent = progressForStatus(item.status, item.progress) * 100;
                  return (
                    <TableRow key={item.conversion_id}>
                      <TableCell className="max-w-64">
                        <Link
                          to={`/conversions/${item.conversion_id}`}
                          className="inline-flex max-w-full items-center gap-2 text-foreground hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                          title={item.filename}
                        >
                          <FileSpreadsheet className="size-3.5 shrink-0 text-muted-2" aria-hidden="true" />
                          <span className="truncate">{truncate(item.filename, 42)}</span>
                        </Link>
                        {item.page_count !== null ? (
                          <p className="mt-0.5 text-[11px] text-muted-2">{formatNumber(item.page_count)} páginas</p>
                        ) : null}
                      </TableCell>
                      <TableCell>
                        <StatusBadge status={item.status} showDot />
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <Progress value={percent} className="h-1.5 w-20" />
                          <span className="font-mono text-[11px] text-muted tabular-nums">
                            {Math.round(percent)}%
                          </span>
                        </div>
                      </TableCell>
                      <TableCell>
                        <QualityBadge quality={item.quality} />
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {item.tables === null ? '—' : formatNumber(item.tables)}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {item.cells === null ? '—' : formatNumber(item.cells)}
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-xs text-muted">
                        {formatDateTime(item.created_at)}
                      </TableCell>
                      <TableCell className="text-right">
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button
                              variant="ghost"
                              size="icon"
                              aria-label={`Ações para ${item.filename}`}
                            >
                              <Ellipsis aria-hidden="true" />
                            </Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end">
                            <DropdownMenuLabel>Ações</DropdownMenuLabel>
                            <DropdownMenuItem onSelect={() => navigate(`/conversions/${item.conversion_id}`)}>
                              <FileText aria-hidden="true" />
                              Abrir processamento
                            </DropdownMenuItem>
                            <DropdownMenuItem
                              onSelect={() => navigate(`/conversions/${item.conversion_id}/result`)}
                            >
                              <FileSpreadsheet aria-hidden="true" />
                              Ver resultado
                            </DropdownMenuItem>
                            <DropdownMenuItem
                              onSelect={() => navigate(`/conversions/${item.conversion_id}/audit`)}
                            >
                              <ShieldCheck aria-hidden="true" />
                              Ver auditoria
                            </DropdownMenuItem>
                            <DropdownMenuSeparator />
                            <DropdownMenuItem destructive onSelect={() => setPendingDelete(item)}>
                              <Trash2 aria-hidden="true" />
                              Excluir
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>

          <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs text-muted-2">
              Página {page} de {totalPages}
            </p>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPage(Math.max(1, page - 1))}
                disabled={page <= 1 || history.isFetching}
              >
                <ArrowLeft aria-hidden="true" />
                Anterior
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPage(page + 1)}
                disabled={page >= totalPages || history.isFetching}
              >
                Próxima
                <ArrowRight aria-hidden="true" />
              </Button>
            </div>
          </div>
        </>
      )}

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => (!open ? setPendingDelete(null) : undefined)}
        title="Excluir esta conversão?"
        description={
          <span>
            <strong className="text-foreground">{pendingDelete?.filename}</strong> será removida, junto com o PDF,
            o Excel e os artefatos de auditoria. Esta ação não pode ser desfeita.
          </span>
        }
        confirmLabel="Excluir definitivamente"
        destructive
        onConfirm={handleDelete}
      />
    </div>
  );
}
