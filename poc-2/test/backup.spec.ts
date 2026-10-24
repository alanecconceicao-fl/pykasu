import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { backupDatabase, restoreDatabase } from '../src/backup';

describe('database backup', () => {
  it('creates a consistent backup and restores it after validation', async () => {
    const dir = mkdtempSync(path.join(os.tmpdir(), 'pykasu-backup-'));
    const live = path.join(dir, 'live.sqlite3');
    const backup = path.join(dir, 'backup.sqlite3');
    try {
      let db = new DatabaseSync(live);
      db.exec("CREATE TABLE sources(id INTEGER PRIMARY KEY,name TEXT); INSERT INTO sources(name) VALUES ('Before');");
      db.close();
      await backupDatabase(live, backup);
      db = new DatabaseSync(live);
      db.exec("INSERT INTO sources(name) VALUES ('After');");
      db.close();
      await restoreDatabase(backup, live);
      db = new DatabaseSync(live, { readOnly: true });
      expect((db.prepare('SELECT name FROM sources').all() as Array<{ name: string }>).map(row => row.name)).toEqual(['Before']);
      db.close();
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });
});
