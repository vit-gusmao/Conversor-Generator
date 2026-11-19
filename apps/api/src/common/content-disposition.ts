/**
 * `Content-Disposition` for attachment downloads: an ASCII fallback without
 * quotes or backslashes, plus the UTF-8 name. A name supplied by the engine
 * never reaches the header raw.
 */
export function contentDisposition(filename: string): string {
  const ascii = filename.replace(/[^\x20-\x7e]/g, '_').replace(/["\\]/g, '_');
  return `attachment; filename="${ascii}"; filename*=UTF-8''${encodeURIComponent(filename)}`;
}
