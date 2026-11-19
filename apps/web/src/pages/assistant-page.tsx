import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import {
  Bot,
  Database,
  FileText,
  Loader2,
  MessageSquarePlus,
  Send,
  Square,
  Trash2,
  TriangleAlert,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Alert } from '@/components/ui/alert';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { PageHeader } from '@/components/layout/page-header';
import { useConversions } from '@/hooks/use-conversions';
import { useEnsureSession } from '@/hooks/use-session';
import { explainError } from '@/lib/errors';
import { formatRelative } from '@/lib/format';
import { cn } from '@/lib/utils';
import {
  createConversation,
  deleteConversation,
  getConversation,
  getModelStatus,
  listConversations,
  streamChatMessage,
  type ChatEvent,
  type ChatMessage,
  type Citation,
  type Conversation,
  type ModelStatus,
} from '@/services/assistant';
import {
  createIndexJob,
  getIndexJob,
  listCollections,
  type KnowledgeCollection,
} from '@/services/knowledge';
import { toast } from '@/stores/ui';

/** Minimal, sanitised Markdown: bold, inline code and http(s) links only. */
function renderMarkdown(text: string) {
  return text.split('\n').map((line, lineIndex) => {
    const nodes: ReactNode[] = [];
    const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)\s]+\))/g;
    let last = 0;
    let match: RegExpExecArray | null;
    while ((match = pattern.exec(line)) !== null) {
      if (match.index > last) nodes.push(line.slice(last, match.index));
      const token = match[0];
      if (token.startsWith('**')) {
        nodes.push(<strong key={`${lineIndex}-${match.index}`}>{token.slice(2, -2)}</strong>);
      } else if (token.startsWith('`')) {
        nodes.push(
          <code key={`${lineIndex}-${match.index}`} className="rounded bg-surface-3 px-1 py-0.5 text-xs">
            {token.slice(1, -1)}
          </code>,
        );
      } else {
        const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(token);
        const url = link?.[2] ?? '';
        if (link && /^https?:\/\//i.test(url)) {
          nodes.push(
            <a
              key={`${lineIndex}-${match.index}`}
              href={url}
              target="_blank"
              rel="noreferrer noopener"
              className="text-accent underline underline-offset-2"
            >
              {link[1]}
            </a>,
          );
        } else {
          nodes.push(token);
        }
      }
      last = match.index + token.length;
    }
    if (last < line.length) nodes.push(line.slice(last));
    return (
      <p key={lineIndex} className="min-h-[1.1em] whitespace-pre-wrap">
        {nodes}
      </p>
    );
  });
}

export function AssistantPage() {
  useEnsureSession();
  const [status, setStatus] = useState<ModelStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [collections, setCollections] = useState<KnowledgeCollection[]>([]);
  const [collectionId, setCollectionId] = useState<string>('');
  const [active, setActive] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [citations, setCitations] = useState<Citation[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [phase, setPhase] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [indexing, setIndexing] = useState(false);
  const [selectedConversion, setSelectedConversion] = useState<string>('');
  const abortRef = useRef<AbortController | null>(null);
  /** Incremented per stream; a stream whose id is no longer current is ignored. */
  const streamIdRef = useRef(0);
  /** Conversation the live stream writes into. */
  const streamConversationRef = useRef<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const conversions = useConversions({ page: 1, page_size: 20 });

  const refreshConversations = useCallback(async () => {
    try {
      const payload = await listConversations();
      setConversations(payload.items);
    } catch {
      // the list is secondary; the chat still works
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [model, cols] = await Promise.all([getModelStatus(), listCollections()]);
        if (cancelled) return;
        setStatus(model);
        setCollections(cols.items);
        setCollectionId((current) => current || cols.items[0]?.collection_id || '');
      } catch (cause) {
        if (!cancelled) setStatusError(explainError(cause));
      }
      await refreshConversations();
    })();
    return () => {
      cancelled = true;
    };
  }, [refreshConversations]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' });
  }, [messages, streaming]);

  const available = Boolean(status?.enabled && status?.available);

  /**
   * Stops the stream that is currently writing into the chat, if any. Bumping
   * the stream id first makes every late delta or final patch from it a no-op,
   * so it cannot land in whichever conversation is shown next.
   */
  function abortActiveStream() {
    streamIdRef.current += 1;
    const controller = abortRef.current;
    abortRef.current = null;
    controller?.abort();
    setStreaming(false);
  }

  async function openConversation(conversation: Conversation) {
    abortActiveStream();
    try {
      const full = await getConversation(conversation.conversation_id);
      setActive(full);
      setMessages(full.messages ?? []);
      setCollectionId(full.collection_id ?? '');
      const lastAssistant = [...(full.messages ?? [])]
        .reverse()
        .find((message) => message.role === 'assistant');
      setCitations(lastAssistant?.citations ?? []);
      setError(null);
    } catch (cause) {
      setError(explainError(cause));
    }
  }

  async function startConversation() {
    abortActiveStream();
    try {
      const conversation = await createConversation({
        collection_id: collectionId || null,
        title: 'Nova conversa',
      });
      setActive(conversation);
      setMessages([]);
      setCitations([]);
      setError(null);
      await refreshConversations();
      return conversation;
    } catch (cause) {
      setError(explainError(cause));
      return null;
    }
  }

  async function handleDelete(conversation: Conversation) {
    // Deleting the conversation that is streaming ends its stream first.
    if (streamConversationRef.current === conversation.conversation_id) abortActiveStream();
    try {
      await deleteConversation(conversation.conversation_id);
      if (active?.conversation_id === conversation.conversation_id) {
        setActive(null);
        setMessages([]);
        setCitations([]);
      }
      await refreshConversations();
      toast({ title: 'Conversa removida', variant: 'success' });
    } catch (cause) {
      toast({ title: 'Falha ao remover', description: explainError(cause), variant: 'error' });
    }
  }

  async function handleSend() {
    const content = input.trim();
    if (!content || streaming) return;
    let conversation = active;
    if (!conversation) conversation = await startConversation();
    if (!conversation) return;

    const requestId =
      typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
        ? crypto.randomUUID()
        : `req-${Date.now()}`;
    const userMessage: ChatMessage = { role: 'user', content, citations: [] };
    const assistantMessage: ChatMessage = { role: 'assistant', content: '', citations: [] };

    const streamId = streamIdRef.current + 1;
    streamIdRef.current = streamId;
    streamConversationRef.current = conversation.conversation_id;
    const isCurrent = () => streamIdRef.current === streamId;
    const controller = new AbortController();
    abortRef.current = controller;

    setMessages((current) => [...current, userMessage, assistantMessage]);
    setInput('');
    setError(null);
    setCitations([]);
    setStreaming(true);
    setPhase('recuperando fontes');

    let text = '';
    try {
      await streamChatMessage(
        conversation.conversation_id,
        {
          content,
          request_id: requestId,
          collection_id: collectionId || null,
          top_k: 8,
        },
        {
          onEvent: (event: ChatEvent) => {
            // Events from a stream that was switched away from are dropped.
            if (!isCurrent()) return;
            if (event.type === 'status') {
              setPhase(event.state);
            } else if (event.type === 'delta') {
              text += event.content;
              setMessages((current) => {
                const next = [...current];
                next[next.length - 1] = { ...assistantMessage, content: text };
                return next;
              });
            } else if (event.type === 'citation') {
              setCitations((current) =>
                current.some((item) => item.chunk_id === event.citation.chunk_id)
                  ? current
                  : [...current, event.citation],
              );
            } else if (event.type === 'done') {
              text = event.content || text;
              setMessages((current) => {
                const next = [...current];
                next[next.length - 1] = {
                  ...assistantMessage,
                  content: text,
                  citations: event.citations,
                };
                return next;
              });
              setCitations(event.citations);
              setPhase('concluído');
            } else if (event.type === 'error') {
              setError(`${event.message} (${event.code})`);
              setMessages((current) => {
                const next = [...current];
                next[next.length - 1] = {
                  ...assistantMessage,
                  content: text || 'A geração não foi concluída.',
                  status: 'failed',
                  error_code: event.code,
                };
                return next;
              });
            }
          },
        },
        controller.signal,
      );
    } catch (cause) {
      // A superseded stream has already been cleaned up by its replacement.
      if (!isCurrent()) return;
      if (cause instanceof DOMException && cause.name === 'AbortError') {
        setMessages((current) => {
          const next = [...current];
          next[next.length - 1] = {
            ...assistantMessage,
            content: text || 'Geração interrompida pelo usuário.',
            status: 'cancelled',
          };
          return next;
        });
        setPhase('cancelado');
      } else {
        setError(explainError(cause));
        setPhase('erro');
      }
    } finally {
      if (isCurrent()) {
        setStreaming(false);
        abortRef.current = null;
      }
      await refreshConversations();
    }
  }

  function stop() {
    abortRef.current?.abort();
  }

  async function handleIndex() {
    if (!selectedConversion) {
      toast({ title: 'Selecione uma conversão', variant: 'warning' });
      return;
    }
    setIndexing(true);
    try {
      const job = await createIndexJob({
        collection_id: collectionId || null,
        collection_name: collectionId ? null : 'Documentos',
        conversion_ids: [selectedConversion],
        scope: 'documents',
      });
      let current = job;
      for (let attempt = 0; attempt < 20 && ['queued', 'running'].includes(current.status); attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 700));
        current = await getIndexJob(job.job_id);
      }
      const cols = await listCollections();
      setCollections(cols.items);
      if (!collectionId) {
        const created = cols.items.find((item) => item.name === 'Documentos');
        if (created) setCollectionId(created.collection_id);
      }
      if (current.status === 'completed') {
        toast({
          title: 'Documento indexado',
          description: `${String((current.stats as { chunks?: number }).chunks ?? 0)} trechos disponíveis.`,
          variant: 'success',
        });
      } else {
        toast({
          title: 'Indexação não concluída',
          description: current.error?.message ?? current.status,
          variant: 'warning',
        });
      }
    } catch (cause) {
      toast({ title: 'Falha ao indexar', description: explainError(cause), variant: 'error' });
    } finally {
      setIndexing(false);
    }
  }

  const currentCollection = useMemo(
    () => collections.find((item) => item.collection_id === collectionId) ?? null,
    [collections, collectionId],
  );

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Assistente local"
        title="Pergunte aos seus documentos"
        description={
          <span className="flex flex-wrap items-center gap-2">
            <span>Qwen3.5-4B via Ollama · respostas com fontes citáveis</span>
            <Badge variant={available ? 'success' : 'warning'}>
              {available ? 'modelo disponível' : status?.enabled ? 'modelo ausente' : 'desativado'}
            </Badge>
            {status?.model ? <span className="text-xs text-muted-2">{status.model}</span> : null}
          </span>
        }
        actions={
          <Button onClick={() => void startConversation()}>
            <MessageSquarePlus aria-hidden="true" />
            Nova conversa
          </Button>
        }
      />

      {statusError ? (
        <Alert variant="error" title="Assistente indisponível">
          {statusError}
        </Alert>
      ) : null}
      {status && !status.enabled ? (
        <Alert variant="warning" title="Assistente desativado">
          Defina ASSISTANT_ENABLED=true e inicie o Ollama com o modelo {status.model}. As conversões
          continuam funcionando normalmente sem o assistente.
        </Alert>
      ) : null}
      {status?.enabled && !status.available ? (
        <Alert variant="warning" title="Modelo não instalado">
          Execute <code className="rounded bg-surface-3 px-1">ollama pull {status.model}</code>. Nada é
          baixado automaticamente ao abrir esta página.
        </Alert>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[16rem_minmax(0,1fr)_18rem]">
        {/* Conversations */}
        <Card className="h-fit">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Conversas</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 pt-0">
            <label className="block text-[11px] uppercase tracking-widest text-muted-2" htmlFor="collection">
              Coleção
            </label>
            <select
              id="collection"
              value={collectionId}
              onChange={(event) => setCollectionId(event.target.value)}
              className="w-full rounded-lg border border-border bg-surface-2 px-2 py-2 text-xs text-foreground"
            >
              <option value="">Todas as coleções autorizadas</option>
              {collections.map((collection) => (
                <option key={collection.collection_id} value={collection.collection_id}>
                  {collection.name} ({collection.chunks})
                </option>
              ))}
            </select>
            <div className="mt-2 space-y-1">
              {conversations.length === 0 ? (
                <p className="text-xs text-muted-2">Nenhuma conversa ainda.</p>
              ) : (
                conversations.map((conversation) => (
                  <div
                    key={conversation.conversation_id}
                    className={cn(
                      'group flex items-center gap-1 rounded-lg px-2 py-1.5',
                      active?.conversation_id === conversation.conversation_id
                        ? 'bg-accent-soft text-foreground'
                        : 'text-muted hover:bg-surface-2',
                    )}
                  >
                    <button
                      type="button"
                      onClick={() => void openConversation(conversation)}
                      className="min-w-0 flex-1 truncate text-left text-xs"
                    >
                      {conversation.title}
                      <span className="block text-[10px] text-muted-2">
                        {conversation.updated_at ? formatRelative(conversation.updated_at) : ''}
                      </span>
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleDelete(conversation)}
                      aria-label="Excluir conversa"
                      className="rounded p-1 text-muted-2 hover:text-danger"
                    >
                      <Trash2 className="size-3.5" aria-hidden="true" />
                    </button>
                  </div>
                ))
              )}
            </div>
          </CardContent>
        </Card>

        {/* Chat */}
        <Card className="flex min-h-[28rem] flex-col">
          <CardHeader className="border-b border-border/70 pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Bot className="size-4 text-accent" aria-hidden="true" />
              {active?.title ?? 'Nova conversa'}
              {currentCollection ? (
                <Badge variant="outline" className="ml-auto">
                  {currentCollection.name}
                </Badge>
              ) : null}
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-1 flex-col gap-3 p-4">
            <div className="flex-1 space-y-3 overflow-y-auto pr-1" aria-live="polite">
              {messages.length === 0 ? (
                <div className="flex h-full min-h-[16rem] flex-col items-center justify-center gap-2 text-center">
                  <Bot className="size-8 text-muted-2" aria-hidden="true" />
                  <p className="text-sm text-muted">
                    Pergunte algo sobre os documentos indexados. A resposta cita a página e o trecho.
                  </p>
                  <p className="text-xs text-muted-2">
                    Sem fonte suficiente, o assistente informa que não encontrou evidência.
                  </p>
                </div>
              ) : (
                messages.map((message, index) => (
                  <div
                    key={`${message.role}-${index}`}
                    className={cn(
                      'max-w-[92%] rounded-xl px-3 py-2 text-sm',
                      message.role === 'user'
                        ? 'ml-auto bg-accent text-accent-foreground'
                        : 'bg-surface-2 text-foreground',
                    )}
                  >
                    {message.role === 'assistant' ? (
                      <div className="space-y-1 leading-relaxed">{renderMarkdown(message.content || '…')}</div>
                    ) : (
                      <p className="whitespace-pre-wrap">{message.content}</p>
                    )}
                    {message.status === 'cancelled' ? (
                      <p className="mt-1 text-[10px] text-warning">cancelado</p>
                    ) : null}
                    {message.status === 'failed' ? (
                      <p className="mt-1 text-[10px] text-danger">falhou</p>
                    ) : null}
                  </div>
                ))
              )}
              {streaming ? (
                <p className="flex items-center gap-2 text-xs text-muted-2">
                  <Loader2 className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                  {phase ?? 'gerando'}
                </p>
              ) : null}
              <div ref={endRef} />
            </div>

            {error ? (
              <Alert variant="error" title="Falha recuperável">
                {error}
              </Alert>
            ) : null}

            <div className="flex items-end gap-2 border-t border-border/70 pt-3">
              <label className="sr-only" htmlFor="chat-input">
                Mensagem
              </label>
              <textarea
                id="chat-input"
                value={input}
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey) {
                    event.preventDefault();
                    void handleSend();
                  }
                }}
                rows={2}
                placeholder="Pergunte sobre os documentos…"
                className="min-h-[2.5rem] flex-1 resize-none rounded-xl border border-border bg-surface-2 px-3 py-2 text-sm text-foreground placeholder:text-muted-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              />
              {streaming ? (
                <Button variant="secondary" onClick={stop}>
                  <Square aria-hidden="true" />
                  Parar
                </Button>
              ) : (
                <Button onClick={() => void handleSend()} disabled={!input.trim()}>
                  <Send aria-hidden="true" />
                  Enviar
                </Button>
              )}
            </div>
            <p className="text-[10px] text-muted-2">
              O conteúdo das fontes é tratado como dado: instruções contidas no documento são ignoradas.
            </p>
          </CardContent>
        </Card>

        {/* Sources + indexing */}
        <div className="space-y-4">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <FileText className="size-4 text-accent" aria-hidden="true" />
                Fontes
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 pt-0">
              {citations.length === 0 ? (
                <p className="text-xs text-muted-2">As fontes citadas aparecem aqui.</p>
              ) : (
                citations.map((citation, index) => (
                  <div key={citation.chunk_id} className="rounded-lg border border-border bg-surface/60 p-2">
                    <p className="text-[11px] font-medium text-foreground">
                      [{citation.index ?? index + 1}] {citation.document_name ?? citation.document_id}
                      {citation.page_number ? ` · p. ${citation.page_number}` : ''}
                    </p>
                    <p className="mt-1 line-clamp-4 text-[11px] leading-relaxed text-muted">
                      {citation.excerpt}
                    </p>
                    {citation.conversion_id ? (
                      <Link
                        to={`/conversions/${citation.conversion_id}/result`}
                        className="mt-1 inline-block text-[10px] font-medium text-accent hover:underline"
                      >
                        Abrir conversão
                      </Link>
                    ) : null}
                  </div>
                ))
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Database className="size-4 text-accent" aria-hidden="true" />
                Indexar documento
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 pt-0">
              <select
                value={selectedConversion}
                onChange={(event) => setSelectedConversion(event.target.value)}
                aria-label="Conversão para indexar"
                className="w-full rounded-lg border border-border bg-surface-2 px-2 py-2 text-xs text-foreground"
              >
                <option value="">Selecione uma conversão</option>
                {(conversions.data?.items ?? []).map((item) => (
                  <option key={item.conversion_id} value={item.conversion_id}>
                    {item.filename}
                  </option>
                ))}
              </select>
              <Button
                size="sm"
                variant="secondary"
                className="w-full"
                onClick={() => void handleIndex()}
                disabled={indexing || !selectedConversion}
              >
                {indexing ? (
                  <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
                ) : (
                  <Database aria-hidden="true" />
                )}
                {indexing ? 'Indexando…' : 'Indexar na coleção'}
              </Button>
              <p className="text-[10px] leading-relaxed text-muted-2">
                Somente fontes explicitamente selecionadas entram no índice. Excluir a coleção remove
                os trechos e impede novas citações.
              </p>
              {indexing ? (
                <p className="flex items-center gap-1 text-[10px] text-muted-2">
                  <TriangleAlert className="size-3" aria-hidden="true" />
                  Aguarde a conclusão do job.
                </p>
              ) : null}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
