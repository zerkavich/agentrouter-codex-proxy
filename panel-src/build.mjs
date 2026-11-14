import { build } from 'esbuild';
import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.dirname(fileURLToPath(import.meta.url));
const target = path.join(root, '../panel');
mkdirSync(target, { recursive: true });
await build({ entryPoints: [path.join(root, 'src/main.jsx')], outfile: path.join(target, 'panel.js'),
  bundle: true, minify: true, format: 'esm', target: ['es2020'],
  define: { 'process.env.NODE_ENV': '"production"' }, legalComments: 'eof' });
execFileSync(process.execPath, [path.join(root, 'node_modules/@tailwindcss/cli/dist/index.mjs'),
  '-i', path.join(root, 'src/styles.css'), '-o', path.join(target, 'panel.css'), '--minify'], { cwd: root, stdio: 'inherit' });
writeFileSync(path.join(target, 'index.html'), `<!doctype html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light"><meta name="description" content="Локальное управление AgentRouter Codex Proxy">
<title>AgentRouter · Управление прокси</title><link rel="icon" type="image/svg+xml" href="/_control/assets/mark.svg">
<link rel="stylesheet" href="/_control/assets/panel.css"></head><body>
<div id="root"></div><noscript>Для управления прокси включите JavaScript. Состояние доступно по адресу /_health.</noscript>
<script type="module" src="/_control/assets/panel.js"></script></body></html>\n`);
writeFileSync(path.join(target, 'mark.svg'), `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="16" fill="#18181b"/><path d="M18 32h28M26 21l-11 11 11 11M38 21l11 11-11 11" stroke="#34d399" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>`);
console.log('Built offline panel assets:', target);
