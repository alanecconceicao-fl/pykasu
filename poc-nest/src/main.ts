import 'reflect-metadata';
import { NestFactory } from '@nestjs/core';
import { FastifyAdapter, NestFastifyApplication } from '@nestjs/platform-fastify';
import { AppModule } from './app.module';
import { loadSettings } from './config';

async function bootstrap(): Promise<void> {
  const settings = loadSettings();
  const adapter = new FastifyAdapter({ bodyLimit: 64 * 1024, trustProxy: false });
  const app = await NestFactory.create<NestFastifyApplication>(AppModule, adapter, { bufferLogs: true });
  if (process.env.VERCEL) app.setGlobalPrefix('poc-nest');
  app.enableShutdownHooks();
  const fastify = adapter.getInstance();
  fastify.addHook('onSend', async (_request, reply, payload) => {
    reply.header('x-content-type-options', 'nosniff');
    reply.header('referrer-policy', 'strict-origin-when-cross-origin');
    reply.header('x-frame-options', 'DENY');
    reply.header('cache-control', 'no-store, max-age=0');
    if (String(reply.getHeader('content-type') || '').includes('text/html')) {
      reply.header('content-security-policy', "default-src 'self'; style-src 'self'; script-src 'self'; base-uri 'self'; frame-ancestors 'none'");
    }
    return payload;
  });
  await app.listen(settings.port, settings.bind);
}

void bootstrap();
