import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { Test } from '@nestjs/testing';
import { FastifyAdapter, NestFastifyApplication } from '@nestjs/platform-fastify';
import { AppModule } from '../src/app.module';
import { SafeHttpService } from '../src/http.service';
import { RefreshService } from '../src/refresh.service';

describe('NestJS HTTP application', () => {
  let app: NestFastifyApplication;
  let dir: string;
  beforeAll(async () => {
    dir = mkdtempSync(path.join(os.tmpdir(), 'pykasu-app-'));
    process.env.PYKASU_DATA_DIR = dir;
    process.env.PYKASU_PORT = '18001';
    const module = await Test.createTestingModule({ imports: [AppModule] })
      .overrideProvider(SafeHttpService).useValue({ validateDestination: async (url: string) => url })
      .overrideProvider(RefreshService).useValue({ startOrJoin: () => null, status: () => ({ active: false, errors: [], sources_total: 0, sources_done: 0, headlines_saved: 0, summaries_attempted: 0, max_discovery_sequence: 0 }) })
      .compile();
    app = module.createNestApplication<NestFastifyApplication>(new FastifyAdapter());
    await app.init();
    await app.getHttpAdapter().getInstance().ready();
  });
  afterAll(async () => {
    await app.close();
    delete process.env.PYKASU_DATA_DIR;
    delete process.env.PYKASU_PORT;
    rmSync(dir, { recursive: true, force: true });
  });

  it('serves health and the Updates and Sources pages', async () => {
    const server = app.getHttpAdapter().getInstance();
    const health = await server.inject({ method: 'GET', url: '/health' });
    expect(health.statusCode).toBe(200);
    expect(health.json()).toEqual({ ok: true });
    const updates = await server.inject({ method: 'GET', url: '/updates' });
    expect(updates.statusCode).toBe(200);
    expect(updates.headers['content-type']).toMatch(/text\/html/);
    const sources = await server.inject({ method: 'GET', url: '/sources' });
    expect(sources.statusCode).toBe(200);
    expect(sources.headers['content-type']).toMatch(/text\/html/);
  });

  it('manages sources and returns saved state without beginning another refresh', async () => {
    const server = app.getHttpAdapter().getInstance();
    const created = await server.inject({ method: 'POST', url: '/api/sources', payload: { name: 'Example', feed_url: 'https://example.org/rss', enabled: true, extract_articles: true } });
    expect(created.statusCode).toBe(201);
    expect(created.json().source.name).toBe('Example');
    const state = await server.inject({ method: 'GET', url: '/api/state?window=24h' });
    expect(state.statusCode).toBe(200);
    expect(state.json().sources).toHaveLength(1);
    expect(state.json().refresh.active).toBe(false);
    const patched = await server.inject({ method: 'PATCH', url: `/api/sources/${created.json().source.id}`, payload: { name: 'Example', feed_url: 'https://example.org/rss', enabled: false, extract_articles: true } });
    expect(patched.statusCode).toBe(200);
  });
});
