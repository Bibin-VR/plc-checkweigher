#!/usr/bin/env node
'use strict';

const { spawnSync } = require('child_process');
const { platform, arch } = require('os');
const path = require('path');
const fs   = require('fs');

// ── Colour helpers ────────────────────────────────────────────────────────────
const B  = '\x1b[1;34m';   // bold blue
const G  = '\x1b[0;32m';   // green
const R  = '\x1b[1;31m';   // red
const Y  = '\x1b[1;33m';   // yellow
const NC = '\x1b[0m';

function die(msg) {
  console.error(`\n${R}Error:${NC} ${msg}`);
  process.exit(1);
}

// ── Platform guards ───────────────────────────────────────────────────────────
if (platform() !== 'linux') {
  die('This installer only runs on Raspberry Pi (Linux). Got: ' + platform());
}
if (arch() !== 'arm64') {
  die('Requires 64-bit ARM (arm64). Got: ' + arch() +
      '\nMake sure you are running 64-bit Raspberry Pi OS.');
}

// ── Banner ────────────────────────────────────────────────────────────────────
console.log(`
${B}╔════════════════════════════════════════════╗
║   PLC Check-Weigher — Full Stack Installer  ║
╚════════════════════════════════════════════╝${NC}
`);

// ── Locate setup.sh (bundled inside this npm package) ────────────────────────
const setupScript = path.resolve(__dirname, '..', 'setup.sh');
if (!fs.existsSync(setupScript)) {
  die('setup.sh not found inside the package — try: npm install -g plc-checkweigher@latest');
}

// ── Inform the user ───────────────────────────────────────────────────────────
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

// ── Run setup.sh via sudo ─────────────────────────────────────────────────────
// stdio:'inherit' keeps the terminal fully interactive:
//   • sudo password prompt works
//   • WiFi password prompt works
//   • All colour output passes through
const result = spawnSync('sudo', ['bash', setupScript], {
  stdio: 'inherit',
  env: process.env,
});

process.exit(result.status ?? 0);
