-- ---------------------------------------------------------------------------
-- PostgreSQL bootstrap: two logical databases, mirroring the ownership boundary
--   pdf2x_api   -> NestJS (Prisma)     : users, documents, conversions, events
--   pdf2x_conv  -> Python (SQLAlchemy) : runs, provenance, audit, output cells
-- No table is written by both services.
--
-- The role is taken from the connection user (POSTGRES_USER), never from a
-- hard-coded name: a customised deployment must not fail silently.
-- ---------------------------------------------------------------------------

SELECT 'CREATE DATABASE pdf2x_api'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'pdf2x_api')\gexec

SELECT 'CREATE DATABASE pdf2x_conv'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'pdf2x_conv')\gexec

GRANT ALL PRIVILEGES ON DATABASE pdf2x_api TO CURRENT_USER;
GRANT ALL PRIVILEGES ON DATABASE pdf2x_conv TO CURRENT_USER;
