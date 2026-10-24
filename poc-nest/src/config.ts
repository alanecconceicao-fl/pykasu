import path from 'node:path';

export interface Settings {
  bind: string;
  port: number;
  dataDir: string;
  databasePath: string;
  groqApiKey?: string;
  groqZeroDataRetention: boolean;
  maxEnabledSources: number;
  entriesPerFeed: number;
  summariesPerRefresh: number;
  summariesPerDay: number;
  modelMinIntervalSeconds: number;
  responseSizeLimit: number;
  requestDeadlineMs: number;
  maxRedirects: number;
  promptCharacterLimit: number;
  modelCompletionLimit: number;
}

export const SETTINGS = Symbol('PYKASU_SETTINGS');
export const REFRESH_WORKER_FACTORY = Symbol('PYKASU_REFRESH_WORKER_FACTORY');

function positiveInt(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const parsed = Number(raw);
  if (!Number.isInteger(parsed) || parsed < 1) throw new Error(`${name} must be a positive integer`);
  return parsed;
}

export function loadSettings(): Settings {
  const defaultDataDir = process.env.VERCEL ? '/tmp/pykasu-nest' : './data';
  const dataDir = path.resolve(process.env.PYKASU_DATA_DIR || defaultDataDir);
  const port = positiveInt('PORT', positiveInt('PYKASU_PORT', 8001));
  if (port > 65535) throw new Error('PYKASU_PORT must be between 1 and 65535');
  return {
    bind: process.env.PYKASU_BIND || (process.env.VERCEL ? '0.0.0.0' : '127.0.0.1'),
    port,
    dataDir,
    databasePath: path.join(dataDir, 'pykasu.sqlite3'),
    groqApiKey: process.env.PYKASU_GROQ_API_KEY || process.env.GROQ_API_KEY || undefined,
    groqZeroDataRetention: /^(1|true|yes|enabled)$/i.test(process.env.PYKASU_GROQ_ZERO_DATA_RETENTION || ''),
    maxEnabledSources: 10,
    entriesPerFeed: 20,
    summariesPerRefresh: 10,
    summariesPerDay: 30,
    modelMinIntervalSeconds: 65,
    responseSizeLimit: 2 * 1024 * 1024,
    requestDeadlineMs: 20_000,
    maxRedirects: 3,
    promptCharacterLimit: 16_000,
    modelCompletionLimit: 2_048
  };
}
