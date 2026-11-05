import { build } from 'esbuild';
import { mkdir, readdir, unlink, writeFile } from 'node:fs/promises';
import { brotliCompressSync, gzipSync } from 'node:zlib';

// Build in memory so an unsuccessful build leaves the working player intact.
const result = await build({
  entryPoints: ['web/app.js', 'web/style.css'],
  bundle: true, minify: true, splitting: true, format: 'esm', target: 'es2022',
  outdir: 'web/dist', chunkNames: 'chunks/[name]-[hash]', write: false,
});
await mkdir('web/dist/chunks', { recursive: true });
const outputs = new Set();
for (const file of result.outputFiles) {
  await writeFile(file.path, file.contents);
  await writeFile(`${file.path}.gz`, gzipSync(file.contents, { level: 9 }));
  await writeFile(`${file.path}.br`, brotliCompressSync(file.contents));
  outputs.add(file.path.split('/').pop());
  outputs.add(`${file.path.split('/').pop()}.gz`);
  outputs.add(`${file.path.split('/').pop()}.br`);
}
// Discard stale hashed chunks from earlier builds.
for (const name of await readdir('web/dist/chunks')) {
  if (!outputs.has(name) && /\.js(?:\.(?:gz|br))?$/.test(name)) {
    await unlink(`web/dist/chunks/${name}`);
  }
}
