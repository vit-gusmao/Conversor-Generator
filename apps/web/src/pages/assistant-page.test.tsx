import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { ChatEvent, Conversation } from '@/services/assistant';
import { AssistantPage } from '@/pages/assistant-page';

vi.mock('@/hooks/use-session', () => ({
  useEnsureSession: vi.fn(),
}));

vi.mock('@/hooks/use-conversions', () => ({
  useConversions: vi.fn(() => ({ data: undefined })),
}));

vi.mock('@/services/knowledge', () => ({
  listCollections: vi.fn(),
  createIndexJob: vi.fn(),
  getIndexJob: vi.fn(),
}));

vi.mock('@/services/assistant', () => ({
  getModelStatus: vi.fn(),
  listConversations: vi.fn(),
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  deleteConversation: vi.fn(),
  streamChatMessage: vi.fn(),
}));

import {
  createConversation,
  deleteConversation,
  getConversation,
  getModelStatus,
  listConversations,
  streamChatMessage,
} from '@/services/assistant';
import { listCollections } from '@/services/knowledge';

interface LiveStream {
  onEvent: (event: ChatEvent) => void;
  signal: AbortSignal | undefined;
}

/** Every stream opened by the page; each one stays open until its signal aborts. */
let streams: LiveStream[] = [];

function delta(content: string): ChatEvent {
  return { type: 'delta', content } as ChatEvent;
}

function summary(id: string, title: string): Conversation {
  return {
    conversation_id: id,
    title,
    collection_id: null,
    document_id: null,
    run_id: null,
    model: null,
    updated_at: '2026-11-19T10:00:00Z',
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <AssistantPage />
    </MemoryRouter>,
  );
}

async function sendMessage(text: string) {
  fireEvent.change(await screen.findByPlaceholderText(/pergunte sobre os documentos/i), {
    target: { value: text },
  });
  fireEvent.click(screen.getByRole('button', { name: /^enviar$/i }));
}

beforeEach(() => {
  streams = [];
  vi.mocked(getModelStatus).mockResolvedValue({
    enabled: true,
    available: true,
    model: 'qwen',
  } as never);
  vi.mocked(listCollections).mockResolvedValue({ items: [], total: 0 } as never);
  vi.mocked(listConversations).mockResolvedValue({ items: [], total: 0 });
  vi.mocked(deleteConversation).mockResolvedValue(undefined);
  vi.mocked(streamChatMessage).mockImplementation(
    (_conversationId, _body, handlers, signal) =>
      new Promise((_resolve, reject) => {
        streams.push({ onEvent: handlers.onEvent, signal });
        signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
      }),
  );
});

describe('AssistantPage streaming', () => {
  it('drops late deltas of a stream once another conversation is opened', async () => {
    vi.mocked(listConversations).mockResolvedValue({
      items: [summary('conv-b', 'Conversa B')],
      total: 1,
    });
    vi.mocked(createConversation).mockResolvedValue(summary('conv-a', 'Nova conversa'));
    vi.mocked(getConversation).mockResolvedValue({
      ...summary('conv-b', 'Conversa B'),
      messages: [
        { role: 'user', content: 'pergunta B', citations: [] },
        { role: 'assistant', content: 'resposta B', citations: [] },
      ],
    } as Conversation);

    renderPage();
    await sendMessage('pergunta A');
    await waitFor(() => expect(streams).toHaveLength(1));
    act(() => streams[0]!.onEvent(delta('Resposta parcial')));

    fireEvent.click(await screen.findByRole('button', { name: /conversa b/i }));
    expect(await screen.findByText('resposta B')).toBeInTheDocument();

    expect(streams[0]!.signal?.aborted).toBe(true);
    act(() => streams[0]!.onEvent(delta(' vazamento')));

    // The opened conversation keeps its own last message intact.
    expect(screen.getByText('resposta B')).toBeInTheDocument();
    expect(screen.queryByText(/vazamento/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Resposta parcial/)).not.toBeInTheDocument();
  });

  it('aborts the stream of a conversation deleted while it streams', async () => {
    vi.mocked(listConversations)
      .mockResolvedValueOnce({ items: [], total: 0 })
      .mockResolvedValue({ items: [summary('conv-a', 'Nova conversa')], total: 1 });
    vi.mocked(createConversation).mockResolvedValue(summary('conv-a', 'Nova conversa'));

    renderPage();
    await sendMessage('pergunta A');
    await waitFor(() => expect(streams).toHaveLength(1));

    fireEvent.click(await screen.findByRole('button', { name: /excluir conversa/i }));
    await waitFor(() => expect(deleteConversation).toHaveBeenCalledWith('conv-a'));

    expect(streams[0]!.signal?.aborted).toBe(true);
    act(() => streams[0]!.onEvent(delta('vazamento')));
    expect(screen.queryByText(/vazamento/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^parar$/i })).not.toBeInTheDocument();
  });
});
