import { useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  ArrowRight,
  Check,
  FileImage,
  FileText,
  Loader2,
  ScanSearch,
  ShieldCheck,
  Sparkles,
  Table2,
  Upload,
  X,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Alert } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { StatusBadge } from '@/components/status-badge';
import { useEnsureSession } from '@/hooks/use-session';
import { useCapabilities, useConversions, useUploadConversion } from '@/hooks/use-conversions';
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
  const [file, setFile] = useState<File | null>(null);
  const [fileKind, setFileKind] = useState<'pdf' | 'image' | null>(null);
  const [targets, setTargets] = useState<string[]>(['xlsx', 'txt', 'csv']);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  const settings = useSettings();
  const capabilities = useCapabilities();
  const maxBytes = settings.data?.max_upload_bytes ?? DEFAULT_MAX_UPLOAD_BYTES;
  const upload = useUploadConversion();
  const recent = useConversions({ page: 1, page_size: 5 });

  const supportedOutputs = capabilities.data?.outputs ?? [
    { format: 'xlsx', description: 'Planilha tipada', available: true },
    { format: 'txt', description: 'Texto por ordem de leitura', available: true },
    { format: 'csv', description: 'Tabelas em CSV', available: true },
  ];

  function acceptFile(candidate: File | null | undefined) {
    if (!candidate) return;
    const result = validateDocumentFile(candidate, { maxBytes });
    if (!result.ok) {
      setFile(null);
      setFileKind(null);
      setError(result.message);
      return;
    }
    setError(null);
    setFile(candidate);
    setFileKind(result.kind ?? 'pdf');
    // Images produce TXT only; other targets are refused by the API.
    setTargets(result.kind === 'image' ? ['txt'] : ['xlsx', 'txt', 'csv']);
  }

  function toggleTarget(format: string) {
    setTargets((current) => {
      if (fileKind === 'image') return ['txt'];
      if (current.includes(format)) {
        const next = current.filter((item) => item !== format);
        return next.length > 0 ? next : current;
      }
      return [...current, format];
    });
  }

  function openPicker() {
    inputRef.current?.click();
  }

  function handleUpload() {
    if (!file || upload.isPending) return;
    setError(null);
    upload.mutate(
      { file, options: { target_formats: targets } },
      {
        onSuccess: (response) => {
          toast({
            title: 'Documento enviado',
            description: 'Acompanhe o processamento em tempo real.',
            variant: 'success',
          });
          navigate(`/conversions/${response.conversion_id}`);
        },
        onError: (cause) => {
          setError(explainError(cause));
          toast({ title: 'Falha no upload', description: explainError(cause), variant: 'error' });
        },
      },
    );
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
          role="button"
          tabIndex={0}
          aria-label="Arraste seu PDF ou imagem para cá ou selecione um arquivo"
          aria-describedby="dropzone-help"
          onClick={openPicker}
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              openPicker();
            }
          }}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            acceptFile(event.dataTransfer.files?.[0]);
          }}
          className={cn(
            'group relative flex cursor-pointer flex-col items-center justify-center gap-4 rounded-[var(--radius-card)] border-2 border-dashed px-6 py-12 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background sm:py-16',
            dragging
              ? 'border-accent bg-accent-soft/30'
              : 'border-border-strong bg-surface/60 hover:border-accent/60 hover:bg-surface-2/60',
          )}
        >
          <span className="grid size-14 place-items-center rounded-2xl bg-accent/10 text-accent transition-transform group-hover:-translate-y-0.5">
            <Upload className="size-6" aria-hidden="true" />
          </span>
          <div className="space-y-1">
            <p className="text-base font-medium text-foreground">Arraste seu PDF ou imagem para cá</p>
            <p id="dropzone-help" className="text-sm text-muted">
              Ou selecione um arquivo do seu computador (PDF, PNG, JPEG, TIFF, GIF, BMP, WebP)
            </p>
          </div>
          <Button
            variant="secondary"
            onClick={(event) => {
              event.stopPropagation();
              openPicker();
            }}
          >
            <FileText aria-hidden="true" />
            Selecionar arquivo
          </Button>
          <p className="text-[11px] text-muted-2">
            Até {formatBytes(maxBytes)}
            {settings.data?.max_pages ? ` · ${formatNumber(settings.data.max_pages)} páginas` : ''}
          </p>
          <input
            ref={inputRef}
            type="file"
            accept={ACCEPTED_DOCUMENT_ACCEPT}
            aria-label="Selecionar PDF ou imagem"
            className="sr-only"
            onClick={(event) => event.stopPropagation()}
            onChange={(event) => {
              acceptFile(event.target.files?.[0]);
              event.target.value = '';
            }}
          />
        </div>

        {error ? (
          <Alert variant="error" title="Não foi possível usar este arquivo">
            {error}
          </Alert>
        ) : null}

        {file ? (
          <fieldset className="space-y-3 rounded-[var(--radius-card)] border border-border bg-surface/50 p-4 text-left">
            <legend className="px-1 text-xs font-semibold uppercase tracking-widest text-muted-2">
              Formatos de saída
            </legend>
            <p className="text-xs text-muted-2">
              {fileKind === 'image'
                ? 'Imagens são reconhecidas por OCR e entregues como TXT.'
                : 'O mesmo PDF alimenta todos os formatos sem repetir o OCR.'}
            </p>
            <div className="flex flex-wrap gap-2">
              {supportedOutputs.map((output) => {
                const disabled =
                  !output.available || (fileKind === 'image' && output.format !== 'txt');
                const checked = targets.includes(output.format);
                return (
                  <label
                    key={output.format}
                    className={cn(
                      'inline-flex cursor-pointer items-center gap-2 rounded-xl border px-3 py-2 text-xs transition-colors',
                      checked
                        ? 'border-accent/60 bg-accent-soft text-foreground'
                        : 'border-border bg-surface-2 text-muted',
                      disabled && 'cursor-not-allowed opacity-50',
                    )}
                  >
                    <input
                      type="checkbox"
                      className="sr-only"
                      checked={checked}
                      disabled={disabled}
                      onChange={() => toggleTarget(output.format)}
                    />
                    <span
                      className={cn(
                        'grid size-4 place-items-center rounded border',
                        checked ? 'border-accent bg-accent text-accent-foreground' : 'border-border-strong',
                      )}
                      aria-hidden="true"
                    >
                      {checked ? <Check className="size-3" /> : null}
                    </span>
                    <span className="font-semibold uppercase">{output.format}</span>
                    <span className="text-muted-2">{output.description}</span>
                  </label>
                );
              })}
            </div>
          </fieldset>
        ) : null}

        {file ? (
          <Card className="text-left animate-fade-in">
            <CardContent className="flex items-center gap-3 p-4 pt-4 sm:p-4">
              <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-accent/10 text-accent">
                {fileKind === 'image' ? (
                  <FileImage className="size-5" aria-hidden="true" />
                ) : (
                  <FileText className="size-5" aria-hidden="true" />
                )}
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-foreground">{file.name}</p>
                <p className="text-xs text-muted-2">
                  {formatBytes(file.size)} · {fileKind === 'image' ? 'imagem' : 'PDF'} ·{' '}
                  {targets.map((item) => item.toUpperCase()).join(', ')}
                </p>
              </div>
              <button
                type="button"
                onClick={() => {
                  setFile(null);
                  setFileKind(null);
                }}
                aria-label="Remover arquivo"
                className="rounded-lg p-2 text-muted-2 transition-colors hover:bg-surface-3 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                <X className="size-4" aria-hidden="true" />
              </button>
              <Button onClick={handleUpload} disabled={upload.isPending}>
                {upload.isPending ? (
                  <>
                    <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
                    Enviando…
                  </>
                ) : (
                  <>
                    Converter
                    <ArrowRight aria-hidden="true" />
                  </>
                )}
              </Button>
            </CardContent>
          </Card>
        ) : null}
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
