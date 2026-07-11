/**
 * sync-template-css.mjs
 *
 * Extracts CSS custom properties from the website's tokens.css and injects
 * them into the Jinja2 connection-info template between SYNC markers.
 *
 * This keeps the template self-contained (inline CSS) while sharing color
 * tokens with the Astro site. The template keeps its own layout/component CSS
 * outside the markers.
 *
 * Usage:
 *   node scripts/sync-template-css.mjs          # inject CSS
 *   node scripts/sync-template-css.mjs --check   # verify CSS is in sync (CI)
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const TOKENS_PATH = path.resolve(__dirname, '../src/styles/tokens.css');
const TEMPLATE_PATH = path.resolve(__dirname, '../../src/meridian/templates/connection-info.html.j2');

const START_MARKER = '/* SYNC:START */';
const END_MARKER = '/* SYNC:END */';

const isCheck = process.argv.includes('--check');

// 1. Read tokens.css
if (!fs.existsSync(TOKENS_PATH)) {
  console.error('✗ tokens.css not found at', TOKENS_PATH);
  process.exit(1);
}

// 2. Read template
if (!fs.existsSync(TEMPLATE_PATH)) {
  console.log('⊘ Template not found at', TEMPLATE_PATH);
  console.log('  CSS sync requires the Jinja2 template to exist.');
  process.exit(0);
}

const template = fs.readFileSync(TEMPLATE_PATH, 'utf-8');
const startIdx = template.indexOf(START_MARKER);
const endIdx = template.indexOf(END_MARKER);

if (startIdx === -1 || endIdx === -1) {
  if (isCheck) {
    console.log('✓ No SYNC markers in template — sync not active');
    process.exit(0);
  }
  console.log('⊘ No SYNC markers found in template.');
  console.log('  Add these markers to enable CSS sync:');
  console.log(`  ${START_MARKER}`);
  console.log('  ... synced CSS goes here ...');
  console.log(`  ${END_MARKER}`);
  process.exit(0);
}

// 3. Extract the CSS custom properties block from tokens.css
const tokens = fs.readFileSync(TOKENS_PATH, 'utf-8');

// Map website token names to template token names
// Website: --bg, --s1, --s2, --b1, --t1, --t2, --t3, --ac, --ok, --err, --warn, --blue, --amber
// Template: --bg, --sf, --sf2, --br, --tx, --tx2, --tx3, (no ac), --green, --red, (no warn), --blue, --amber
//
// The mapping is intentionally documented here but NOT automated.
// The template uses a dark-first scheme with different variable names.
// Sync only the protocol card colors (--blue, --amber) which are shared.

// Extract :root block from tokens
const rootMatch = tokens.match(/:root\s*\{([^}]+)\}/);
const darkMatch = tokens.match(/@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]+)\}/);

if (!rootMatch) {
  console.error('✗ Could not extract :root block from tokens.css');
  process.exit(1);
}

// Extract shared protocol tokens (blue, amber)
function extractVars(block, vars) {
  const result = {};
  for (const v of vars) {
    const re = new RegExp(`--${v}:\\s*([^;]+);`);
    const m = block.match(re);
    if (m) result[v] = m[1].trim();
  }
  return result;
}

const sharedVars = ['blue', 'blue-bg', 'blue-br', 'amber', 'amber-bg', 'amber-br'];
const lightVals = extractVars(rootMatch[1], sharedVars);
const darkVals = darkMatch ? extractVars(darkMatch[1], sharedVars) : {};

// Build the synced CSS block
let syncCSS = '\n';
syncCSS += '  /* Protocol card colors — synced from website/src/styles/tokens.css */\n';
syncCSS += '  /* Light mode overrides */\n';
syncCSS += '  @media(prefers-color-scheme:light){:root{\n';
for (const [k, v] of Object.entries(lightVals)) {
  syncCSS += `    --${k}: ${v};\n`;
}
syncCSS += '  }}\n';
if (Object.keys(darkVals).length > 0) {
  syncCSS += '  /* Dark mode values */\n';
  syncCSS += '  @media(prefers-color-scheme:dark){:root{\n';
  for (const [k, v] of Object.entries(darkVals)) {
    syncCSS += `    --${k}: ${v};\n`;
  }
  syncCSS += '  }}\n';
}

// 4. Replace content between markers
const before = template.substring(0, startIdx + START_MARKER.length);
const after = template.substring(endIdx);
const newTemplate = before + syncCSS + '  ' + after;

if (isCheck) {
  if (newTemplate === template) {
    console.log('✓ CSS sync check passed — template in sync with tokens.css');
    process.exit(0);
  } else {
    console.error('✗ CSS sync drift detected!');
    console.error('  Run: node website/scripts/sync-template-css.mjs');
    process.exit(1);
  }
}

// 5. Write back
fs.writeFileSync(TEMPLATE_PATH, newTemplate, 'utf-8');
console.log('✓ CSS synced to template');
console.log(`  Synced ${Object.keys(lightVals).length} light + ${Object.keys(darkVals).length} dark tokens`);
