/**
 * Deterministic environment for the e2e suites.
 *
 * Runs before any module is imported, so `ConfigModule.forRoot()` sees these
 * values. Small parts let a two-kilobyte fixture exercise multi-part assembly.
 */
process.env.UPLOAD_PART_BYTES = process.env.UPLOAD_PART_BYTES ?? '65536';
process.env.BATCH_UPLOAD_ENABLED = process.env.BATCH_UPLOAD_ENABLED ?? 'true';
