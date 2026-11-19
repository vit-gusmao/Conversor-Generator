import { describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { IMPORT_ITEMS_POLL_MS, sourceKeys, useImportItems } from '@/hooks/use-sources';

vi.mock('@/services/sources', () => ({
  listImportItems: vi.fn(async () => []),
}));

/** The polling policy is the `refetchInterval` of the items query. */
function intervalFor(client: QueryClient, importId: string): number | false {
  const query = client.getQueryCache().find({ queryKey: sourceKeys.importItems(importId) });
  const options = query?.options as { refetchInterval?: unknown } | undefined;
  const option = options?.refetchInterval;
  if (typeof option !== 'function') throw new Error('items query has no refetchInterval policy');
  return option(query as never, undefined as never) as number | false;
}

function setup(importId: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  renderHook(() => useImportItems(importId), { wrapper });
  return client;
}

describe('useImportItems polling', () => {
  it('polls every few seconds while the import is running', () => {
    const client = setup('imp-1');
    client.setQueryData(sourceKeys.import('imp-1'), { status: 'running' });

    expect(intervalFor(client, 'imp-1')).toBe(IMPORT_ITEMS_POLL_MS);
    expect(IMPORT_ITEMS_POLL_MS).toBe(3_000);
  });

  it('stops polling once the import is terminal', () => {
    for (const status of ['completed', 'partial', 'failed', 'cancelled']) {
      const client = setup(`imp-${status}`);
      client.setQueryData(sourceKeys.import(`imp-${status}`), { status });
      expect(intervalFor(client, `imp-${status}`)).toBe(false);
    }
  });
});
