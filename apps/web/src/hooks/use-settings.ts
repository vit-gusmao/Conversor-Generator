import { useQuery } from '@tanstack/react-query';
import type { PlatformSettings } from '@pdf2x/contracts';
import { STALE_TIME, queryKeys } from '@/lib/query-keys';
import { getPlatformSettings } from '@/services/settings';

export function useSettings() {
  return useQuery<PlatformSettings>({
    queryKey: queryKeys.settings,
    queryFn: getPlatformSettings,
    staleTime: STALE_TIME.settings,
    retry: 1,
  });
}
