import type { PlatformSettings } from '@pdf2x/contracts';
import { apiFetch } from '@/lib/api';

export async function getPlatformSettings(): Promise<PlatformSettings> {
  return apiFetch<PlatformSettings>('/settings');
}
