import { backup, DatabaseSync } from 'node:sqlite';
import { chmodSync, existsSync, mkdirSync, renameSync, rmSync } from 'node:fs';
import path from 'node:path';
import { loadSettings } from './config';

export async function backupDatabase(source: string, destination: string): Promise<void> {
  const src = path.resolve(source);
  const dest = path.resolve(destination);
  if (!existsSync(src)) throw new Error(`Database does not exist: ${src}`);
  if (src === dest) throw new Error('Backup destination must differ from the live database');
  mkdirSync(path.dirname(dest), { recursive: true, mode: 0o700 });
  const db = new DatabaseSync(src, { readOnly: true });
  try { await backup(db, dest); } finally { db.close(); }
  chmodSync(dest, 0o600);
}

export async function restoreDatabase(source: string, destination: string): Promise<void> {
  const src = path.resolve(source);
  const dest = path.resolve(destination);
  if (!existsSync(src)) throw new Error(`Backup does not exist: ${src}`);
  if (src === dest) throw new Error('Restore source must differ from the live database');
  const check = new DatabaseSync(src, { readOnly: true });
  try {
    const integrity = check.prepare('PRAGMA integrity_check').get() as { integrity_check: string };
    if (integrity.integrity_check !== 'ok') throw new Error('Backup integrity check failed');
    const hasSchema = check.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sources'").get();
    if (!hasSchema) throw new Error('Backup does not contain a PYKASU database schema');
  } finally { check.close(); }
  mkdirSync(path.dirname(dest), { recursive: true, mode: 0o700 });
  const temporary = `${dest}.restore-tmp`;
  try {
    await backupDatabase(src, temporary);
    if (existsSync(dest)) {
      const snapshot = `${dest}.pre-restore-${new Date().toISOString().replace(/[:.]/g, '-')}`;
      await backupDatabase(dest, snapshot);
    }
    renameSync(temporary, dest);
    for (const suffix of ['-wal', '-shm']) {
      const sidecar = `${dest}${suffix}`;
      if (existsSync(sidecar)) rmSync(sidecar);
    }
    chmodSync(dest, 0o600);
  } finally {
    if (existsSync(temporary)) rmSync(temporary);
  }
}

async function main(): Promise<void> {
  const [command, file] = process.argv.slice(2);
  if (!['backup', 'restore'].includes(command) || !file) {
    throw new Error('Usage: node dist/backup.js backup <destination> | restore <backup-file>');
  }
  const live = loadSettings().databasePath;
  if (command === 'backup') {
    await backupDatabase(live, file);
    process.stdout.write(`Backup written to ${path.resolve(file)}\n`);
  } else {
    await restoreDatabase(file, live);
    process.stdout.write(`Database restored from ${path.resolve(file)}\n`);
  }
}

if (require.main === module) void main().catch(error => { process.stderr.write(`${error instanceof Error ? error.message : 'Backup operation failed'}\n`); process.exitCode = 1; });
