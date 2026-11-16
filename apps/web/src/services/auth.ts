import type { AuthSession, User } from '@pdf2x/contracts';
import { apiFetch } from '@/lib/api';

/**
 * Local, account-free session: the server derives one deterministic owner per
 * browser (cookie + device id) and returns a bearer token for the API. There is
 * no registration, login or password.
 */
export async function createAnonymousSession(): Promise<AuthSession> {
  return apiFetch<AuthSession>('/auth/anonymous', { method: 'POST' });
}

export async function fetchCurrentUser(): Promise<User> {
  return apiFetch<User>('/auth/me');
}
