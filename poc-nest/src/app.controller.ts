import { Body, Controller, Get, HttpException, HttpStatus, Param, Patch, Post, Query, Res } from '@nestjs/common';
import { FastifyReply } from 'fastify';
import { z } from 'zod';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { DatabaseService } from './database.service';
import { RefreshService } from './refresh.service';
import { SafeHttpService } from './http.service';
import { normalizeText } from './util';

const sourceSchema = z.object({
  name: z.string().trim().min(1).max(120),
  feed_url: z.string().url(),
  enabled: z.boolean().optional().default(true),
  extract_articles: z.boolean().optional().default(true)
}).strict();

@Controller()
export class AppController {
  constructor(private readonly database: DatabaseService, private readonly refresh: RefreshService, private readonly http: SafeHttpService) {}

  @Get('/')
  root(@Res() reply: FastifyReply): void { void reply.redirect('/updates'); }

  @Get('/updates')
  async updatesPage(@Res() reply: FastifyReply): Promise<void> {
    this.refresh.startOrJoin();
    await this.page(reply, 'updates.html');
  }

  @Get('/sources')
  async sourcesPage(@Res() reply: FastifyReply): Promise<void> {
    this.refresh.startOrJoin();
    await this.page(reply, 'sources.html');
  }

  @Get('/api/state')
  state(@Query('window') rawWindow?: string, @Query('source') rawSource?: string, @Query('q') rawSearch?: string, @Query('since') rawSince?: string, @Query('ack') rawAck?: string): Record<string, unknown> {
    const window = rawWindow === '7d' ? '7d' : '24h';
    const sourceId = rawSource && /^\d+$/.test(rawSource) ? Number(rawSource) : undefined;
    const parsedSince = rawSince && /^\d+$/.test(rawSince) ? Number(rawSince) : undefined;
    const result = this.database.queryItems(this.database.db, { window, sourceId, search: normalizeText(rawSearch).slice(0, 160) || undefined, acknowledge: rawAck === '1', newSince: parsedSince });
    return { ...result, sources: this.database.listSources(this.database.db), refresh: this.refresh.status(), limits: { max_enabled_sources: this.database.settings.maxEnabledSources } };
  }

  @Get('/api/refresh-status')
  refreshStatus(): object { return this.refresh.status(); }

  @Get('/health')
  health(): Record<string, boolean> { return { ok: true }; }

  @Post('/api/refresh')
  startRefresh(): object {
    this.refresh.startOrJoin();
    return this.refresh.status();
  }

  @Post('/api/sources')
  async createSource(@Body() raw: unknown): Promise<Record<string, unknown>> {
    const parsed = sourceSchema.safeParse(raw);
    if (!parsed.success) throw new HttpException('Provide a source name and public RSS/Atom URL.', HttpStatus.BAD_REQUEST);
    const feedUrl = await this.http.validateDestination(parsed.data.feed_url);
    try {
      const id = this.database.createSource(this.database.db, { name: parsed.data.name, feedUrl, enabled: parsed.data.enabled, extractArticles: parsed.data.extract_articles });
      return { source: this.database.getSource(this.database.db, id) };
    } catch (error) { throw this.asHttpError(error); }
  }

  @Patch('/api/sources/:id')
  async updateSource(@Param('id') rawId: string, @Body() raw: unknown): Promise<Record<string, unknown>> {
    const id = Number(rawId);
    if (!Number.isSafeInteger(id) || id < 1) throw new HttpException('Source not found', HttpStatus.NOT_FOUND);
    const parsed = sourceSchema.safeParse(raw);
    if (!parsed.success) throw new HttpException('Provide a source name and public RSS/Atom URL.', HttpStatus.BAD_REQUEST);
    const feedUrl = await this.http.validateDestination(parsed.data.feed_url);
    try {
      this.database.updateSource(this.database.db, id, { name: parsed.data.name, feedUrl, enabled: parsed.data.enabled, extractArticles: parsed.data.extract_articles });
      return { source: this.database.getSource(this.database.db, id) };
    } catch (error) { throw this.asHttpError(error); }
  }

  @Get('/assets/:name')
  async asset(@Param('name') name: string, @Res() reply: FastifyReply): Promise<void> {
    if (!['app.js', 'style.css'].includes(name)) throw new HttpException('Not found', HttpStatus.NOT_FOUND);
    await this.page(reply, name, name.endsWith('.js') ? 'text/javascript; charset=utf-8' : 'text/css; charset=utf-8');
  }

  private async page(reply: FastifyReply, filename: string, contentType = 'text/html; charset=utf-8'): Promise<void> {
    const here = path.resolve(process.cwd(), 'public', filename);
    try {
      const body = (await readFile(here, 'utf8')).replaceAll('__BASE_PATH__', process.env.VERCEL ? '/poc-nest' : '');
      reply.header('content-type', contentType).header('cache-control', 'no-store, max-age=0').send(body);
    } catch {
      const distPublic = path.resolve(__dirname, '../public', filename);
      try {
        const body = (await readFile(distPublic, 'utf8')).replaceAll('__BASE_PATH__', process.env.VERCEL ? '/poc-nest' : '');
        reply.header('content-type', contentType).header('cache-control', 'no-store').send(body);
      }
      catch { throw new HttpException('Page asset not found', HttpStatus.NOT_FOUND); }
    }
  }

  private asHttpError(error: unknown): HttpException {
    const message = error instanceof Error ? error.message : 'Request failed';
    const status = message === 'Source not found' ? HttpStatus.NOT_FOUND : HttpStatus.BAD_REQUEST;
    return new HttpException(message, status);
  }
}
