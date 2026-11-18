import { useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  ArrowRight,
  FileText,
  Loader2,
  ScanSearch,
  ShieldCheck,
  Sparkles,
  Table2,
  Upload,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Alert } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { StatusBadge } from '@/components/status-badge';
import { UploadItemList } from '@/components/upload-item-list';
import { useEnsureSession } from '@/hooks/use-session';
import { useConversions } from '@/hooks/use-conversions';
import { useSettings } from '@/hooks/use-settings';
import {
  ACCEPTED_DOCUMENT_ACCEPT,
  DEFAULT_MAX_UPLOAD_BYTES,
  validateDocumentFile,
} from '@/lib/validate-file';
import { explainError } from '@/lib/errors';
import { formatBytes, formatNumber, formatRelative } from '@/lib/format';
import { cn } from '@/lib/utils';
import { toast } from '@/stores/ui';
import { useUploadQueue } from '@/stores/upload-queue';
import { defaultConversionOptions } from '@pdf2x/contracts';
import { newClientKey, startBatch, type TransferItem } from '@/services/transfer';

const FEATURES = [
  {
    icon: ScanSearch,
    title: 'Somente texto e dados',
    description:
      'O PDF é lido como camada textual, OCR e geometria. Nenhuma imagem é necessária para montar a planilha.',
  },
  {
    icon: Table2,
    title: 'Sem imagens no Excel',
    description:
      'O arquivo gerado contém apenas células e valores. Nada de capturas de página ou imagens embutidas.',
  },
  {
    icon: ShieldCheck,
    title: 'Auditoria completa',
    description:
      'Cada valor tem origem, decisão, motor e célula de destino registrados em uma trilha encadeada por hashes.',
  },
] as const;

export function HomePage() {
  useEnsureSession();
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);

  const items = useUploadQueue((state) => state.items);
  const enqueueFiles = useUploadQueue((state) => state.enqueueFiles);
  const removeItem = useUploadQueue((state) => state.removeItem);
  const setItemState = useUploadQueue((state) => state.setItemState);
  const clearFinished = useUploadQueue((state) => state.clearFinished);

  const [invalidFiles, setInvalidFiles] = useState<string[]>([]);
  const [sendError, setSendError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [dragging, setDragging] = useState(false);

  const settings = useSettings();
  const recent = useConversions({ page: 1, page_size: 5 });

  const uploadCapabilities = settings.data?.upload;
  // The resumable path has no document ceiling when the server reports `null`.
  // The legacy single-shot limit is used only when capabilities are absent, so
  // an older server still gets a sane client-side guard.
  const validationMaxBytes = uploadCapabilities
    ? uploadCapabilities.max_file_bytes
    : (settings.data?.max_upload_bytes ?? DEFAULT_MAX_UPLOAD_BYTES);

  const total = items.length;
  const queuedCount = items.filter((item) => item.state === 'queued').length;
  const uploadingCount = items.filter((item) => item.state === 'uploading').length;
  const uploadedCount = items.filter((item) => item.state === 'uploaded').length;
  const failedCount = items.filter((item) => item.state === 'failed').length;
  const needsReselectionCount = items.filter((item) => item.state === 'needs_reselection').length;

  const queueSummary =
    `${total} ${total === 1 ? 'arquivo na fila' : 'arquivos na fila'}` +
    (uploadingCount > 0 ? ` · ${uploadingCount} em envio` : '') +
    (failedCount > 0 ? ` · ${failedCount} com erro` : '');

  function openPicker() {
    inputRef.current?.click();
  }

  /** Drops and picks accumulate: an invalid file never clears the queue. */
  function acceptFiles(candidates: File[]) {
    if (candidates.length === 0) return;

    const accepted: File[] = [];
    const problems: string[] = [];

    for (const candidate of candidates) {
      const result = validateDocumentFile(candidate, { maxBytes: validationMaxBytes });
      if (result.ok) {
        accepted.push(candidate);
      } else {
        problems.push(`${candidate.name}: ${result.message ?? 'arquivo não suportado.'}`);
      }
    }

    if (accepted.length > 0) {
      enqueueFiles(accepted, defaultConversionOptions('pdf'));
    }
    setInvalidFiles(problems);
    setSendError(null);
  }

  async function handleSend() {
    if (sending || total === 0) return;

    const sendable = items.filter(
      (item) => item.state === 'queued' || item.state === 'uploading' || item.state === 'failed',
    );
    const missingFile = items.filter((item) => item.state === 'needs_reselection');

    if (sendable.length === 0) {
      setSendError('Nenhum arquivo pronto para envio. Adicione documentos à fila.');
      return;
    }
    if (missingFile.length > 0) {
      setSendError(
        `${missingFile.length} arquivo${missingFile.length === 1 ? '' : 's'} precisa${
          missingFile.length === 1 ? '' : 'm'
        } ser selecionado${missingFile.length === 1 ? '' : 's'} novamente e ficou fora deste envio.`,
      );
    } else {
      setSendError(null);
    }

    const transferItems: TransferItem[] = sendable.map((item) => ({
      clientItemId: item.clientItemId,
      file: item.file,
      filename: item.filename,
      size: item.size,
      options: item.options,
    }));

    // One key per logical batch attempt, reused if this submit is retried.
    const clientBatchKey = newClientKey();

    setSending(true);
    try {
      const result = await startBatch({
        items: transferItems,
        clientBatchKey,
        onItemPatch: (clientItemId, patch) => setItemState(clientItemId, patch),
      });

      clearFinished();

      if (result.failures.length > 0) {
        toast({
          title: 'Lote criado com falhas',
          description: `${result.failures.length} arquivo(s) não puderam ser enviados.`,
          variant: 'warning',
        });
      } else {
        toast({
          title: 'Lote enviado',
          description: `${sendable.length} arquivo(s) em processamento.`,
          variant: 'success',
        });
      }

      navigate(`/batches/${result.batchId}`);
    } catch (cause) {
      const message = explainError(cause);
      setSendError(message);
      toast({ title: 'Falha ao enviar', description: message, variant: 'error' });
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="space-y-12 sm:space-y-16">
      <section className="mx-auto max-w-3xl space-y-8 text-center">
        <div className="space-y-4">
          <Badge variant="outline" className="mx-auto">
            <Sparkles className="size-3.5 text-accent" aria-hidden="true" />
            PDF e imagens com rastreabilidade total
          </Badge>
          <h1 className="text-4xl font-semibold tracking-tight text-foreground sm:text-5xl">
            Converta documentos em dados
          </h1>
          <p className="mx-auto max-w-xl text-base text-muted sm:text-lg">
            PDF e imagens → Excel, TXT e CSV
          </p>
          <p className="mx-auto max-w-xl text-sm text-muted-2">
            Texto por ordem de leitura · Tabelas em CSV · Auditoria completa de cada decisão
          </p>
        </div>

        <div
          data-testid="home-dropzone"
          aria-label="Área para arrastar arquivos"
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            acceptFiles(Array.from(event.dataTransfer.files ?? []));
          }}
          className={cn(
            'group relative flex flex-col items-center justify-center gap-4 rounded-[var(--radius-card)] border-2 border-dashed px-6 py-12 transition-colors sm:py-16',
            dragging
              ? 'border-accent bg-accent-soft/30'
              : 'border-border-strong bg-surface/60 hover:border-accent/60 hover:bg-surface-2/60',
          )}
        >
          <span className="grid size-14 place-items-center rounded-2xl bg-accent/10 text-accent transition-transform group-hover:-translate-y-0.5">
            <Upload className="size-6" aria-hidden="true" />
          </span>
          <div className="space-y-1">
            <p className="text-base font-medium text-foreground">Arraste seus arquivos para cá</p>
            <p id="dropzone-help" className="text-sm text-muted">
              Ou selecione os documentos do seu computador. Vários arquivos podem ser adicionados de
              uma vez; novos envios se somam à fila.
            </p>
          </div>
          <Button variant="secondary" onClick={openPicker} aria-describedby="dropzone-help">
            <FileText aria-hidden="true" />
            Selecionar arquivos
          </Button>
          <p className="text-[11px] text-muted-2">
            {uploadCapabilities
              ? uploadCapabilities.max_file_bytes === null
                ? 'Sem limite de tamanho no envio por partes'
                : `Até ${formatBytes(uploadCapabilities.max_file_bytes)} por arquivo`
              : `Até ${formatBytes(
                  settings.data?.max_upload_bytes ?? DEFAULT_MAX_UPLOAD_BYTES,
                )} por arquivo`}
            {uploadCapabilities ? ` · partes de ${formatBytes(uploadCapabilities.part_size_bytes)}` : ''}
            {settings.data?.max_pages ? ` · ${formatNumber(settings.data.max_pages)} páginas` : ''}
          </p>
          <input
            ref={inputRef}
            type="file"
            multiple
            accept={ACCEPTED_DOCUMENT_ACCEPT}
            aria-label="Selecionar PDF ou imagem"
            className="sr-only"
            onChange={(event) => {
              acceptFiles(Array.from(event.target.files ?? []));
              event.target.value = '';
            }}
          />
        </div>

        {invalidFiles.length > 0 ? (
          <Alert variant="error" title="Não foi possível adicionar alguns arquivos">
            <ul className="list-disc space-y-1 pl-4 text-left">
              {invalidFiles.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          </Alert>
        ) : null}

        {sendError ? (
          <Alert variant="error" title="Falha no envio">
            {sendError}
          </Alert>
        ) : null}

        <Card className="text-left animate-fade-in">
          <CardContent className="space-y-4 p-5 pt-5 sm:p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 id="queue-title" className="text-sm font-semibold text-foreground">
                    Fila de envio
                  </h2>
                  <Badge variant="secondary">{total}</Badge>
                </div>
                <p id="queue-summary" className="text-xs text-muted-2" aria-live="polite">
                  {queueSummary}
                </p>
              </div>
              <Button
                onClick={() => void handleSend()}
                disabled={sending || total === 0}
                aria-describedby="queue-summary"
              >
                {sending ? (
                  <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
                ) : (
                  <Upload aria-hidden="true" />
                )}
                {sending ? 'Enviando…' : 'Enviar arquivos'}
              </Button>
            </div>

            {total > 0 ? (
              <div className="flex flex-wrap gap-2">
                {queuedCount > 0 ? <Badge variant="secondary">{queuedCount} na fila</Badge> : null}
                {uploadingCount > 0 ? <Badge variant="info">{uploadingCount} enviando</Badge> : null}
                {uploadedCount > 0 ? <Badge variant="success">{uploadedCount} enviados</Badge> : null}
                {failedCount > 0 ? <Badge variant="danger">{failedCount} com erro</Badge> : null}
                {needsReselectionCount > 0 ? (
                  <Badge variant="warning">{needsReselectionCount} para reselecionar</Badge>
                ) : null}
              </div>
            ) : null}

            <UploadItemList
              items={items}
              onRemove={removeItem}
              onRetry={(clientItemId) =>
                setItemState(clientItemId, { state: 'queued', error: undefined })
              }
              onCancel={(clientItemId) => setItemState(clientItemId, { state: 'cancelled' })}
              emptyMessage="A fila está vazia. Adicione PDFs ou imagens para começar."
            />

            <p className="text-[11px] text-muted-2">
              Os arquivos são enviados em partes, no máximo 3 transferências ao mesmo tempo. Fechar o
              navegador não sela o lote.
            </p>
          </CardContent>
        </Card>
      </section>

      <section aria-labelledby="features-title" className="space-y-5">
        <h2 id="features-title" className="sr-only">
          Por que usar o Conversor Generator
        </h2>
        <div className="grid gap-4 sm:grid-cols-3">
          {FEATURES.map((feature) => {
            const Icon = feature.icon;
            return (
              <Card key={feature.title}>
                <CardContent className="space-y-3 p-5 pt-5 sm:p-5">
                  <span className="grid size-9 place-items-center rounded-lg bg-surface-2 text-accent">
                    <Icon className="size-4" aria-hidden="true" />
                  </span>
                  <h3 className="text-sm font-semibold text-foreground">{feature.title}</h3>
                  <p className="text-xs leading-relaxed text-muted">{feature.description}</p>
                </CardContent>
              </Card>
            );
          })}
        </div>
      </section>

      <section aria-labelledby="recent-title" className="space-y-4">
        <div className="flex items-end justify-between gap-3">
          <div>
            <h2 id="recent-title" className="text-lg font-semibold text-foreground">
              Últimas conversões
            </h2>
            <p className="text-xs text-muted-2">Retome o acompanhamento ou abra a auditoria.</p>
          </div>
          <Link
            to="/history"
            className="inline-flex items-center gap-1 text-xs font-medium text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            Ver histórico
            <ArrowRight className="size-3.5" aria-hidden="true" />
          </Link>
        </div>

        {recent.isPending ? (
          <div className="space-y-2" aria-hidden="true">
            {[0, 1, 2].map((item) => (
              <div key={item} className="h-14 animate-pulse rounded-xl border border-border bg-surface/50" />
            ))}
          </div>
        ) : recent.isError ? (
          <p className="rounded-xl border border-border bg-surface/50 px-4 py-3 text-sm text-muted">
            Não foi possível carregar as conversões recentes.
          </p>
        ) : !recent.data || recent.data.items.length === 0 ? (
          <p className="rounded-xl border border-dashed border-border bg-surface/40 px-4 py-6 text-center text-sm text-muted">
            Nenhuma conversão ainda. Envie seu primeiro PDF acima.
          </p>
        ) : (
          <ul className="space-y-2">
            {recent.data.items.map((item) => (
              <li key={item.conversion_id}>
                <Link
                  to={`/conversions/${item.conversion_id}`}
                  className="flex flex-wrap items-center gap-3 rounded-xl border border-border bg-surface/60 px-4 py-3 transition-colors hover:border-border-strong hover:bg-surface-2/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                >
                  <FileText className="size-4 shrink-0 text-muted-2" aria-hidden="true" />
                  <span className="min-w-0 flex-1 truncate text-sm text-foreground">{item.filename}</span>
                  <StatusBadge status={item.status} showDot />
                  <span className="text-xs text-muted-2">{formatNumber(item.tables ?? 0)} tabelas</span>
                  <span className="text-xs text-muted-2">{formatRelative(item.created_at)}</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
