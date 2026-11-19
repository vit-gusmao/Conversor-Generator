import { DEV_INTERNAL_TOKEN, DEV_JWT_SECRET, configuration } from './configuration';

const MANAGED = [
  'NODE_ENV',
  'JWT_SECRET',
  'CONVERTER_INTERNAL_TOKEN',
  'API_DATABASE_URL',
  'API_POSTGRES_URL',
] as const;

describe('configuration', () => {
  const saved: Record<string, string | undefined> = {};

  beforeEach(() => {
    for (const name of MANAGED) {
      saved[name] = process.env[name];
      delete process.env[name];
    }
    // Quiet by default; the warning tests switch to another value explicitly.
    process.env.NODE_ENV = 'test';
  });

  afterEach(() => {
    for (const name of MANAGED) {
      if (saved[name] === undefined) delete process.env[name];
      else process.env[name] = saved[name];
    }
    jest.restoreAllMocks();
  });

  describe('development secrets (A-02)', () => {
    it('refuses to start in production with the dev JWT secret', () => {
      process.env.NODE_ENV = 'production';
      process.env.CONVERTER_INTERNAL_TOKEN = 'token-real';
      process.env.JWT_SECRET = DEV_JWT_SECRET;

      expect(() => configuration()).toThrow(/JWT_SECRET/);
    });

    it('refuses to start in production with the dev internal token', () => {
      process.env.NODE_ENV = 'production';
      process.env.JWT_SECRET = 'jwt-real';
      process.env.CONVERTER_INTERNAL_TOKEN = DEV_INTERNAL_TOKEN;

      expect(() => configuration()).toThrow(/CONVERTER_INTERNAL_TOKEN/);
    });

    it('starts in production when both secrets are set', () => {
      process.env.NODE_ENV = 'production';
      process.env.JWT_SECRET = 'jwt-real';
      process.env.CONVERTER_INTERNAL_TOKEN = 'token-real';

      const config = configuration();

      expect(config.api.jwtSecret).toBe('jwt-real');
      expect(config.converter.internalToken).toBe('token-real');
    });

    it('warns outside production and tests when a dev secret is in use', () => {
      process.env.NODE_ENV = 'development';
      const warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);

      const config = configuration();

      expect(config.api.jwtSecret).toBe(DEV_JWT_SECRET);
      expect(warn).toHaveBeenCalledWith(expect.stringContaining('JWT_SECRET'));
    });

    it('stays quiet under NODE_ENV=test', () => {
      process.env.NODE_ENV = 'test';
      const warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);

      expect(() => configuration()).not.toThrow();
      expect(warn).not.toHaveBeenCalled();
    });
  });

  describe('database URL (A-15)', () => {
    it('defaults to the SQLite file when neither variable is set', () => {
      expect(configuration().api.databaseUrl).toBe('file:./data/api.db');
    });

    it('uses API_POSTGRES_URL when API_DATABASE_URL is unset', () => {
      process.env.API_POSTGRES_URL = 'postgresql://pdf2x:pdf2x@db:5432/pdf2x_api?schema=api';

      expect(configuration().api.databaseUrl).toBe(
        'postgresql://pdf2x:pdf2x@db:5432/pdf2x_api?schema=api',
      );
    });

    it('prefers API_DATABASE_URL when both are set', () => {
      process.env.API_DATABASE_URL = 'file:./data/other.db';
      process.env.API_POSTGRES_URL = 'postgresql://pdf2x:pdf2x@db:5432/pdf2x_api';

      expect(configuration().api.databaseUrl).toBe('file:./data/other.db');
    });
  });
});
