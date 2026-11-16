import { useMemo } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import {
  AlertTriangle,
  ArrowRight,
  Code2,
  Download,
  FileSearch,
  FileSpreadsheet,
  Gauge,
  GitBranch,
  LayoutDashboard,
  ListTree,
  ShieldCheck,
  Table2,
  TextCursorInput,
  Database,
} from 'lucide-react';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Button } from '@/components/ui/button';
import { Alert } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { PageHeader } from '@/components/layout/page-header';
import { ErrorBlock, LoadingBlock } from '@/components/layout/states';
import { QualityBadge, StatusBadge } from '@/components/status-badge';
import { OverviewTab } from '@/components/audit/overview-tab';
import { TimelineTab } from '@/components/audit/timeline-tab';
import { PagesTab } from '@/components/audit/pages-tab';
import { FieldsTab } from '@/components/audit/fields-tab';
import { TablesTab } from '@/components/audit/tables-tab';
import { ExtractedDataTab } from '@/components/audit/extracted-data-tab';
import { ExcelTab } from '@/components/audit/excel-tab';
import { DecisionsTab } from '@/components/audit/decisions-tab';
import { IssuesTab } from '@/components/audit/issues-tab';
import { PerformanceTab } from '@/components/audit/performance-tab';
import { TechnicalTab } from '@/components/audit/technical-tab';
import { ExportTab } from '@/components/audit/export-tab';
import { useAuditIssues, useAuditSummary } from '@/hooks/use-audit';
import { useEnsureSession } from '@/hooks/use-session';
import { explainError } from '@/lib/errors';
import { formatNumber } from '@/lib/format';

const TABS = [
  { value: 'overview', label: 'Visão Geral', icon: LayoutDashboard },
  { value: 'timeline', label: 'Linha do Tempo', icon: ListTree },
  { value: 'pages', label: 'Páginas', icon: FileSearch },
  { value: 'fields', label: 'Campos', icon: TextCursorInput },
  { value: 'tables', label: 'Tabelas', icon: Table2 },
  { value: 'data', label: 'Dados Extraídos', icon: Database },
  { value: 'excel', label: 'Excel Gerado', icon: FileSpreadsheet },
  { value: 'decisions', label: 'Decisões', icon: GitBranch },
  { value: 'issues', label: 'Inconsistências', icon: AlertTriangle },
  { value: 'performance', label: 'Desempenho', icon: Gauge },
  { value: 'technical', label: 'Auditoria Técnica', icon: Code2 },
  { value: 'export', label: 'Exportar Auditoria', icon: Download },
] as const;

type TabValue = (typeof TABS)[number]['value'];
const TAB_VALUES = new Set<string>(TABS.map((tab) => tab.value));

export function AuditPage() {
  const { id = '' } = useParams<{ id: string }>();
  useEnsureSession();
  const [params, setParams] = useSearchParams();

  const summary = useAuditSummary(id);
  const issues = useAuditIssues(id);

  const rawTab = params.get('tab');
  const tab: TabValue = rawTab && TAB_VALUES.has(rawTab) ? (rawTab as TabValue) : 'overview';
  const page = Math.max(1, Number.parseInt(params.get('page') ?? '1', 10) || 1);
  const sheet = params.get('sheet');
  const cell = params.get('cell');

  const issueItems = useMemo(() => issues.data?.items ?? [], [issues.data]);
  const topIssues = issueItems.slice(0, 2);

  function updateParams(updates: Record<string, string | null>, mode: 'set' | 'delete' = 'set') {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(updates)) {
      if (mode === 'delete' || value === null) next.delete(key);
      else next.set(key, value);
    }
    setParams(next, { replace: true });
  }

  function setTab(value: string) {
    updateParams({ tab: value });
  }

  function openPage(target: number) {
    updateParams({ tab: 'pages', page: String(target) });
  }

  function selectCell(nextSheet: string | null, nextCell: string | null) {
    updateParams({ tab: 'excel', sheet: nextSheet, cell: nextCell });
  }

  if (!id) {
    return <ErrorBlock title="Auditoria inválida" message="O identificador da conversão não foi informado." />;
  }

  if (summary.isPending) {
    return (
      <div className="space-y-4">
        <PageHeader eyebrow="Auditoria" title="Carregando auditoria…" />
        <LoadingBlock label="Carregando resumo da auditoria" />
        <LoadingBlock label="Carregando indicadores" />
      </div>
    );
  }

  if (summary.isError || !summary.data) {
    return (
      <div className="space-y-4">
        <PageHeader
          eyebrow="Auditoria"
          title="Auditoria indisponível"
          backTo={{ to: `/conversions/${id}`, label: 'Processamento' }}
        />
        <ErrorBlock
          title="Não foi possível carregar a auditoria"
          message={explainError(summary.error)}
          onRetry={() => void summary.refetch()}
        />
        <Button asChild variant="secondary">
          <Link to={`/conversions/${id}`}>Voltar ao processamento</Link>
        </Button>
      </div>
    );
  }

  const data = summary.data;
  const totalPages = Math.max(1, data.metrics.pages_total);

  return (
    <div>
      <PageHeader
        backTo={{ to: `/conversions/${id}`, label: 'Processamento' }}
        eyebrow="Auditoria"
        title="Auditoria da conversão"
        description={
          <span className="flex flex-wrap items-center gap-2 text-xs">
            <span className="font-mono text-muted-2">{data.conversion_id}</span>
            <StatusBadge status={data.status} />
            <QualityBadge quality={data.quality} />
            <Badge variant={data.hash_chain_valid ? 'success' : 'danger'}>
              <ShieldCheck className="size-3" aria-hidden="true" />
              {data.hash_chain_valid ? 'Cadeia de hashes válida' : 'Cadeia de hashes inválida'}
            </Badge>
            <span className="text-muted-2">
              {formatNumber(data.metrics.events_total)} eventos · {formatNumber(data.metrics.issues_total)}{' '}
              inconsistências
            </span>
          </span>
        }
        actions={
          <>
            <Button asChild variant="secondary" size="sm">
              <Link to={`/conversions/${id}/result`}>
                <FileSpreadsheet aria-hidden="true" />
                Ver resultado
                <ArrowRight aria-hidden="true" />
              </Link>
            </Button>
            <Button size="sm" onClick={() => setTab('export')}>
              <Download aria-hidden="true" />
              Exportar
            </Button>
          </>
        }
      />

      {issueItems.length > 0 ? (
        <Alert variant="warning" title={`${formatNumber(issueItems.length)} inconsistência(s) registrada(s)`} className="mb-4">
          <div className="flex flex-wrap items-center gap-2">
            <span className="min-w-0 flex-1">
              {topIssues.map((issue) => issue.message).join(' · ')}
            </span>
            <Button variant="secondary" size="sm" onClick={() => setTab('issues')}>
              Investigar
            </Button>
          </div>
        </Alert>
      ) : null}

      <Tabs value={tab} onValueChange={setTab}>
        <div className="-mx-4 overflow-x-auto px-4 pb-1 sm:mx-0 sm:px-0">
          <TabsList aria-label="Seções da auditoria" className="min-w-max flex-nowrap">
            {TABS.map((item) => {
              const Icon = item.icon;
              return (
                <TabsTrigger key={item.value} value={item.value}>
                  <Icon className="size-3.5" aria-hidden="true" />
                  {item.label}
                </TabsTrigger>
              );
            })}
          </TabsList>
        </div>

        <TabsContent value="overview">
          <OverviewTab summary={data} />
        </TabsContent>

        <TabsContent value="timeline">
          <TimelineTab conversionId={id} />
        </TabsContent>

        <TabsContent value="pages">
          <PagesTab conversionId={id} page={Math.min(page, totalPages)} totalPages={totalPages} onPageChange={openPage} />
        </TabsContent>

        <TabsContent value="fields">
          <FieldsTab conversionId={id} summary={data} />
        </TabsContent>

        <TabsContent value="tables">
          <TablesTab conversionId={id} summary={data} onOpenPage={openPage} />
        </TabsContent>

        <TabsContent value="data">
          <ExtractedDataTab conversionId={id} summary={data} />
        </TabsContent>

        <TabsContent value="excel">
          <ExcelTab conversionId={id} sheet={sheet} cell={cell} onSelectCell={selectCell} />
        </TabsContent>

        <TabsContent value="decisions">
          <DecisionsTab summary={data} />
        </TabsContent>

        <TabsContent value="issues">
          <IssuesTab conversionId={id} onOpenPage={openPage} />
        </TabsContent>

        <TabsContent value="performance">
          <PerformanceTab summary={data} />
        </TabsContent>

        <TabsContent value="technical">
          <TechnicalTab conversionId={id} />
        </TabsContent>

        <TabsContent value="export">
          <ExportTab conversionId={id} summary={data} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
