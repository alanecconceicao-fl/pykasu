import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { DatabaseService } from '../src/database.service';
import { fingerprint, nowIso } from '../src/util';

describe('SQLite persistence', () => {
  let dir: string;
  let database: DatabaseService;
  beforeEach(() => {
    dir = mkdtempSync(path.join(os.tmpdir(), 'pykasu-db-'));
    database = new DatabaseService({ dataDir: dir, databasePath: path.join(dir, 'pykasu.sqlite3'), maxEnabledSources: 10, summariesPerDay: 30, modelMinIntervalSeconds: 65 } as any);
  });
  afterEach(() => { database.onModuleDestroy(); rmSync(dir, { recursive: true, force: true }); });

  it('deduplicates one normalized URL while preserving every source association', () => {
    const one = database.createSource(database.db, { name: 'One', feedUrl: 'https://one.example/feed', enabled: true, extractArticles: true });
    const two = database.createSource(database.db, { name: 'Two', feedUrl: 'https://two.example/feed', enabled: true, extractArticles: false });
    const input = { url: 'https://publisher.example/story', title: 'Report', publishedAt: nowIso(), excerpt: 'A sufficiently detailed feed excerpt for this source item.', fingerprint: fingerprint('v1') };
    const first = database.insertOrUpdateItem(database.db, one, input);
    const second = database.insertOrUpdateItem(database.db, two, input);
    expect(first.inserted).toBe(true);
    expect(second.inserted).toBe(false);
    const associations = database.db.prepare('SELECT count(*) n FROM item_sources WHERE item_id=?').get(first.id) as { n: number };
    expect(associations.n).toBe(2);
  });

  it('reopens a changed item version as pending and preserves visit sequence behavior', () => {
    const sourceId = database.createSource(database.db, { name: 'One', feedUrl: 'https://one.example/feed', enabled: true, extractArticles: true });
    const first = database.insertOrUpdateItem(database.db, sourceId, { url: 'https://publisher.example/story', title: 'Report', publishedAt: nowIso(), excerpt: '', fingerprint: 'v1' });
    database.setSummary(database.db, first.id, 'input-v1', 'A supported report.', 'ready', [{ paragraph_id: 1, quote: 'supported report' }]);
    database.insertOrUpdateItem(database.db, sourceId, { url: 'https://publisher.example/story', title: 'Updated report', publishedAt: nowIso(), excerpt: '', fingerprint: 'v2' });
    const item = database.db.prepare('SELECT summary,summary_status FROM items WHERE id=?').get(first.id) as any;
    expect(item.summary).toBeNull();
    expect(item.summary_status).toBe('pending');
    const firstView = database.queryItems(database.db, { window: '7d', acknowledge: true });
    expect(firstView.items[0].is_new).toBe(true);
    expect(firstView.items[0]).not.toHaveProperty('feed_excerpt');
    expect(firstView.items[0]).not.toHaveProperty('supporting_excerpts');
    const nextViewSameVisit = database.queryItems(database.db, { window: '7d', acknowledge: true, newSince: firstView.acknowledgedSequence });
    expect(nextViewSameVisit.items[0].is_new).toBe(true);
    const laterVisit = database.queryItems(database.db, { window: '7d', acknowledge: true });
    expect(laterVisit.items[0].is_new).toBe(false);
  });

  it('keeps a source freshness timestamp on failure and saves refresh progress', () => {
    const sourceId = database.createSource(database.db, { name: 'One', feedUrl: 'https://one.example/feed', enabled: true, extractArticles: true });
    database.sourceFetchSuccess(database.db, sourceId, 'etag', null);
    const lastSuccess = (database.getSource(database.db, sourceId) as any).last_success_at;
    database.sourceFetchFailure(database.db, sourceId, 'HTTP 502');
    const source = database.getSource(database.db, sourceId) as any;
    expect(source.last_success_at).toBe(lastSuccess);
    expect(source.last_error).toBe('HTTP 502');
  });
});
