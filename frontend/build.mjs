import {copyFile, readFile, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {build} from 'esbuild';

const assets = '../xclaw/channels/static';
const result = await build({
  entryPoints: ['src/index.tsx'],
  bundle: true,
  minify: true,
  format: 'iife',
  outfile: path.join(assets, 'openui.js'),
  define: {'process.env.NODE_ENV': '"production"'},
  legalComments: 'eof',
  metafile: true,
});
await copyFile('catalog-prompt.txt', path.join(assets, 'openui-prompt.txt'));

const packages = new Set();
for (const input of Object.keys(result.metafile.inputs)) {
  const match = input.match(/^(.*node_modules\/(?:@[^/]+\/)?[^/]+)\//);
  if (match) packages.add(match[1]);
}
const notices = [];
for (const directory of [...packages].sort()) {
  const manifest = JSON.parse(await readFile(path.join(directory, 'package.json'), 'utf8'));
  const license = await readFile(path.join(directory, 'LICENSE'), 'utf8');
  notices.push(manifest.name + ' ' + manifest.version + '\n' + license);
}
await writeFile(path.join(assets, 'openui-licenses.txt'), notices.join('\n\n'));
