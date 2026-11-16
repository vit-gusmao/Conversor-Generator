import '@testing-library/jest-dom/vitest';
import { afterEach, beforeEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';

// jsdom lacks a few APIs used by Radix primitives and by the preview grid.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

if (!('ResizeObserver' in globalThis)) {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
}

if (!('matchMedia' in globalThis)) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }));
}

if (typeof Element !== 'undefined') {
  Element.prototype.scrollIntoView = Element.prototype.scrollIntoView || function scrollIntoView() {};
}

if (typeof HTMLElement !== 'undefined') {
  HTMLElement.prototype.hasPointerCapture =
    HTMLElement.prototype.hasPointerCapture || function hasPointerCapture() {
      return false;
    };
  HTMLElement.prototype.releasePointerCapture =
    HTMLElement.prototype.releasePointerCapture || function releasePointerCapture() {};
  HTMLElement.prototype.setPointerCapture =
    HTMLElement.prototype.setPointerCapture || function setPointerCapture() {};
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
});
