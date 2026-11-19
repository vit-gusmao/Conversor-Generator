import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { Check, Database, HardDrive, KeyRound, ScanText, Server, ShieldCheck, SlidersHorizontal, X } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { PageHeader } from '@/components/layout/page-header';
import { ErrorBlock, LoadingBlock } from '@/components/layout/states';
import { useSettings } from '@/hooks/use-settings';
import { useEnsureSession } from '@/hooks/use-session';
import { explainError } from '@/lib/errors';
import { formatBytes, formatNumber } from '@/lib/format';

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 py-1.5 text-sm">
      <span className="text-muted">{label}</span>
      <span className="font-medium text-foreground">{value}</span>
    </div>
  );
}

export function SettingsPage() {
  const localModelsLink = (
    <p className="text-sm text-muted">
      Modelos locais de OCR e do assistente:{' '}
      <Link to="/settings/local-models" className="text-accent hover:underline">
        abrir configuração de IA local
      </Link>
      .
    </p>
  );
  useEnsureSession();
  const settings = useSettings();

  if (settings.isPending) {
    return (
      <div className="space-y-4">
        <LoadingBlock label="Carregando configurações" />
        <LoadingBlock label="Carregando limites" />
      </div>
    );
  }

  if (settings.isError || !settings.data) {
    return (
      <div className="space-y-4">
        {localModelsLink}
      <PageHeader eyebrow="Configurações" title="Plataforma" />
        <ErrorBlock
          title="Não foi possível ler as configurações"
          message={explainError(settings.error)}
          onRetry={() => void settings.refetch()}
        />
      </div>
    );
  }

  const data = settings.data;
  const capabilities = Object.entries(data.capabilities ?? {});

  return (
    <div>
      <PageHeader
        eyebrow="Configurações"
        title="Plataforma"
        description="Parâmetros efetivos publicados pela API. Esta tela é somente leitura: a configuração é definida pelo operador no arquivo de ambiente."
        actions={<Badge variant="outline">Somente leitura</Badge>}
      />

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2">
              <Server className="size-4 text-accent" aria-hidden="true" />
              Execução
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            <Row
              label="Modo de runtime"
              value={
                <Badge variant={data.runtime_mode === 'full' ? 'default' : 'info'}>
                  {data.runtime_mode === 'full' ? 'Full (NestJS + serviços)' : 'Standalone (FastAPI)'}
                </Badge>
              }
            />
            <Row label="Versão do motor" value={<span className="font-mono text-xs">{data.engine_version}</span>} />
            <Row
              label="Armazenamento"
              value={
                <span className="inline-flex items-center gap-1.5">
                  <HardDrive className="size-3.5 text-muted-2" aria-hidden="true" />
                  {data.storage_backend === 's3' ? 'S3 / MinIO' : 'Local'}
                </span>
              }
            />
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2">
              <KeyRound className="size-4 text-accent" aria-hidden="true" />
              Autenticação
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            <Row
              label="Modo"
              value={
                <Badge variant={data.auth_mode === 'required' ? 'warning' : 'secondary'}>
                  {data.auth_mode === 'required' ? 'Obrigatória (JWT)' : 'Opcional (sessão anônima)'}
                </Badge>
              }
            />
            <Row label="Token" value={<span className="font-mono text-xs">Bearer JWT</span>} />
            <Row label="Isolamento" value="Cada sessão vê apenas suas conversões" />
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2">
              <SlidersHorizontal className="size-4 text-accent" aria-hidden="true" />
              Limites de upload
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            <Row label="Tamanho máximo" value={formatBytes(data.max_upload_bytes)} />
            <Row label="Páginas máximas" value={formatNumber(data.max_pages)} />
            <Row label="Formatos aceitos" value="PDF (application/pdf)" />
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2">
              <ScanText className="size-4 text-accent" aria-hidden="true" />
              OCR
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            <Row
              label="Habilitado"
              value={
                data.ocr_enabled ? (
                  <Badge variant="success">Ativo</Badge>
                ) : (
                  <Badge variant="muted">Desativado</Badge>
                )
              }
            />
            <Row label="Motor" value={data.ocr_engine ?? 'Nenhum motor configurado'} />
            <Row label="Fallback" value="OCR é acionado somente quando a camada nativa é insuficiente" />
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2">
              <Database className="size-4 text-accent" aria-hidden="true" />
              Retenção de artefatos
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            <Row label="PDF original" value={`${formatNumber(data.retention.pdf_days)} dias`} />
            <Row label="Excel gerado" value={`${formatNumber(data.retention.xlsx_days)} dias`} />
            <Row label="Trilha de auditoria" value={`${formatNumber(data.retention.audit_days)} dias`} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2">
              <ShieldCheck className="size-4 text-accent" aria-hidden="true" />
              Capacidades
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            {capabilities.length === 0 ? (
              <p className="py-2 text-sm text-muted">A API não publicou capacidades.</p>
            ) : (
              <ul className="space-y-1.5">
                {capabilities.map(([name, enabled]) => (
                  <li key={name} className="flex items-center justify-between gap-3 text-sm">
                    <span className="text-muted">{name}</span>
                    <span className="inline-flex items-center gap-1.5 text-xs">
                      {enabled ? (
                        <>
                          <Check className="size-4 text-success" aria-hidden="true" />
                          <span className="text-[#8ef0cd]">Disponível</span>
                        </>
                      ) : (
                        <>
                          <X className="size-4 text-danger" aria-hidden="true" />
                          <span className="text-[#ffb4b4]">Indisponível</span>
                        </>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>

      <Separator className="my-6" />
      <p className="text-xs text-muted-2">
        Para alterar qualquer parâmetro, edite o arquivo de ambiente do serviço e reinicie a API. O frontend
        apenas reflete o contrato publicado em <code className="font-mono">GET /api/v1/settings</code>.
      </p>
    </div>
  );
}
