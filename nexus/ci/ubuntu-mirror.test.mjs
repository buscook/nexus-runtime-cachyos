// SPDX-License-Identifier: MIT
import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {configureUbuntuMirrors,officialUbuntuMirrors} from './ubuntu-mirror.mjs';

async function fixture(run){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'nexus-ubuntu-mirror-'));
  try{await run(root);}finally{await fs.rm(root,{recursive:true,force:true});}
}

test('existing official archive, azure and security mirrors keep their endpoints',()=>{
  const source=[
    'deb [arch=amd64 signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] http://archive.ubuntu.com/ubuntu noble main restricted',
    'deb http://azure.archive.ubuntu.com/ubuntu noble-updates universe',
    'deb http://security.ubuntu.com/ubuntu noble-security main',
    'deb https://archive.ubuntu.com/ubuntu noble-backports multiverse',
  ].join('\n');
  assert.equal(officialUbuntuMirrors(source),[
    'deb [arch=amd64 signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] https://archive.ubuntu.com/ubuntu noble main restricted',
    'deb https://archive.ubuntu.com/ubuntu noble-updates universe',
    'deb https://security.ubuntu.com/ubuntu noble-security main',
    'deb https://archive.ubuntu.com/ubuntu noble-backports multiverse',
  ].join('\n'));
});

test('region EC2 archive hosts map to official archive HTTPS in legacy and deb822 URLs',()=>{
  const legacy=[
    'deb http://us-east-2.ec2.archive.ubuntu.com/ubuntu noble main restricted',
    'deb http://eu-west-1.ec2.archive.ubuntu.com/ubuntu noble-updates universe',
    'deb http://us-east-2.ec2.archive.ubuntu.com/ubuntu noble-backports multiverse',
  ].join('\n');
  assert.equal(officialUbuntuMirrors(legacy),[
    'deb https://archive.ubuntu.com/ubuntu noble main restricted',
    'deb https://archive.ubuntu.com/ubuntu noble-updates universe',
    'deb https://archive.ubuntu.com/ubuntu noble-backports multiverse',
  ].join('\n'));
  const deb822=[
    'Types: deb',
    'URIs: http://us-east-2.ec2.archive.ubuntu.com/ubuntu',
    'Suites: noble noble-updates',
    'Components: main restricted universe multiverse',
    'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg',
    '',
  ].join('\n');
  assert.equal(officialUbuntuMirrors(deb822),[
    'Types: deb',
    'URIs: https://archive.ubuntu.com/ubuntu',
    'Suites: noble noble-updates',
    'Components: main restricted universe multiverse',
    'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg',
    '',
  ].join('\n'));
  assert.equal(officialUbuntuMirrors('http://us-east-2.ec2.archive.ubuntu.com/ubuntu'),
    'https://archive.ubuntu.com/ubuntu');
});

test('GitHub mirrorlist is rewritten even when recognition comes from the security file',async()=>fixture(async root=>{
  const sources='deb mirror+file:/etc/apt/apt-mirrors.txt noble main restricted\ndeb mirror+file:/etc/apt/apt-mirrors.txt noble-updates main\n';
  const mirrorlist='# GitHub Actions Ubuntu mirror list\nhttp://us-east-2.ec2.archive.ubuntu.com/ubuntu\n';
  const security='http://security.ubuntu.com/ubuntu\n';
  await fs.mkdir(path.join(root,'sources.list.d'));
  await fs.writeFile(path.join(root,'sources.list'),sources);
  await fs.writeFile(path.join(root,'apt-mirrors.txt'),mirrorlist);
  await fs.writeFile(path.join(root,'apt-mirrors-security.txt'),security);
  await fs.writeFile(path.join(root,'sources.list.d','ubuntu.sources'),[
    'Types: deb','URIs: mirror+file:/etc/apt/apt-mirrors.txt','Suites: noble','Components: main','',
  ].join('\n'));

  assert.equal(await configureUbuntuMirrors(root),2);
  assert.equal(await fs.readFile(path.join(root,'apt-mirrors.txt'),'utf8'),
    '# GitHub Actions Ubuntu mirror list\nhttps://archive.ubuntu.com/ubuntu\n');
  assert.equal(await fs.readFile(path.join(root,'apt-mirrors-security.txt'),'utf8'),
    'https://security.ubuntu.com/ubuntu\n');
  assert.equal(await fs.readFile(path.join(root,'sources.list'),'utf8'),sources);
  assert.equal(await fs.readFile(path.join(root,'sources.list.d','ubuntu.sources'),'utf8'),
    ['Types: deb','URIs: mirror+file:/etc/apt/apt-mirrors.txt','Suites: noble','Components: main',''].join('\n'));
  assert.equal(await configureUbuntuMirrors(root),0);
  assert.equal(await fs.readFile(path.join(root,'apt-mirrors.txt'),'utf8'),
    '# GitHub Actions Ubuntu mirror list\nhttps://archive.ubuntu.com/ubuntu\n');
}));

test('an EC2-only mirrorlist or deb822 source is recognized as a supported configuration',async()=>{
  await fixture(async root=>{
    await fs.writeFile(path.join(root,'apt-mirrors.txt'),'http://us-east-2.ec2.archive.ubuntu.com/ubuntu\n');
    assert.equal(await configureUbuntuMirrors(root),1);
    assert.equal(await fs.readFile(path.join(root,'apt-mirrors.txt'),'utf8'),'https://archive.ubuntu.com/ubuntu\n');
  });
  await fixture(async root=>{
    await fs.mkdir(path.join(root,'sources.list.d'));
    await fs.writeFile(path.join(root,'sources.list.d','ubuntu.sources'),[
      'Types: deb','URIs: http://us-east-2.ec2.archive.ubuntu.com/ubuntu','Suites: noble','Components: main','',
    ].join('\n'));
    assert.equal(await configureUbuntuMirrors(root),1);
    assert.equal(await fs.readFile(path.join(root,'sources.list.d','ubuntu.sources'),'utf8'),[
      'Types: deb','URIs: https://archive.ubuntu.com/ubuntu','Suites: noble','Components: main','',
    ].join('\n'));
  });
});

test('unrelated third-party and lookalike URLs are never rewritten',()=>{
  const source=[
    'deb https://packages.microsoft.com/ubuntu/24.04/prod noble main',
    'deb https://ppa.launchpadcontent.net/owner/ppa/ubuntu noble main',
    'deb http://mirror.example.com/ubuntu noble main',
    'deb http://us-east-2.ec2.archive.ubuntu.com.evil.example/ubuntu noble main',
    'deb http://archive.ubuntu.com.evil.example/ubuntu noble main',
    'deb http://notarchive.ubuntu.com/ubuntu noble main',
    'deb http://archive.ubuntu.com/ubuntuevil noble main',
    'deb http://archive.ubuntu.com/ubuntu-evil noble main',
  ].join('\n');
  assert.equal(officialUbuntuMirrors(source),source);
});

test('unsupported configurations fail closed without rewriting anything',async()=>{
  await fixture(async root=>{
    const sources='deb https://packages.microsoft.com/ubuntu/24.04/prod noble main\n';
    await fs.writeFile(path.join(root,'sources.list'),sources);
    await assert.rejects(configureUbuntuMirrors(root),/No supported Ubuntu APT mirror configuration found\./);
    assert.equal(await fs.readFile(path.join(root,'sources.list'),'utf8'),sources);
  });
  await fixture(async root=>{
    await assert.rejects(configureUbuntuMirrors(root),/No supported Ubuntu APT mirror configuration found\./);
  });
});

test('symlinked APT sources fail closed before any planned write',async()=>fixture(async root=>{
  const sources='deb http://us-east-2.ec2.archive.ubuntu.com/ubuntu noble main\n';
  const target=path.join(root,'outside.txt');
  await fs.writeFile(path.join(root,'sources.list'),sources);
  await fs.writeFile(target,sources);
  await fs.symlink(target,path.join(root,'apt-mirrors.txt'));
  await assert.rejects(configureUbuntuMirrors(root),/APT source must be a regular file: apt-mirrors\.txt/);
  assert.equal(await fs.readFile(path.join(root,'sources.list'),'utf8'),sources);
  assert.equal(await fs.readFile(target,'utf8'),sources);
}));
