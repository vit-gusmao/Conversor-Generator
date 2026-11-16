import type { AuthenticatedUser } from '../../common/request-context';
import { AppConfigService } from '../../config/app-config.service';
import { PrismaService } from '../../database/prisma.service';
import { FileValidationService } from '../../upload/file-validation.service';
import type { StorageService } from '../../upload/storage.service';
import { ConversionEventsService } from '../realtime/conversion-events.service';
import { FakePrisma } from '../../../test/helpers/fake-prisma';
import { captureApiErrorAsync, makePdf, uuidPattern } from '../../../test/helpers/test-utils';

import { ConverterClient } from './converter.client';
import { ConversionsController } from './conversions.controller';
import { ConversionsService } from './conversions.service';
import type { ListConversionsQueryDto } from '../../common/dto/pagination.dto';
import { OutboxService } from './outbox.service';

describe('ConversionsService', () => {
  let prisma: FakePrisma;
  let service: ConversionsService;
  let converter: {
    result: jest.Mock;
    preview: jest.Mock;
    download: jest.Mock;
    cancel: jest.Mock;
  };
  let storage: StorageService;
  let events: { record: jest.Mock };
  let outbox: { record: jest.Mock; dispatch: jest.Mock };

  const user: AuthenticatedUser = {
    id: 'user-owner-1',
    email: null,
    name: null,
    role: 'owner',
    isAnonymous: true,
    createdAt: new Date().toISOString(),
    deviceId: 'device-owner-1',
  };

  const query = (extra: Partial<ListConversionsQueryDto> = {}): ListConversionsQueryDto =>
    ({ page: 1, page_size: 20, ...extra }) as ListConversionsQueryDto;

  const makeFile = (pages = 2, name = 'holerite.pdf') => ({
    buffer: makePdf(pages),
    originalname: name,
    mimetype: 'application/pdf',
    size: makePdf(pages).length,
  });

  beforeEach(() => {
    prisma = new FakePrisma();
    converter = {
      result: jest.fn(),
      preview: jest.fn(),
      download: jest.fn(),
      cancel: jest.fn().mockResolvedValue(undefined),
    };
    storage = {
      backend: 'local',
      put: jest.fn().mockResolvedValue({ key: 'aa/hash.pdf', size: 10 }),
      get: jest.fn(),
      delete: jest.fn().mockResolvedValue(undefined),
      exists: jest.fn(),
    } as unknown as StorageService;
    events = { record: jest.fn().mockResolvedValue(null) };
    outbox = {
      record: jest.fn().mockResolvedValue({ id: 'outbox-1' }),
      dispatch: jest.fn().mockResolvedValue(undefined),
    };

    service = new ConversionsService(
      prisma as unknown as PrismaService,
      new FileValidationService({ maxBytes: 10 * 1024 * 1024, maxPages: 500 }),
      storage,
      converter as unknown as ConverterClient,
      { uploadMaxPages: 500 } as unknown as AppConfigService,
      events as unknown as ConversionEventsService,
      outbox as unknown as OutboxService,
    );
  });

  it('creates a conversion and returns the 202 body shape', async () => {
    const response = await service.create(user, makeFile());

    expect(response.conversion_id).toMatch(uuidPattern);
    expect(response.status).toBe('queued');
    expect(response.message).toBe('Conversão recebida e enfileirada.');
    expect(Object.keys(response.links)).toEqual(
      expect.arrayContaining(['status', 'events', 'result', 'preview', 'download', 'audit']),
    );
    expect(response.links['events']).toBe(`/api/v1/conversions/${response.conversion_id}/events`);

    // persistence
    expect(prisma.documents).toHaveLength(1);
    expect(prisma.conversions[0]['status']).toBe('queued');
    expect(prisma.conversions[0]['activeRunId']).toBe(prisma.runs[0]['id']);
    expect(prisma.runs[0]).toMatchObject({ attempt: 1, status: 'queued' });
    expect(prisma.events).toHaveLength(0); // events service is mocked
    expect(events.record).toHaveBeenCalledWith(
      response.conversion_id,
      expect.objectContaining({ status: 'queued', type: 'status' }),
    );

    // enqueue is durable-first: outbox row + fire-and-forget dispatch
    expect(outbox.record).toHaveBeenCalledWith(
      'conversion.enqueue',
      expect.objectContaining({ conversionId: response.conversion_id }),
      expect.anything(),
    );
    expect(outbox.dispatch).toHaveBeenCalledWith('outbox-1');
    expect(converter.cancel).not.toHaveBeenCalled();
  });

  it('is declared as HTTP 202 on the controller', () => {
    const httpCode = Reflect.getMetadata(
      '__httpCode__',
      ConversionsController.prototype.create,
    ) as number | undefined;
    expect(httpCode).toBe(202);
  });

  it('replays an idempotent request with the same conversion', async () => {
    const first = await service.create(user, makeFile(), 'chave-unica-1');
    const second = await service.create(user, makeFile(), 'chave-unica-1');

    expect(second.conversion_id).toBe(first.conversion_id);
    expect(second.status).toBe('queued');
    expect(prisma.conversions).toHaveLength(1);
    expect(prisma.keys).toHaveLength(1);
  });

  it('rejects an idempotency key reused with different content', async () => {
    await service.create(user, makeFile(2), 'chave-unica-2');

    const error = await captureApiErrorAsync(() =>
      service.create(user, makeFile(3), 'chave-unica-2'),
    );

    expect(error.status).toBe(409);
    expect(error.code).toBe('IDEMPOTENT_REPLAY');
  });

  it('rejects a non-PDF upload before touching storage', async () => {
    const error = await captureApiErrorAsync(() =>
      service.create(user, {
        buffer: Buffer.from('not a pdf'),
        originalname: 'arquivo.pdf',
        mimetype: 'application/pdf',
        size: 9,
      }),
    );

    expect(error.code).toBe('INVALID_FILE');
    expect(storage.put).not.toHaveBeenCalled();
    expect(prisma.conversions).toHaveLength(0);
  });

  it('enforces ownership with 403', async () => {
    const created = await service.create(user, makeFile());
    const intruder: AuthenticatedUser = { ...user, id: 'user-intruder' };

    const error = await captureApiErrorAsync(() =>
      service.getDetailResponse(intruder, created.conversion_id),
    );

    expect(error.status).toBe(403);
    expect(error.code).toBe('FORBIDDEN');
  });

  it('returns 404 for an unknown conversion', async () => {
    const error = await captureApiErrorAsync(() =>
      service.getDetailResponse(user, '00000000-0000-4000-8000-999999999999'),
    );

    expect(error.status).toBe(404);
    expect(error.code).toBe('NOT_FOUND');
  });

  it('creates a new run on retry after a terminal state', async () => {
    const created = await service.create(user, makeFile());
    prisma.conversions[0]['status'] = 'failed';
    prisma.conversions[0]['errorCode'] = 'ENGINE_FAILURE';

    const response = await service.retry(user, created.conversion_id);

    expect(response.conversion_id).toBe(created.conversion_id);
    expect(response.status).toBe('queued');
    expect(prisma.runs).toHaveLength(2);
    expect(prisma.runs[1]).toMatchObject({ attempt: 2, status: 'queued' });
    expect(prisma.conversions[0]['activeRunId']).toBe(prisma.runs[1]['id']);
    expect(prisma.conversions[0]['errorCode']).toBeNull();
    expect(outbox.record).toHaveBeenCalledTimes(2);
  });

  it('refuses retry while a run is still active', async () => {
    const created = await service.create(user, makeFile());

    const error = await captureApiErrorAsync(() => service.retry(user, created.conversion_id));

    expect(error.status).toBe(409);
    expect(error.code).toBe('CONVERSION_BUSY');
    expect(prisma.runs).toHaveLength(1);
  });

  it('refuses cancel on a terminal conversion', async () => {
    const created = await service.create(user, makeFile());
    prisma.conversions[0]['status'] = 'completed';

    const error = await captureApiErrorAsync(() => service.cancel(user, created.conversion_id));

    expect(error.status).toBe(409);
    expect(error.code).toBe('ALREADY_TERMINAL');
  });

  it('cancels an active conversion and marks the run finished', async () => {
    const created = await service.create(user, makeFile());

    const response = await service.cancel(user, created.conversion_id);

    expect(response.status).toBe('cancelled');
    expect(converter.cancel).toHaveBeenCalledWith(prisma.runs[0]['id']);
    expect(prisma.conversions[0]['status']).toBe('cancelled');
    expect(prisma.runs[0]['finishedAt']).toBeInstanceOf(Date);
  });

  it('blocks download until the conversion is ready', async () => {
    const created = await service.create(user, makeFile());

    const error = await captureApiErrorAsync(() =>
      service.getDownload(user, created.conversion_id),
    );

    expect(error.status).toBe(409);
    expect(error.code).toBe('NOT_READY');
    expect(converter.download).not.toHaveBeenCalled();
  });

  it('proxies download for a completed conversion', async () => {
    const created = await service.create(user, makeFile());
    prisma.conversions[0]['status'] = 'completed';
    converter.download.mockResolvedValue({ stream: null, contentType: 'x', contentLength: 1, filename: 'a.xlsx' });

    await service.getDownload(user, created.conversion_id);

    expect(converter.download).toHaveBeenCalledWith(prisma.runs[0]['id']);
  });

  it('soft deletes and purges the PDF once terminal', async () => {
    const created = await service.create(user, makeFile());
    prisma.conversions[0]['status'] = 'completed';

    await service.remove(user, created.conversion_id);

    expect(prisma.conversions[0]['deletedAt']).toBeInstanceOf(Date);
    expect(storage.delete).toHaveBeenCalled();

    const error = await captureApiErrorAsync(() =>
      service.getDetailResponse(user, created.conversion_id),
    );
    expect(error.status).toBe(404);
  });

  it('lists only the owner conversions with filters', async () => {
    await service.create(user, makeFile(2, 'folha-janeiro.pdf'));
    await service.create(user, makeFile(3, 'folha-fevereiro.pdf'));
    prisma.conversions[0]['status'] = 'completed';

    const all = await service.list(user, query());
    expect(all.total).toBe(2);
    expect(all.items).toHaveLength(2);
    expect(all.page).toBe(1);
    expect(all.page_size).toBe(20);

    const completed = await service.list(user, query({ status: 'completed' }));
    expect(completed.total).toBe(1);

    const search = await service.list(user, query({ q: 'fevereiro' }));
    expect(search.total).toBe(1);
    expect(search.items[0].filename).toBe('folha-fevereiro.pdf');
  });
});
