#!/usr/bin/env node
'use strict';

const { spawnSync } = require('child_process');
const { platform, arch } = require('os');
const path = require('path');
const fs   = require('fs');

// ── Colour helpers ────────────────────────────────────────────────────────────
const B  = '\x1b[1;34m';
const G  = '\x1b[0;32m';
const R  = '\x1b[1;31m';
const Y  = '\x1b[1;33m';
const D  = '\x1b[2m';
const NC = '\x1b[0m';

function die(msg) {
  console.error(`\n${R}Error:${NC} ${msg}`);
  process.exit(1);
}

// ── Argument parsing ──────────────────────────────────────────────────────────
const args  = process.argv.slice(2);
const arg   = args[0] || '';

const INSTALL_FLAGS   = ['-tov', '--install',   'install'];
const UNINSTALL_FLAGS = ['-ex',  '--uninstall',  'uninstall', 'remove'];
const HELP_FLAGS      = ['-h',   '--help',       'help'];

let mode = 'install';   // default

if (HELP_FLAGS.includes(arg)) {
  console.log(`
${B}npx plc-checkweigher${NC} — PLC Check-Weigher setup utility

  ${Y}npx plc-checkweigher${NC}             Install  ${D}(default)${NC}
  ${Y}npx plc-checkweigher -tov${NC}        Install  ${D}(explicit)${NC}
  ${Y}npx plc-checkweigher -ex${NC}         Uninstall — removes all packages, code, services
  ${Y}npx plc-checkweigher --help${NC}      Show this help

${D}Aliases:  install / --install / -tov   ·   uninstall / --uninstall / -ex / remove${NC}
`);
  process.exit(0);
}

if (UNINSTALL_FLAGS.includes(arg))      mode = 'uninstall';
else if (INSTALL_FLAGS.includes(arg))   mode = 'install';
else if (arg !== '')                    die(`Unknown argument: ${arg}\nRun: npx plc-checkweigher --help`);

// ── Platform guards ───────────────────────────────────────────────────────────
if (platform() !== 'linux') {
  die('This installer only runs on Raspberry Pi (Linux). Got: ' + platform());
}
if (arch() !== 'arm64') {
  die('Requires 64-bit ARM (arm64). Got: ' + arch() +
      '\nMake sure you are running 64-bit Raspberry Pi OS.');
}

// ── Locate scripts ────────────────────────────────────────────────────────────
const pkgRoot       = path.resolve(__dirname, '..');
const setupScript   = path.join(pkgRoot, 'setup.sh');
const uninstallScript = path.join(pkgRoot, 'uninstall.sh');

// ── INSTALL ───────────────────────────────────────────────────────────────────
if (mode === 'install') {
  if (!fs.existsSync(setupScript)) {
    die('setup.sh not found inside the package — try: npx plc-checkweigher@latest');
  }

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

// ── UNINSTALL ─────────────────────────────────────────────────────────────────
if (mode === 'uninstall') {
  if (!fs.existsSync(uninstallScript)) {
    die('uninstall.sh not found inside the package — try: npx plc-checkweigher@latest -ex');
  }

  console.log(`
${R}╔══════════════════════════════════════════════╗
║   PLC Check-Weigher — Uninstaller            ║
╚══════════════════════════════════════════════╝${NC}
`);
  console.log(`${D}This will remove all services, code, venv, kernel config, and CLI tools.${NC}`);
  console.log(`${D}You will be asked whether to keep your PDF reports.${NC}`);
  console.log('');

  const result = spawnSync('sudo', ['bash', uninstallScript], {
    stdio: 'inherit',
    env:   process.env,
  });
  process.exit(result.status ?? 0);
}
