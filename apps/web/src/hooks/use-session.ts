import { useEffect } from 'react';
import { useAuthStore } from '@/stores/auth';
import type { SessionStatus } from '@/stores/auth';
import type { User } from '@pdf2x/contracts';

export interface SessionState {
  token: string | null;
  user: User | null;
  status: SessionStatus;
  error: string | null;
}

/**
 * Creates the anonymous device session once. Safe to call from every page:
 * the store deduplicates concurrent calls and persists the token.
 */
export function useEnsureSession(): SessionState {
  const token = useAuthStore((state) => state.token);
  const user = useAuthStore((state) => state.user);
  const status = useAuthStore((state) => state.status);
  const error = useAuthStore((state) => state.error);
  const ensureSession = useAuthStore((state) => state.ensureSession);

  useEffect(() => {
    if (!token && status === 'idle') {
      void ensureSession();
    }
  }, [token, status, ensureSession]);

  return { token, user, status, error };
}
