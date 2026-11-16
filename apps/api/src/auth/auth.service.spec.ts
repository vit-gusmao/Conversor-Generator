import { JwtService } from '@nestjs/jwt';

import { AppConfigService } from '../config/app-config.service';
import { PrismaService } from '../database/prisma.service';
import { FakePrisma } from '../../test/helpers/fake-prisma';
import { uuidPattern } from '../../test/helpers/test-utils';

import { AuthService, expiresInSeconds, type JwtPayload } from './auth.service';

describe('AuthService', () => {
  let prisma: FakePrisma;
  let jwt: JwtService;
  let service: AuthService;

  const config = {
    jwtSecret: 'segredo-de-teste',
    jwtExpiresIn: '7d',
    authMode: 'optional',
  } as unknown as AppConfigService;

  beforeEach(() => {
    prisma = new FakePrisma();
    jwt = new JwtService({ secret: 'segredo-de-teste' });
    service = new AuthService(prisma as unknown as PrismaService, jwt, config);
  });

  it('issues a local session token for a browser device', async () => {
    const session = await service.createAnonymousSession('device-abc-123');

    expect(session.token_type).toBe('Bearer');
    expect(session.expires_in).toBe(7 * 24 * 3600);
    expect(session.user.id).toMatch(uuidPattern);
    expect(session.user.is_anonymous).toBe(true);

    const payload = await jwt.verifyAsync<JwtPayload>(session.access_token, {
      secret: 'segredo-de-teste',
    });
    expect(payload.sub).toBe(session.user.id);
    expect(payload.anonymous).toBe(true);
  });

  it('creates a deterministic device identity', async () => {
    const first = await service.getOrCreateDeviceUser('device-abc-123');
    const second = await service.getOrCreateDeviceUser('device-abc-123');

    expect(first.id).toBe(second.id);
    expect(first.isAnonymous).toBe(true);
    expect(prisma.users).toHaveLength(1);
  });

  it('verifies access tokens and rejects garbage', async () => {
    const session = await service.createAnonymousSession('device-xyz-999');

    const authenticated = await service.verifyAccessToken(session.access_token);
    expect(authenticated?.id).toBe(session.user.id);
    expect(authenticated?.deviceId).toBe('device-xyz-999');

    await expect(service.verifyAccessToken('nao-e-um-token')).resolves.toBeNull();
  });

  it('parses JWT expiry strings', () => {
    expect(expiresInSeconds('7d')).toBe(604800);
    expect(expiresInSeconds('12h')).toBe(43200);
    expect(expiresInSeconds('30m')).toBe(1800);
    expect(expiresInSeconds('3600')).toBe(3600);
  });
});
