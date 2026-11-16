import type { ConversionStatus } from '@pdf2x/contracts';

/** Response of `POST /conversions` and `POST /conversions/:id/retry` (202). */
export interface CreateConversionResponse {
  conversion_id: string;
  status: ConversionStatus;
  message: string;
  links: Record<string, string>;
}

/** Response of `POST /conversions/:id/cancel`. */
export interface CancelConversionResponse {
  conversion_id: string;
  status: ConversionStatus;
  message: string;
  links: Record<string, string>;
}

/** Multer file shape, narrowed so unit tests do not need Express types. */
export interface UploadedPdfFile {
  buffer: Buffer;
  originalname: string;
  mimetype: string;
  size: number;
}
