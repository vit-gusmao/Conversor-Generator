import type { AuthenticatedUser } from '../../common/request-context';
import { PrismaService } from '../../database/prisma.service';
import { FakePrisma } from '../../../test/helpers/fake-prisma';
import { captureApiErrorAsync } from '../../../test/helpers/test-utils';
import { ConverterClient, type QueryParams } from '../conversions/converter.client';

import { AuditController } from './audit.controller';
import { AuditService, forwardableQuery } from './audit.service';

describe('AuditController / AuditService', () => {
  const user: AuthenticatedUser = {
    id: 'user-1',
    email: null,
    name: null,
    role: 'owner',
    isAnonymous: true,
    createdAt: new Date().toISOString(),
    deviceId: 'device-1',
  };

  describe('query forwarding', () => {
    it('keeps scalars and drops empty/structured values', () => {
      expect(
        forwardableQuery({
          stage: 'extract',
          event_type: 'decision',
          severity: 'warning',
          page: '2',
          since_sequence: 10,
          limit: '50',
          empty: '',
          nested: { a: 1 },
          list: ['first', 'second'],
          nothing: undefined,
        }),
      ).toEqual({
        stage: 'extract',
        event_type: 'decision',
        severity: 'warning',
        page: '2',
        since_sequence: 10,
        limit: '50',
        list: 'first',
      });
    });
  });

  describe('service', () => {
    let prisma: FakePrisma;
    let converter: {
      auditSummary: jest.Mock;
      auditEvents: jest.Mock;
      auditPage: jest.Mock;
      auditCells: jest.Mock;
      auditIssues: jest.Mock;
      auditExport: jest.Mock;
    };
    let service: AuditService;

    const seedConversion = (ownerId: string): void => {
      prisma.conversions.push({
        id: 'conv-1',
        documentId: 'doc-1',
        ownerId,
        status: 'completed',
        quality: 'validated',
        progress: 1,
        stage: 'done',
        activeRunId: 'run-1',
        errorCode: null,
        errorMessage: null,
        statsJson: null,
        validationJson: null,
        deletedAt: null,
        createdAt: new Date(),
        updatedAt: new Date(),
      });
      prisma.runs.push({
        id: 'run-1',
        conversionId: 'conv-1',
        attempt: 1,
        status: 'completed',
        progress: 1,
        stage: 'done',
        startedAt: new Date(),
        finishedAt: new Date(),
        engineVersion: '1.0.0',
        configJson: null,
        errorCode: null,
        errorMessage: null,
        statsJson: null,
        createdAt: new Date(),
      });
    };

    beforeEach(() => {
      prisma = new FakePrisma();
      converter = {
        auditSummary: jest.fn().mockResolvedValue({ conversion_id: 'conv-1' }),
        auditEvents: jest.fn().mockResolvedValue({ items: [], total: 0 }),
        auditPage: jest.fn().mockResolvedValue({ page_number: 1 }),
        auditCells: jest.fn().mockResolvedValue({ items: [], total: 0 }),
        auditIssues: jest.fn().mockResolvedValue({ items: [], total: 0 }),
        auditExport: jest
          .fn()
          .mockResolvedValue({ stream: {}, contentType: 'application/json', filename: 'a.json' }),
      };
      service = new AuditService(
        prisma as unknown as PrismaService,
        converter as unknown as ConverterClient,
      );
    });

    it('forwards query params to the engine, keyed by run id', async () => {
      seedConversion(user.id);

      await service.events(user, 'conv-1', {
        stage: 'extract',
        event_type: 'decision',
        page: '2',
        limit: '50',
        garbage: '',
      });

      const [runId, params] = converter.auditEvents.mock.calls[0] as [string, QueryParams];
      expect(runId).toBe('run-1');
      expect(params).toEqual({
        stage: 'extract',
        event_type: 'decision',
        page: '2',
        limit: '50',
      });
    });

    it('always pins the export format', async () => {
      seedConversion(user.id);

      await service.export(user, 'conv-1', { format: 'txt' });
      const [, params] = converter.auditExport.mock.calls[0] as [string, QueryParams];
      expect(params).toMatchObject({ format: 'txt' });

      await service.export(user, 'conv-1', {});
      const [, defaults] = converter.auditExport.mock.calls[1] as [string, QueryParams];
      expect(defaults).toMatchObject({ format: 'json' });
    });

    it('returns 403 for a conversion owned by someone else', async () => {
      seedConversion('another-user');

      const error = await captureApiErrorAsync(() =>
        service.summary(user, 'conv-1'),
      );

      expect(error.status).toBe(403);
      expect(error.code).toBe('FORBIDDEN');
      expect(converter.auditSummary).not.toHaveBeenCalled();
    });

    it('returns 404 for unknown or deleted conversions', async () => {
      const missing = await captureApiErrorAsync(() => service.summary(user, 'nope'));
      expect(missing.status).toBe(404);

      seedConversion(user.id);
      prisma.conversions[0]['deletedAt'] = new Date();
      const deleted = await captureApiErrorAsync(() => service.summary(user, 'conv-1'));
      expect(deleted.status).toBe(404);
    });

    it('propagates engine unavailability instead of faking data', async () => {
      seedConversion(user.id);
      converter.auditSummary.mockRejectedValue(
        Object.assign(new Error('Motor indisponível'), { status: 503, code: 'CONVERTER_UNAVAILABLE' }),
      );

      await expect(service.summary(user, 'conv-1')).rejects.toMatchObject({
        status: 503,
        code: 'CONVERTER_UNAVAILABLE',
      });
    });
  });

  describe('controller', () => {
    it('passes the user, id and raw query through to the service', async () => {
      const auditService = {
        events: jest.fn().mockResolvedValue({ items: [], total: 0 }),
        summary: jest.fn(),
        page: jest.fn(),
        cells: jest.fn(),
        issues: jest.fn(),
        export: jest.fn(),
      };
      const controller = new AuditController(auditService as unknown as AuditService);
      const query = { stage: 'extract', limit: '50' };

      await controller.events('conv-1', user, query);

      expect(auditService.events).toHaveBeenCalledWith(user, 'conv-1', query);
    });

    it('surfaces the 403 thrown by the service', async () => {
      const forbidden = Object.assign(new Error('forbidden'), { status: 403, code: 'FORBIDDEN' });
      const auditService = {
        events: jest.fn().mockRejectedValue(forbidden),
        summary: jest.fn(),
        page: jest.fn(),
        cells: jest.fn(),
        issues: jest.fn(),
        export: jest.fn(),
      };
      const controller = new AuditController(auditService as unknown as AuditService);

      await expect(controller.events('conv-1', user, {})).rejects.toMatchObject({ status: 403 });
    });

    it('rejects when no identity could be resolved', async () => {
      const controller = new AuditController({
        events: jest.fn(),
      } as unknown as AuditService);

      expect(() => controller.events('conv-1', undefined, {})).toThrow(
        expect.objectContaining({ status: 401 }) as unknown as Error,
      );
    });
  });
});
