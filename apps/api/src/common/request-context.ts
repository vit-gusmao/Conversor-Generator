import type { Request } from 'express';

export type UserRole = 'owner' | 'admin' | 'viewer';

/** What the guards put on the request and what `@CurrentUser()` returns. */
export interface AuthenticatedUser {
  id: string;
  email: string | null;
  name: string | null;
  role: UserRole;
  isAnonymous: boolean;
  createdAt: string;
  deviceId: string | null;
}

export interface RequestWithContext extends Request {
  user?: AuthenticatedUser;
  requestId?: string;
  deviceId?: string | null;
}

export function isUserRole(value: unknown): value is UserRole {
  return value === 'owner' || value === 'admin' || value === 'viewer';
}
