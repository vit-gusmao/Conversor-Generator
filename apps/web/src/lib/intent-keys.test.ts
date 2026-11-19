import { describe, expect, it } from 'vitest';
import { createIntentKeys } from '@/lib/intent-keys';

describe('createIntentKeys', () => {
  it('reuses one key for the same intent until it settles', () => {
    let tick = 0;
    const keys = createIntentKeys(() => String(++tick));
    const first = keys.keyFor('sch_1');
    expect(keys.keyFor('sch_1')).toBe(first);
    expect(keys.keyFor('sch_1')).toBe(first);
  });

  it('issues a new key for the next intent after success', () => {
    let tick = 0;
    const keys = createIntentKeys(() => String(++tick));
    const first = keys.keyFor('sch_1');
    keys.settle('sch_1');
    expect(keys.keyFor('sch_1')).not.toBe(first);
  });

  it('keeps scopes independent', () => {
    const keys = createIntentKeys(() => 'fixed');
    expect(keys.keyFor('a')).toBe('a:fixed');
    expect(keys.keyFor('b')).toBe('b:fixed');
  });
});
