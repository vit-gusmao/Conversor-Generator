import { describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { useRetryConversion } from '@/hooks/use-conversions';

vi.mock('@/services/conversions', () => ({
  retryConversion: vi.fn(async () => ({ conversion_id: 'conv-1' })),
}));

describe('useRetryConversion', () => {
  it('invalidates the result, sheet, preview and export data of the previous run', async () => {
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const spy = vi.spyOn(client, 'invalidateQueries');
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useRetryConversion('conv-1'), { wrapper });
    await act(async () => {
      await result.current.mutateAsync(undefined);
    });

    const keys = spy.mock.calls.map((call) => call[0]?.queryKey);
    expect(keys).toEqual(
      expect.arrayContaining([
        ['conversions', 'detail', 'conv-1'],
        ['audit', 'conv-1'],
        ['conversions', 'conv-1', 'result'],
        ['conversions', 'conv-1', 'result-overview'],
        ['conversions', 'conv-1', 'sheets'],
        ['conversions', 'conv-1', 'preview'],
        ['exports', 'conv-1'],
      ]),
    );
  });
});
