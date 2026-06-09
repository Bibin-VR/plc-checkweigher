#!/usr/bin/env node
'use strict';

const { spawnSync } = require('child_process');
const { platform, arch } = require('os');
const path = require('path');
const fs   = require('fs');

// ── Colours ───────────────────────────────────────────────────────────────────
const B  = '\x1b[1;34m';   // bold blue
const G  = '\x1b[0;32m';   // green
const R  = '\x1b[1;31m';   // red
const Y  = '\x1b[1;33m';   // yellow
const D  = '\x1b[2m';      // dim
const NC = '\x1b[0m';      // reset

function die(msg) {
  console.error(`\n${R}Error:${NC} ${msg}`);
  process.exit(1);
}

// ── Dot-matrix font  (5 px wide × 5 px tall, █ = lit, space = dark) ──────────
const GLYPHS = {
  'T': ['█████', '  █  ', '  █  ', '  █  ', '  █  '],
  'Ø': [' ███ ', '█  /█', '█ / █', '█/  █', ' ███ '],
  'V': ['█   █', '█   █', ' █ █ ', ' █ █ ', '  █  '],
  'E': ['█████', '█    ', '████ ', '█    ', '█████'],
  'X': ['█   █', ' █ █ ', '  █  ', ' █ █ ', '█   █'],
  'S': [' ████', '█    ', ' ███ ', '    █', '████ '],
  'Y': ['█   █', ' █ █ ', '  █  ', '  █  ', '  █  '],
  'M': ['█   █', '██ ██', '█ █ █', '█   █', '█   █'],
  ' ': ['     ', '     ', '     ', '     ', '     '],
};

/**
 * Returns 5 equal-length strings representing the dot-matrix rows of `word`.
 * Each character glyph is 5 wide; glyphs are separated by a single space.
 */
function dotRows(word) {
  const rows = ['', '', '', '', ''];
  for (const ch of word.toUpperCase()) {
    const g = GLYPHS[ch] || GLYPHS[' '];
    for (let i = 0; i < 5; i++) rows[i] += g[i] + ' ';
  }
  // Remove the one trailing separator space added after the last glyph
  return rows.map(r => r.slice(0, -1));
}

// ── TØVEX-SYSTEMS access banner ───────────────────────────────────────────────
function buildBanner() {
  const INNER = 52;

  function cen(str) {
    const len  = str.length;
    const lpad = Math.floor((INNER - len) / 2);
    const rpad = INNER - len - lpad;
    return ' '.repeat(Math.max(0, lpad)) + str + ' '.repeat(Math.max(0, rpad));
  }

  const bar   = '═'.repeat(INNER);
  const blank = `${B}║${' '.repeat(INNER)}║${NC}`;

  function boxRow(str, color) {
    return `${B}║${NC}${color}${cen(str)}${NC}${B}║${NC}`;
  }

  const tRow = dotRows('TØVEX');
  const sRow = dotRows('SYSTEMS');
  const sep  = Array.from({ length: sRow[0].length }, (_, i) => i % 2 ? ' ' : '·').join('');

  const lines = [
    '',
    `${B}╔${bar}╗${NC}`,
    blank,
    ...tRow.map(r => boxRow(r, B)),
    blank,
    boxRow(sep, D),
    blank,
    ...sRow.map(r => boxRow(r, B)),
    blank,
    `${B}╚${bar}╝${NC}`,
    '',
    `  ${R}⚠  Please contact administrator for access${NC}`,
    '',
  ];
  return lines.join('\n');
}

function showAccessDenied() {
  const banner = buildBanner();
  const isPostinstall = process.env.npm_lifecycle_event === 'postinstall';

  if (isPostinstall) {
    // npm 7+ pipes away stdout/stderr of dependency lifecycle scripts.
    // Write directly to /dev/tty so it reaches the terminal regardless.
    try {
      const fd = fs.openSync('/dev/tty', 'w');
      fs.writeSync(fd, banner + '\n');
      fs.closeSync(fd);
    } catch (_) {
      // No real terminal attached (CI, pipe) — silently skip.
    }
    process.exit(0);
  }

  console.log(banner);
  process.exit(1);
}

// ── Argument parsing ──────────────────────────────────────────────────────────
const arg = (process.argv[2] || '').trim();

const INSTALL_FLAGS   = ['-tov', '--install',   'install'];
const UNINSTALL_FLAGS = ['-ex',  '--uninstall',  'uninstall', 'remove'];
const HELP_FLAGS      = ['-h',   '--help',       'help'];

if (HELP_FLAGS.includes(arg)) {
  showAccessDenied();
}

let mode = 'access';   // default: show brand banner + access denied
if      (INSTALL_FLAGS.includes(arg))   mode = 'install';
else if (UNINSTALL_FLAGS.includes(arg)) mode = 'uninstall';
else if (arg !== '')                    showAccessDenied();

// ── Platform guards (skip for help / access-denied) ───────────────────────────
if (mode !== 'access') {
  if (platform() !== 'linux')
    die('This installer only runs on Raspberry Pi (Linux). Got: ' + platform());
  if (arch() !== 'arm64')
    die('Requires 64-bit ARM (arm64). Got: ' + arch() +
        '\nMake sure you are running 64-bit Raspberry Pi OS.');
}

// ── Locate scripts ────────────────────────────────────────────────────────────
const pkgRoot        = path.resolve(__dirname, '..');
const setupScript    = path.join(pkgRoot, 'setup.sh');
const uninstallScript = path.join(pkgRoot, 'uninstall.sh');

// ── Dispatch ──────────────────────────────────────────────────────────────────
if (mode === 'access') {
  showAccessDenied();   // exits with code 1
}

if (mode === 'install') {
  if (!fs.existsSync(setupScript))
    die('setup.sh not found — try reinstalling the package: npx plc-checkweigher@latest');

  console.log(`
${B}╔══════════════════════════════════════════════╗
║   PLC Check-Weigher — Full Stack Installer   ║
╚══════════════════════════════════════════════╝${NC}
`);
  console.log(`${Y}This will:${NC}`);
  console.log('  1. Install the PREEMPT_RT real-time kernel (reboots once)');
  console.log('  2. Install all Python dependencies');
  console.log('  3. Clone / update the plc-checkweigher repo');
  console.log('  4. Configure WiFi, SMB file sharing  (credentials → smb_config.py)');
  console.log('  5. Install systemd services with RT scheduling priority');
  console.log('  6. Set up live dashboard  →  http://<pi-ip>:8080/live');
  console.log('  7. Set up PDF report viewer with instant auto-refresh');
  console.log('');
  console.log(`${Y}Sudo password required to make system-level changes.${NC}`);
  console.log('');

  const result = spawnSync('sudo', ['bash', setupScript], {
    stdio: 'inherit',
    env:   process.env,
  });
  process.exit(result.status ?? 0);
}

if (mode === 'uninstall') {
  if (!fs.existsSync(uninstallScript))
    die('uninstall.sh not found — try reinstalling the package: npx plc-checkweigher@latest');

  console.log(`
${R}╔══════════════════════════════════════════════╗
║   PLC Check-Weigher — Uninstaller            ║
╚══════════════════════════════════════════════╝${NC}
`);
  console.log(`${D}Removes all services, code, venv, kernel config, and CLI tools.${NC}`);
  console.log(`${D}You will be asked whether to keep your PDF reports.${NC}`);
  console.log('');

  const result = spawnSync('sudo', ['bash', uninstallScript], {
    stdio: 'inherit',
    env:   process.env,
  });
  process.exit(result.status ?? 0);
}
