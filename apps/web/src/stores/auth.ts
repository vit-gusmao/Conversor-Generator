import type { AuthSession, User } from '@pdf2x/contracts';
import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { setAuthTokenGetter, errorMessage } from '@/lib/api';
import { createAnonymousSession } from '@/services/auth';

export type SessionStatus = 'idle' | 'loading' | 'ready' | 'error';

interface AuthState {
  token: string | null;
  user: User | null;
  status: SessionStatus;
  error: string | null;
  ensureSession: () => Promise<string | null>;
  setSession: (session: AuthSession) => void;
  setUser: (user: User | null) => void;
  clear: () => void;
}

let inFlight: Promise<string | null> | null = null;

/** Local sessions are bearer tokens with an `exp`; refresh before using them. */
function tokenExpired(token: string): boolean {
  try {
    const payload = token.split('.')[1];
    if (!payload) return false;
    const normalized = payload.replace(/-/g, '+').replace(/_/g, '/');
    const decoded = JSON.parse(atob(normalized)) as { exp?: number };
    return typeof decoded.exp === 'number' ? decoded.exp * 1000 <= Date.now() + 5_000 : false;
  } catch {
    return false;
  }
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      token: null,
      user: null,
      status: 'idle',
      error: null,

      ensureSession: async () => {
        const existing = get().token;
        if (existing && !tokenExpired(existing)) return existing;
        if (existing) set({ token: null });
        if (inFlight) return inFlight;

        set({ status: 'loading', error: null });
        inFlight = createAnonymousSession()
          .then((session) => {
            set({
              token: session.access_token,
              user: session.user,
              status: 'ready',
              error: null,
            });
            return session.access_token;
          })
          .catch((error: unknown) => {
            set({ status: 'error', error: errorMessage(error) });
            return null;
          })
          .finally(() => {
            inFlight = null;
          });

        return inFlight;
      },

      setSession: (session) =>
        set({
          token: session.access_token,
          user: session.user,
          status: 'ready',
          error: null,
        }),

      setUser: (user) => set({ user }),

      clear: () => set({ token: null, user: null, status: 'idle', error: null }),
    }),
    {
      name: 'pdf2x.auth',
      partialize: (state) => ({ token: state.token, user: state.user }),
      onRehydrateStorage: () => (state) => {
        if (state?.token) state.status = 'ready';
      },
    },
  ),
);

// Every apiFetch call resolved through the store, never through module globals.
setAuthTokenGetter(() => useAuthStore.getState().token);

export function getAccessToken(): string | null {
  return useAuthStore.getState().token;
}
