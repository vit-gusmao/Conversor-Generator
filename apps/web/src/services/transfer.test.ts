import { beforeEach, describe, expect, it, vi } from 'vitest';
import { defaultConversionOptions } from '@pdf2x/contracts';

vi.mock('@/services/batches', () => ({
  createBatch: vi.fn(),
  registerItem: vi.fn(),
  cancelItem: vi.fn(),
}));

vi.mock('@/services/uploads', () => ({
  putPart: vi.fn(),
  completeUpload: vi.fn(),
}));

vi.mock('@/lib/sha256', () => ({
  sha256Hex: vi.fn(async () => 'a'.repeat(64)),
}));

import { cancelItem, createBatch, registerItem } from '@/services/batches';
import { completeUpload, putPart } from '@/services/uploads';
import {
  cancelTransfer,
  isTransferActive,
  startBatch,
  type TransferItem,
  type TransferItemPatch,
} from '@/services/transfer';

const OPTIONS = defaultConversionOptions('pdf');

interface PendingPart {
  signal: AbortSignal | undefined;
  resolve: () => void;
}

let pendingParts: PendingPart[] = [];

function item(id: string, bytes = 4): TransferItem {
  return {
    clientItemId: id,
    file: new File([new Uint8Array(bytes)], `${id}.pdf`, { type: 'application/pdf' }),
    filename: `${id}.pdf`,
    size: bytes,
    options: OPTIONS,
  };
}

function recorder() {
  const patches: Array<{ id: string; patch: TransferItemPatch }> = [];
  return {
    patches,
    onItemPatch: (id: string, patch: TransferItemPatch) => {
      patches.push({ id, patch });
    },
  };
}

beforeEach(() => {
  pendingParts = [];
  vi.mocked(createBatch).mockResolvedValue({ batch_id: 'batch-1' } as never);
  vi.mocked(cancelItem).mockResolvedValue({} as never);
  vi.mocked(registerItem).mockImplementation(
    async (_batchId: string, input: { client_item_id: string }) =>
      ({
        upload_id: `up-${input.client_item_id}`,
        part_size: 4,
        next_part: 0,
        item: { item_id: `item-${input.client_item_id}`, status: 'pending_upload', conversion_id: null },
      }) as never,
  );
  // Behaves like fetch: the request rejects with AbortError once its signal aborts.
  vi.mocked(putPart).mockImplementation(
    (_uploadId: string, _part: number, _chunk: Blob, _digest: string, options?: { signal?: AbortSignal }) =>
      new Promise((resolve, reject) => {
        const signal = options?.signal;
        pendingParts.push({ signal, resolve: () => resolve({} as never) });
        signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
      }),
  );
  vi.mocked(completeUpload).mockResolvedValue({ conversion_id: 'conv-1' } as never);
});

describe('cancelTransfer', () => {
  it('aborts the signal of an in-flight part and keeps the item out of uploaded/failed', async () => {
    const { patches, onItemPatch } = recorder();
    const run = startBatch({ items: [item('c1', 8)], onItemPatch });

    await vi.waitFor(() => expect(pendingParts).toHaveLength(1));
    expect(isTransferActive('c1')).toBe(true);

    expect(cancelTransfer('c1')).toBe(true);
    expect(pendingParts[0]?.signal?.aborted).toBe(true);

    const result = await run;
    expect(result.failures).toEqual([]);
    expect(completeUpload).not.toHaveBeenCalled();
    expect(isTransferActive('c1')).toBe(false);

    const c1States = patches
      .filter((entry) => entry.id === 'c1')
      .map((entry) => entry.patch.state)
      .filter((state) => state !== undefined);
    expect(c1States).toEqual(['uploading']);
  });

  it('stops an item still waiting for a slot so it never registers or starts', async () => {
    const { patches, onItemPatch } = recorder();
    const items = ['q1', 'q2', 'q3', 'q4'].map((id) => item(id, 4));
    const run = startBatch({ items, onItemPatch });

    // Three slots are busy; q4 is queued behind them.
    await vi.waitFor(() => expect(pendingParts).toHaveLength(3));
    expect(cancelTransfer('q4')).toBe(true);

    for (const part of pendingParts) part.resolve();
    const result = await run;

    expect(result.failures).toEqual([]);
    const registered = vi.mocked(registerItem).mock.calls.map((call) => call[1].client_item_id);
    expect(registered).toHaveLength(3);
    expect(registered).not.toContain('q4');
    expect(patches.some((entry) => entry.id === 'q4' && entry.patch.state !== undefined)).toBe(false);
    expect(patches.filter((entry) => entry.id === 'q1' && entry.patch.state === 'uploaded')).toHaveLength(1);
  });

  it('returns false for an item with no active transfer', () => {
    expect(cancelTransfer('nunca-iniciado')).toBe(false);
  });
});

describe('cancelTransfer on the server', () => {
  it('passes the transfer signal to the registration request', async () => {
    const { onItemPatch } = recorder();
    const run = startBatch({ items: [item('r1', 4)], onItemPatch });
    await vi.waitFor(() => expect(registerItem).toHaveBeenCalled());

    const options = vi.mocked(registerItem).mock.calls[0]![2];
    expect(options?.signal).toBeInstanceOf(AbortSignal);
    expect(options?.signal?.aborted).toBe(false);

    // Completing the flow keeps the other tests' mocks clean.
    await vi.waitFor(() => expect(pendingParts).toHaveLength(1));
    pendingParts[0]!.resolve();
    await run;
  });

  it('aborts an in-flight completeUpload and cancels the registered item on the server', async () => {
    let completeSignal: AbortSignal | undefined;
    vi.mocked(completeUpload).mockImplementation(
      (_uploadId: string, _sha: string, _total: number, options?: { signal?: AbortSignal }) =>
        new Promise((_resolve, reject) => {
          completeSignal = options?.signal;
          completeSignal?.addEventListener('abort', () =>
            reject(new DOMException('aborted', 'AbortError')),
          );
        }),
    );
    const { patches, onItemPatch } = recorder();
    const run = startBatch({ items: [item('c9', 4)], onItemPatch });

    await vi.waitFor(() => expect(pendingParts).toHaveLength(1));
    pendingParts[0]!.resolve();
    await vi.waitFor(() => expect(completeSignal).toBeDefined());

    expect(cancelTransfer('c9')).toBe(true);
    expect(completeSignal?.aborted).toBe(true);
    const result = await run;

    expect(result.failures).toEqual([]);
    expect(cancelItem).toHaveBeenCalledTimes(1);
    expect(cancelItem).toHaveBeenCalledWith('batch-1', 'item-c9');
    expect(patches.some((entry) => entry.id === 'c9' && entry.patch.state === 'uploaded')).toBe(false);
  });

  it('cancels on the server a registration that resolves after the local cancel', async () => {
    let finishRegistration: (() => void) | undefined;
    vi.mocked(registerItem).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishRegistration = () =>
            resolve({
              upload_id: 'up-late',
              part_size: 4,
              next_part: 0,
              item: { item_id: 'item-late', status: 'pending_upload', conversion_id: null },
            } as never);
        }),
    );
    const { onItemPatch } = recorder();
    const run = startBatch({ items: [item('late', 4)], onItemPatch });

    await vi.waitFor(() => expect(finishRegistration).toBeDefined());
    expect(cancelTransfer('late')).toBe(true);
    finishRegistration!();
    const result = await run;

    expect(result.failures).toEqual([]);
    expect(putPart).not.toHaveBeenCalled();
    expect(cancelItem).toHaveBeenCalledWith('batch-1', 'item-late');
  });

  it('does not call the server for an item that was never registered', async () => {
    const { onItemPatch } = recorder();
    const items = ['q1', 'q2', 'q3', 'q4'].map((id) => item(id, 4));
    const run = startBatch({ items, onItemPatch });

    await vi.waitFor(() => expect(pendingParts).toHaveLength(3));
    expect(cancelTransfer('q4')).toBe(true);

    for (const part of pendingParts) part.resolve();
    await run;

    expect(cancelItem).not.toHaveBeenCalledWith('batch-1', 'item-q4');
  });

  it('keeps the local cancel when the server cancel fails', async () => {
    vi.mocked(cancelItem).mockRejectedValue(new Error('gateway offline'));
    const { onItemPatch } = recorder();
    const run = startBatch({ items: [item('f1', 8)], onItemPatch });

    await vi.waitFor(() => expect(pendingParts).toHaveLength(1));
    expect(cancelTransfer('f1')).toBe(true);
    const result = await run;

    expect(result.failures).toEqual([]);
    expect(cancelItem).toHaveBeenCalledWith('batch-1', 'item-f1');
    expect(isTransferActive('f1')).toBe(false);
  });
});
