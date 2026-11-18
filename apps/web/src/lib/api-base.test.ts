import { afterEach, describe, expect, it, vi } from 'vitest';
import { buildUrl, getApiBase } from '@/lib/api';

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('getApiBase', () => {
  it('defaults to the same-origin path', () => {
    vi.stubEnv('VITE_API_BASE', '');

    expect(getApiBase()).toBe('/api/v1');
  });

  it('uses VITE_API_BASE when it is set', () => {
    vi.stubEnv('VITE_API_BASE', 'https://api.build.example/api/v1');

    expect(getApiBase()).toBe('https://api.build.example/api/v1');
    expect(buildUrl('/conversions')).toBe('https://api.build.example/api/v1/conversions');
  });

  it('trims a trailing slash from VITE_API_BASE', () => {
    vi.stubEnv('VITE_API_BASE', 'https://api.build.example/api/v1/');

    expect(getApiBase()).toBe('https://api.build.example/api/v1');
  });
});
