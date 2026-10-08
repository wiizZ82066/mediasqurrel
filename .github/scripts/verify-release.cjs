'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const yaml = require('js-yaml');
const { version } = require('../../package.json');
const root = path.resolve(process.argv[2] || 'release');
const manifest = yaml.load(fs.readFileSync(path.join(root, 'latest.yml'), 'utf8'));
const name = `Media-Squirrel-Setup-${version}.exe`;
assert.equal(manifest.version, version);
assert.equal(manifest.files.length, 1);
assert.equal(manifest.files[0].url, name);
const data = fs.readFileSync(path.join(root, name));
assert.equal(manifest.files[0].size, data.length);
assert.equal(manifest.files[0].sha512, crypto.createHash('sha512').update(data).digest('base64'));
assert.ok(fs.statSync(path.join(root, name + '.blockmap')).size > 0);
const config = yaml.load(fs.readFileSync(path.join(root, 'win-unpacked/resources/app-update.yml'), 'utf8'));
assert.equal(config.provider, 'github');
assert.equal(config.owner, 'wiizZ82066');
assert.equal(config.repo, 'mediasqurrel');
assert.equal(config.publisherName, undefined, 'Unsigned channel must not promise a certificate publisher');
const files = [name, name + '.blockmap', 'latest.yml'];
fs.writeFileSync(path.join(root, 'SHA256SUMS.txt'), files.map(file =>
  crypto.createHash('sha256').update(fs.readFileSync(path.join(root, file))).digest('hex') + '  ' + file
).join('\n') + '\n');
console.log('Version, GitHub provider, installer size/SHA512 and blockmap verified:', version);
