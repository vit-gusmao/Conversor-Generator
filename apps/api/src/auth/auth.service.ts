import { Injectable, Logger } from '@nestjs/common';
import { JwtService } from '@nestjs/jwt';

import { ApiError } from '../common/errors/api-error';
import type { AuthenticatedUser, UserRole } from '../common/request-context';
import { AppConfigService } from '../config/app-config.service';
import { PrismaService } from '../database/prisma.service';
import type { User as ContractUser, AuthSession } from '@pdf2x/contracts';

export interface JwtPayload {
  sub: string;
  email: string | null;
  role: string;
  anonymous: boolean;
}

/** Minimal user shape used by the auth layer (both real and fake Prisma). */
export interface UserRecord {
  id: string;
  email: string | null;
  passwordHash: string | null;
  name: string | null;
  role: string;
  isAnonymous: boolean;
  deviceId: string | null;
  createdAt: Date;
}

const DEVICE_ID_PATTERN = /^[A-Za-z0-9._:-]{8,128}$/;

export function isValidDeviceId(value: unknown): value is string {
  return typeof value === 'string' && DEVICE_ID_PATTERN.test(value);
}

/** '7d' -> 604800, '12h' -> 43200, '3600' -> 3600. */
export function expiresInSeconds(raw: string): number {
  const match = /^(\d+)([smhd])?$/.exec(raw.trim());
  if (!match) return 604800;
  const amount = Number.parseInt(match[1], 10);
  const unit = match[2] ?? 's';
  const factor = unit === 's' ? 1 : unit === 'm' ? 60 : unit === 'h' ? 3600 : 86400;
  return amount * factor;
}

function toRole(value: string): UserRole {
  return value === 'admin' || value === 'viewer' ? value : 'owner';
}

@Injectable()
export class AuthService {
  private readonly logger = new Logger(AuthService.name);

  constructor(
    private readonly prisma: PrismaService,
    private readonly jwt: JwtService,
    private readonly config: AppConfigService,
  ) {}

  async createAnonymousSession(deviceId?: string): Promise<AuthSession> {
    const id = isValidDeviceId(deviceId) ? deviceId : this.newDeviceId();
    const user = await this.getOrCreateDeviceUser(id);
    return this.issueSession(user);
  }

  /**
   * Deterministic identity for device sessions: the same `x-device-id` always
   * maps to the same user, so an anonymous user keeps access to their files.
   */
  async getOrCreateDeviceUser(deviceId: string): Promise<UserRecord> {
    const existing = await this.prisma.user.findUnique({ where: { deviceId } });
    if (existing) return existing;

    try {
      return await this.prisma.user.create({
        data: { deviceId, isAnonymous: true, role: 'owner' },
      });
    } catch (error) {
      // Concurrent creation for the same device: re-read instead of failing.
      const again = await this.prisma.user.findUnique({ where: { deviceId } });
      if (again) return again;
      this.logger.error('Falha ao criar usuário de dispositivo', String(error));
      throw ApiError.internal({ reason: 'device_user_creation_failed' });
    }
  }

  async findById(id: string): Promise<UserRecord | null> {
    return this.prisma.user.findUnique({ where: { id } });
  }

  async verifyAccessToken(token: string): Promise<AuthenticatedUser | null> {
    try {
      const payload = await this.jwt.verifyAsync<JwtPayload>(token, {
        secret: this.config.jwtSecret,
      });
      if (!payload?.sub) return null;
      const user = await this.findById(payload.sub);
      return user ? this.toAuthenticated(user) : null;
    } catch {
      return null;
    }
  }

  issueSession(user: UserRecord): AuthSession {
    const expiresIn = expiresInSeconds(this.config.jwtExpiresIn);
    const payload: JwtPayload = {
      sub: user.id,
      email: user.email,
      role: user.role,
      anonymous: user.isAnonymous,
    };

    const accessToken = this.jwt.sign(payload, {
      secret: this.config.jwtSecret,
      expiresIn,
    });

    return {
      access_token: accessToken,
      token_type: 'Bearer',
      expires_in: expiresIn,
      user: this.toContractUser(user),
    };
  }

  toAuthenticated(user: UserRecord): AuthenticatedUser {
    return {
      id: user.id,
      email: user.email,
      name: user.name,
      role: toRole(user.role),
      isAnonymous: user.isAnonymous,
      createdAt: user.createdAt.toISOString(),
      deviceId: user.deviceId,
    };
  }

  toContractUser(user: UserRecord): ContractUser {
    return {
      id: user.id,
      email: user.email,
      name: user.name,
      role: toRole(user.role),
      is_anonymous: user.isAnonymous,
      created_at: user.createdAt.toISOString(),
    };
  }

  newDeviceId(): string {
    // globalThis.crypto is available on Node 20+.
    return globalThis.crypto.randomUUID();
  }
}
