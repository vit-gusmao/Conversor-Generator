/**
 * SHA-256 helpers built on Web Crypto.
 *
 * `crypto.subtle` is only available in secure browser contexts (and is not
 * implemented by jsdom), so the unit tests mock this module. Callers should
 * treat failures as recoverable and surface a readable message.
 */

function toHex(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let hex = '';
  for (const byte of bytes) {
    hex += byte.toString(16).padStart(2, '0');
  }
  return hex;
}

function subtleCrypto(): SubtleCrypto | null {
  try {
    return globalThis.crypto?.subtle ?? null;
  } catch {
    return null;
  }
}

function isBlob(value: ArrayBuffer | Uint8Array | Blob): value is Blob {
  return typeof Blob !== 'undefined' && value instanceof Blob;
}

/** Hex SHA-256 of an ArrayBuffer, a byte view or a Blob. */
export async function sha256Hex(data: ArrayBuffer | Uint8Array | Blob): Promise<string> {
  const subtle = subtleCrypto();
  if (!subtle) {
    throw new Error(
      'WEB_CRYPTO_UNAVAILABLE: este navegador não oferece crypto.subtle para calcular o SHA-256.',
    );
  }

  let bytes: Uint8Array;
  if (isBlob(data)) {
    bytes = new Uint8Array(await data.arrayBuffer());
  } else if (data instanceof Uint8Array) {
    bytes = data;
  } else {
    bytes = new Uint8Array(data);
  }

  // `digest` must not see extra bytes from a subarray view. When the view spans
  // the whole buffer we pass it through without an extra copy.
  const wholeView = bytes.byteOffset === 0 && bytes.byteLength === bytes.buffer.byteLength;
  const buffer = (wholeView
    ? bytes.buffer
    : bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)) as ArrayBuffer;
  const digest = await subtle.digest('SHA-256', buffer);
  return toHex(digest);
}
