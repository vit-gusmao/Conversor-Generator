/** Test helpers shared by the unit specs. */
import { ApiError } from '../../src/common/errors/api-error';

/** Returns the ApiError thrown by `fn`, failing the test if nothing throws. */
export function captureApiError(fn: () => unknown): ApiError {
  try {
    fn();
  } catch (error) {
    if (error instanceof ApiError) return error;
    throw new Error(`Esperava ApiError, recebeu: ${String(error)}`);
  }
  throw new Error('Esperava que uma exceção fosse lançada');
}

export async function captureApiErrorAsync(
  fn: () => Promise<unknown>,
): Promise<ApiError> {
  try {
    await fn();
  } catch (error) {
    if (error instanceof ApiError) return error;
    throw new Error(`Esperava ApiError, recebeu: ${String(error)}`);
  }
  throw new Error('Esperava que uma exceção fosse lançada');
}

/** Builds a syntactically valid PDF with `pages` page objects. */
export function makePdf(pages = 2): Buffer {
  const objects = Array.from(
    { length: pages },
    (_, index) => `${index + 3} 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >>\nendobj\n`,
  ).join('');

  return Buffer.from(
    `%PDF-1.7\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n` +
      `2 0 obj\n<< /Type /Pages /Count ${pages} /Kids [] >>\nendobj\n` +
      objects +
      `trailer\n<< /Root 1 0 R >>\nstartxref\n0\n%%EOF\n`,
    'latin1',
  );
}

export const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
