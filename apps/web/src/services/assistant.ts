import { ApiError, apiFetch, authHeaders, buildUrl } from '@/lib/api';

export interface Citation {
  index?: number;
  chunk_id: string;
  document_id: string;
  document_name: string | null;
  run_id: string | null;
  conversion_id?: string | null;
  revision_id: string | null;
  page_number: number | null;
  excerpt: string;
  offset_start: number;
  offset_end: number;
  score?: number;
  token_count?: number;
}

export interface ChatMessage {
  message_id?: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  citations: Citation[];
  model?: string | null;
  status?: string;
  request_id?: string | null;
  error_code?: string | null;
  created_at?: string | null;
}

export interface Conversation {
  conversation_id: string;
  title: string;
  collection_id: string | null;
  document_id: string | null;
  run_id: string | null;
  model: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  messages?: ChatMessage[];
}

export interface ModelStatus {
  enabled: boolean;
  provider: string;
  host: string;
  model: string;
  available: boolean;
  server_version: string | null;
  digest: string | null;
  context_tokens: number;
  max_output_tokens: number;
  max_concurrent: number;
  embedding_model: string;
  local: boolean;
  installed_models: string[];
}

export type ChatEvent =
  | { type: 'status'; state: string; message?: string; sequence?: number }
  | { type: 'delta'; content: string; sequence?: number }
  | { type: 'citation'; citation: Citation; sequence?: number }
  | { type: 'done'; content: string; citations: Citation[]; abstained?: boolean; replayed?: boolean }
  | { type: 'error'; code: string; message: string; sequence?: number };

export async function getModelStatus(): Promise<ModelStatus> {
  return apiFetch<ModelStatus>('/models/status');
}

export async function listConversations(): Promise<{ items: Conversation[]; total: number }> {
  return apiFetch<{ items: Conversation[]; total: number }>('/chat/conversations');
}

export async function createConversation(input: {
  title?: string;
  collection_id?: string | null;
  document_id?: string | null;
  run_id?: string | null;
}): Promise<Conversation> {
  return apiFetch<Conversation>('/chat/conversations', { method: 'POST', json: input });
}

export async function getConversation(id: string): Promise<Conversation> {
  return apiFetch<Conversation>(`/chat/conversations/${encodeURIComponent(id)}`);
}

export async function deleteConversation(id: string): Promise<void> {
  await apiFetch<void>(`/chat/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

/**
 * POST + streaming SSE. `EventSource` cannot send a body or an Authorization
 * header, so the stream is read from `fetch` and parsed incrementally.
 */
export async function streamChatMessage(
  conversationId: string,
  body: { content: string; request_id?: string; collection_id?: string | null; top_k?: number },
  handlers: { onEvent: (event: ChatEvent) => void },
  signal?: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(
      buildUrl(`/chat/conversations/${encodeURIComponent(conversationId)}/messages`),
      {
        method: 'POST',
        headers: {
          ...authHeaders(),
          Accept: 'text/event-stream',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(body),
        signal,
      },
    );
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw new ApiError({
      code: 'NETWORK_ERROR',
      message: 'Não foi possível contatar o assistente local.',
      status: 0,
      details: { cause: cause instanceof Error ? cause.message : String(cause) },
    });
  }

  if (!response.ok) {
    let code = `HTTP_${response.status}`;
    let message = 'O assistente não pôde processar a mensagem.';
    try {
      const parsed = (await response.json()) as { error?: { code?: string; message?: string } };
      if (parsed?.error?.code) code = parsed.error.code;
      if (parsed?.error?.message) message = parsed.error.message;
    } catch {
      // keep defaults
    }
    throw new ApiError({ code, message, status: response.status });
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new ApiError({ code: 'STREAM_UNAVAILABLE', message: 'O stream não está disponível.', status: 0 });
  }
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary = buffer.indexOf('\n\n');
    while (boundary >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const data = block
        .split(/\r?\n/)
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trim())
        .join('');
      if (data) {
        try {
          handlers.onEvent(JSON.parse(data) as ChatEvent);
        } catch {
          // ignore malformed frames; the next one may be valid
        }
      }
      boundary = buffer.indexOf('\n\n');
    }
  }
}
