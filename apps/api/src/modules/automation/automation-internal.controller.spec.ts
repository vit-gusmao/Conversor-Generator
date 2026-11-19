import type { Request } from 'express';

import {
  createHarness,
  OWNER,
  registerUpload,
  type Harness,
} from '../../../test/helpers/batch-harness';
import { makePdf } from '../../../test/helpers/test-utils';

import { AutomationInternalController } from './automation-internal.controller';

describe('AutomationInternalController registered options (A-10)', () => {
  let h: Harness;
  let controller: AutomationInternalController;

  beforeEach(() => {
    h = createHarness(64);
    controller = new AutomationInternalController(h.batches, h.storage, h.conversions as never);
  });

  afterEach(() => {
    h.dispose();
  });

  it('uses the options of an item registered beyond the first 200 of its batch', async () => {
    const batch = await h.batches.create(OWNER, 'source-import:imp-1');
    const bytes = makePdf(1);
    for (let index = 0; index < 250; index += 1) {
      await registerUpload(h, batch.batch_id, `srcimp:${index}`, `doc-${index}.pdf`, bytes, {
        target_formats: index === 240 ? ['txt'] : ['xlsx'],
      });
    }

    const body = bytes;
    const request = {
      headers: {
        'x-owner-id': OWNER.id,
        'x-import-id': 'imp-1',
        'x-filename': 'doc-240.pdf',
        'x-content-sha256': '',
      },
      body,
    } as unknown as Request;

    await controller.putContent(batch.batch_id, 'srcimp:240', request);

    const call = h.conversions.createStoredConversion.mock.calls[0];
    expect(call).toBeDefined();
    const options = call[2] as { target_formats?: string[] };
    expect(options.target_formats).toEqual(['txt']);
  });
});
