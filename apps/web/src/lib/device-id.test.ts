import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { getOrCreateDeviceId } from '@/lib/api';
import { desktopDeviceId } from '@/lib/platform';

const DEVICE_KEY = 'pdf2x.device_id';

function setDesktopBridge(value: unknown) {
  (window as unknown as { desktop?: unknown }).desktop = value;
}

describe('device identity (F4): desktop owner shared with the page', () => {
  beforeEach(() => {
    setDesktopBridge(undefined);
    window.localStorage.clear();
  });

  afterEach(() => {
    setDesktopBridge(undefined);
    window.localStorage.clear();
  });

  it('uses the bridge device id instead of a browser-generated one', () => {
    window.localStorage.setItem(DEVICE_KEY, 'browser-local-value');
    setDesktopBridge({ deviceId: 'desktop-owner-abcdefghijklmnopqrstu' });

    expect(desktopDeviceId()).toBe('desktop-owner-abcdefghijklmnopqrstu');
    expect(getOrCreateDeviceId()).toBe('desktop-owner-abcdefghijklmnopqrstu');
    // localStorage is neither read for the answer nor rewritten.
    expect(window.localStorage.getItem(DEVICE_KEY)).toBe('browser-local-value');
  });

  it('does not write a browser identity while running in the desktop', () => {
    setDesktopBridge({ deviceId: 'desktop-owner-abcdefghijklmnopqrstu' });
    getOrCreateDeviceId();
    expect(window.localStorage.getItem(DEVICE_KEY)).toBeNull();
  });

  it('falls back to the browser identity when there is no bridge or an empty id', () => {
    expect(desktopDeviceId()).toBeNull();
    const browserId = getOrCreateDeviceId();
    expect(browserId).toBeTruthy();
    expect(window.localStorage.getItem(DEVICE_KEY)).toBe(browserId);

    setDesktopBridge({ deviceId: '' });
    expect(desktopDeviceId()).toBeNull();
    expect(getOrCreateDeviceId()).toBe(browserId);
  });
});
