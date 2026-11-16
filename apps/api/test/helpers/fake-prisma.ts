/**
 * Minimal in-memory Prisma double.
 *
 * It implements only the surface the API actually uses (findUnique/findFirst/
 * findMany/create/update/updateMany/count/aggregate/$transaction) with the same
 * shapes, so unit tests never need a live database. Relation filters are not
 * evaluated — none of the assertions depend on them.
 */

type Row = Record<string, unknown>;
type StoreName = 'users' | 'documents' | 'conversions' | 'runs' | 'events' | 'keys' | 'outbox';

interface QueryArgs {
  where?: Row;
  data?: Row;
  include?: Row;
  select?: Row;
  orderBy?: Row;
  take?: number;
  skip?: number;
  _max?: Row;
}

const OPERATORS = new Set([
  'contains',
  'startsWith',
  'endsWith',
  'gt',
  'gte',
  'lt',
  'lte',
  'not',
  'in',
  'notIn',
]);

function isRecord(value: unknown): value is Row {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

let counter = 0;

function nextId(): string {
  counter += 1;
  return `00000000-0000-4000-8000-${String(counter).padStart(12, '0')}`;
}

function comparable(value: unknown): number | string {
  if (value instanceof Date) return value.getTime();
  if (typeof value === 'number') return value;
  return String(value);
}

export class FakePrisma {
  users: Row[] = [];
  documents: Row[] = [];
  conversions: Row[] = [];
  runs: Row[] = [];
  events: Row[] = [];
  keys: Row[] = [];
  outbox: Row[] = [];

  readonly user = this.model('users');
  readonly document = this.model('documents');
  readonly conversion = this.model('conversions');
  readonly conversionRun = this.model('runs');
  readonly conversionEvent = this.model('events');
  readonly idempotencyKey = this.model('keys');
  readonly outboxEvent = this.model('outbox');

  async $transaction<T>(fn: (tx: FakePrisma) => Promise<T>): Promise<T> {
    return fn(this);
  }

  async $queryRaw(): Promise<Array<{ ok: number }>> {
    return [{ ok: 1 }];
  }

  /** PrismaService.ping() equivalent used by /health. */
  async ping(): Promise<boolean> {
    return true;
  }

  async $connect(): Promise<void> {
    // no-op
  }

  async $disconnect(): Promise<void> {
    // no-op
  }

  /* --------------------------------- core -------------------------------- */

  private store(name: StoreName): Row[] {
    return this[name];
  }

  private model(name: StoreName) {
    return {
      findUnique: async (args: QueryArgs): Promise<Row | null> => {
        const row = this.store(name).find((candidate) => this.match(candidate, args.where, name));
        return row ? this.attach(name, row, args) : null;
      },
      findFirst: async (args: QueryArgs = {}): Promise<Row | null> => {
        const rows = this.filter(name, args);
        return rows.length > 0 ? this.attach(name, rows[0], args) : null;
      },
      findMany: async (args: QueryArgs = {}): Promise<Row[]> => {
        let rows = this.filter(name, args);
        if (args.orderBy) rows = this.sort(rows, args.orderBy);
        if (typeof args.skip === 'number') rows = rows.slice(args.skip);
        if (typeof args.take === 'number') rows = rows.slice(0, args.take);
        return rows.map((row) => this.attach(name, row, args));
      },
      create: async (args: QueryArgs): Promise<Row> => {
        const row: Row = { ...(args.data ?? {}) };
        if (!row['id']) row['id'] = nextId();
        if (!row['createdAt']) row['createdAt'] = new Date();
        if (name === 'conversions' && row['updatedAt'] === undefined) {
          row['updatedAt'] = new Date();
        }
        this.store(name).push(row);
        return this.attach(name, row, args);
      },
      update: async (args: QueryArgs): Promise<Row> => {
        const row = this.store(name).find((candidate) => this.match(candidate, args.where));
        if (!row) {
          const error = new Error('Registro não encontrado') as Error & { code: string };
          error.code = 'P2025';
          throw error;
        }
        this.apply(row, args.data ?? {});
        return this.attach(name, row, args);
      },
      updateMany: async (args: QueryArgs): Promise<{ count: number }> => {
        const rows = this.filter(name, { where: args.where });
        for (const row of rows) this.apply(row, args.data ?? {});
        return { count: rows.length };
      },
      count: async (args: QueryArgs = {}): Promise<number> => this.filter(name, args).length,
      aggregate: async (args: QueryArgs = {}): Promise<{ _max: Row }> => {
        const rows = this.filter(name, { where: args.where });
        const max: Row = {};
        for (const field of Object.keys(args._max ?? {})) {
          max[field] = rows.reduce<number | null>((acc, row) => {
            const value = row[field];
            if (typeof value !== 'number') return acc;
            return acc === null || value > acc ? value : acc;
          }, null);
        }
        return { _max: max };
      },
    };
  }

  private filter(name: StoreName, args: QueryArgs): Row[] {
    return this.store(name).filter((row) => this.match(row, args.where, name));
  }

  private apply(row: Row, data: Row): void {
    for (const [key, value] of Object.entries(data)) {
      if (isRecord(value) && 'increment' in value) {
        row[key] = Number(row[key] ?? 0) + Number(value['increment']);
        continue;
      }
      row[key] = value;
    }
    if ('updatedAt' in row) row['updatedAt'] = new Date();
  }

  private attach(name: StoreName, row: Row, args: QueryArgs): Row {
    const out: Row = { ...row };

    if (args.include) {
      if (args.include['document'] && name === 'conversions') {
        out['document'] = this.documents.find((doc) => doc['id'] === row['documentId']) ?? null;
      }
      if (args.include['runs'] && name === 'conversions') {
        const include = isRecord(args.include['runs']) ? args.include['runs'] : {};
        let runs = this.runs.filter((run) => run['conversionId'] === row['id']);
        if (include['orderBy']) runs = this.sort(runs, include['orderBy'] as Row);
        if (typeof include['take'] === 'number') runs = runs.slice(0, include['take'] as number);
        out['runs'] = runs;
      }
    }

    if (args.select) {
      const picked: Row = {};
      for (const [key, enabled] of Object.entries(args.select)) {
        if (enabled) picked[key] = out[key];
      }
      return picked;
    }

    return out;
  }

  private sort(rows: Row[], orderBy: Row): Row[] {
    const [field, direction] = Object.entries(orderBy)[0] as [string, 'asc' | 'desc'];
    return [...rows].sort((a, b) => {
      const left = comparable(a[field]);
      const right = comparable(b[field]);
      if (left === right) return 0;
      const result = left < right ? -1 : 1;
      return direction === 'desc' ? -result : result;
    });
  }

  private match(row: Row, where?: Row, name?: StoreName): boolean {
    if (!where) return true;

    return Object.entries(where).every(([key, condition]) => {
      if (condition === undefined) return true;

      if (key === 'AND' && Array.isArray(condition)) {
        return condition.every((nested) => this.match(row, nested as Row, name));
      }
      if (key === 'OR' && Array.isArray(condition)) {
        return condition.some((nested) => this.match(row, nested as Row, name));
      }
      if (key === 'NOT' && Array.isArray(condition)) {
        return !condition.some((nested) => this.match(row, nested as Row, name));
      }

      // Unset optional columns behave like NULL in the real database.
      if (condition === null) return row[key] === null || row[key] === undefined;

      if (isRecord(condition)) {
        const operatorKeys = Object.keys(condition).filter((op) => OPERATORS.has(op));
        if (operatorKeys.length === 0) {
          const allScalar = Object.values(condition).every((value) => !isRecord(value));
          if (allScalar) {
            // Composite unique, e.g. `ownerId_key: { ownerId, key }`.
            return Object.entries(condition).every(
              ([nestedKey, nestedValue]) => row[nestedKey] === nestedValue,
            );
          }
          // Relation filter, e.g. `document: { filename: { contains } }`.
          if (key === 'document' && name === 'conversions') {
            const document = this.documents.find((doc) => doc['id'] === row['documentId']);
            return document ? this.match(document, condition, 'documents') : false;
          }
          return true;
        }
        return this.matchOperators(row[key], condition);
      }

      return row[key] === condition;
    });
  }

  private matchOperators(value: unknown, condition: Row): boolean {
    return Object.entries(condition).every(([operator, expected]) => {
      switch (operator) {
        case 'contains':
          return typeof value === 'string' && value.includes(String(expected));
        case 'startsWith':
          return typeof value === 'string' && value.startsWith(String(expected));
        case 'endsWith':
          return typeof value === 'string' && value.endsWith(String(expected));
        case 'gt':
          return comparable(value) > comparable(expected);
        case 'gte':
          return comparable(value) >= comparable(expected);
        case 'lt':
          return comparable(value) < comparable(expected);
        case 'lte':
          return comparable(value) <= comparable(expected);
        case 'not':
          return value !== expected;
        case 'in':
          return Array.isArray(expected) && expected.includes(value);
        case 'notIn':
          return Array.isArray(expected) && !expected.includes(value);
        default:
          return true;
      }
    });
  }
}
