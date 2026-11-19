/**
 * One Idempotency-Key per user intent (e.g. "run now" for a schedule).
 * The same key is reused on retries of that intent and released only after
 * success, so a second click starts a new intent instead of replaying a
 * fresh `Date.now()` key that the server cannot dedupe.
 */
export interface IntentKeys {
  keyFor(scope: string): string;
  settle(scope: string): void;
}

export function createIntentKeys(prefix: () => string = () => String(Date.now())): IntentKeys {
  const pending = new Map<string, string>();
  return {
    keyFor(scope) {
      let key = pending.get(scope);
      if (!key) {
        key = `${scope}:${prefix()}`;
        pending.set(scope, key);
      }
      return key;
    },
    settle(scope) {
      pending.delete(scope);
    },
  };
}
