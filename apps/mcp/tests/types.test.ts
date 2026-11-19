/**
 * Wire-type drift guard: `BatchItem['kind']` must cover every kind that
 * packages/contracts emits. The assignment fails to compile if a member is
 * missing from the union, so `tsc` in `pretest` enforces the mirror.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { BatchItem } from '../src/types.js';

test('BatchItem kind includes spreadsheet, matching packages/contracts', () => {
  const kinds: BatchItem['kind'][] = ['pdf', 'image', 'spreadsheet', 'unknown'];
  assert.deepEqual(kinds, ['pdf', 'image', 'spreadsheet', 'unknown']);
});
