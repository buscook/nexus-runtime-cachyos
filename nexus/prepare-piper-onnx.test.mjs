// SPDX-License-Identifier: MIT
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const repo=path.resolve(import.meta.dirname,'..');
const helper=path.join(repo,'nexus/prepare-piper-onnx.py');
const lockPath=path.join(repo,'nexus/onnxruntime.lock.json');
const repositoryPackage=path.resolve(repo,'../../prerequisite-fix/microsoft.ml.onnxruntime.1.14.1.nupkg');
const repositoryCmake=[path.join(repo,'piper/CMakeLists.txt'),
  path.resolve(repo,'../nexus-runtime-cachyos/piper/CMakeLists.txt')];

const sha256=async file=>createHash('sha256').update(await fs.readFile(file)).digest('hex');
const exists=async file=>fs.lstat(file).then(()=>true,error=>{if(error.code==='ENOENT')return false;throw error;});

async function fixture(run){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'nexus-piper-onnx-node-'));
  try{await run(root);}finally{await fs.rm(root,{recursive:true,force:true});}
}

test('lock pins the official NuGet package, catalog, Piper CMake and both architectures',async()=>{
  const lock=JSON.parse(await fs.readFile(lockPath,'utf8'));
  assert.equal(lock.schema,1);
  assert.equal(lock.package.id,'Microsoft.ML.OnnxRuntime');
  assert.equal(lock.package.version,'1.14.1');
  assert.equal(lock.package.url,
    'https://api.nuget.org/v3-flatcontainer/microsoft.ml.onnxruntime/1.14.1/microsoft.ml.onnxruntime.1.14.1.nupkg');
  assert.equal(lock.package.size,104750444);
  assert.equal(lock.package.sha256,'3a277421efce94b5f91f4a5dc8efc1add99b4c01f547279d81098ffb4fafbb6d');
  assert.equal(lock.package.sha512,'mctLvjIwbOQM4HuPU/x5qM/21O/4ZaT/Hi1F7I3zqMQA2jo3ozKaBwqPwMPdWr2WJ3OdqoiL3R8RgWjdg1UGcg==');
  assert.match(lock.package.catalog.url,/^https:\/\/api\.nuget\.org\/v3\/catalog0\/data\/2023\.02\.27\.18\.52\.05\//);
  assert.equal(lock.package.catalog.sha256,'d7e6834b367eb409eb020260d442aedc8e55bd9bb04f457e62b3bc9b2fde3be5');
  assert.equal(lock.package.source_commit,'c57cf374b67f72575546d7b4c69a1af4972e2b54');
  assert.equal(lock.piper.commit,'9d06b74959570772e8bcbe7a3f696664d2421167');
  assert.equal(lock.piper.cmake_sha256,'70d118a08c7671705b478a77c1b83b7b9951d9544ea3fb6739976e726c4c9c01');
  assert.equal(lock.architectures.x86_64.runtime,'linux-x64');
  assert.equal(lock.architectures.aarch64.runtime,'linux-arm64');
  assert.equal(lock.architectures.x86_64.sha256,'9a6828636dd7bed5887e6ce898cac1010cbbd593e6931b63423d189d42165434');
  assert.equal(lock.architectures.aarch64.sha256,'8eec3ec997f04114baf667c22f192fbaaadc98cca3ece598d5042b8c2c57a44c');
  assert.equal(lock.headers.length,7);
  assert.deepEqual(lock.notices,['LICENSE.txt','ThirdPartyNotices.txt']);
});

test('make runs the ONNX prefetch before piper configure and passes it one direct argument',async()=>fixture(async root=>{
  const result=spawnSync('make',['-f',path.join(repo,'Makefile.in'),'-n',path.join(root,'.piper-x86_64-configure'),`SRCDIR=${repo}`],{
    cwd:root,encoding:'utf8',env:{...process.env,CONTAINER:'1',TARGET_ARCH:'x86_64',SOURCE_DATE_EPOCH:'0'},
  });
  assert.equal(result.status,0,result.stderr);
  const prefetch=result.stdout.indexOf('prepare-piper-onnx.py');
  const configure=result.stdout.indexOf(':: configuring piper-x86_64');
  assert.ok(prefetch>=0,result.stdout);
  assert.ok(configure>prefetch,'prefetch must run before nested piper CMake configure');
  assert.ok(result.stdout.includes(`--source "${root}/src-piper"`),result.stdout);
  assert.ok(result.stdout.includes(`--output "${root}/nexus-onnxruntime"`),result.stdout);
  assert.ok(result.stdout.includes('--arch "x86_64"'),result.stdout);
  assert.ok(result.stdout.includes('touch '+path.join(root,'.piper-post-source')),result.stdout);
  assert.ok(result.stdout.includes(`-DONNXRUNTIME_DIR:PATH=${root}/nexus-onnxruntime`),result.stdout);
}));

test('Makefile ships the ONNX notices from the same prefetch hook',async()=>{
  const makefile=await fs.readFile(path.join(repo,'Makefile.in'),'utf8');
  assert.match(makefile,/\$\(OBJ\)\/\.piper-post-source:\n\tpython3 "\$\(SRCDIR\)\/nexus\/prepare-piper-onnx\.py"/);
  assert.match(makefile,/\$\(OBJ\)\/\.piper-onnxruntime-docs: \$\(OBJ\)\/\.piper-post-source/);
  assert.match(makefile,/\$\(OBJ\)\/\.piper-x86_64-post-build: \$\(OBJ\)\/\.piper-onnxruntime-docs/);
  assert.match(makefile,/\$\(OBJ\)\/\.piper-aarch64-post-build: \$\(OBJ\)\/\.piper-onnxruntime-docs/);
  assert.match(makefile,/PIPER_ONNX_DOC := \$\(DST_DIR\)\/share\/doc\/nexus-runtime\/onnxruntime/);
  assert.match(makefile,/PIPER_CMAKE_ARGS = -DONNXRUNTIME_DIR:PATH=\$\(PIPER_ONNX_DIR\)/);
});

test('python fixture suite covers archive rejection, CMake pinning and idempotence',()=>{
  const result=spawnSync('python3',[path.join(repo,'nexus/prepare-piper-onnx.test.py')],{
    cwd:repo,encoding:'utf8',env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'},
  });
  assert.equal(result.status,0,result.stdout+result.stderr);
});

test('production CLI fetches from the already verified 104 MB package and is offline-idempotent',async t=>{
  const cmake=repositoryCmake[0];
  const alternate=repositoryCmake[1];
  const sourceCmake=await exists(cmake)?cmake:(await exists(alternate)?alternate:null);
  if(!await exists(repositoryPackage)||!sourceCmake){
    t.skip('requires prerequisite-fix package and an actual pinned Piper checkout');
    return;
  }
  await fixture(async root=>{
    const source=path.join(root,'src');
    const output=path.join(root,'nexus-onnxruntime');
    await fs.mkdir(source);
    await fs.copyFile(sourceCmake,path.join(source,'CMakeLists.txt'));
    const first=spawnSync('python3',[helper,'--source',source,'--output',output,'--arch','x86_64','--archive',repositoryPackage],{encoding:'utf8'});
    assert.equal(first.status,0,first.stderr);
    assert.match(first.stdout,/Piper ONNX Runtime 1\.14\.1 prepared/);
    assert.equal(await sha256(path.join(source,'CMakeLists.txt')),JSON.parse(await fs.readFile(lockPath,'utf8')).piper.cmake_patched_sha256);
    assert.equal(await sha256(path.join(output,'lib/libonnxruntime.so.1.14.1')),
      '9a6828636dd7bed5887e6ce898cac1010cbbd593e6931b63423d189d42165434');
    assert.equal(await fs.readlink(path.join(output,'lib/libonnxruntime.so')),'libonnxruntime.so.1.14.1');
    assert.equal((await fs.readdir(path.join(output,'include'))).filter(name=>name.endsWith('.h')).length,7);
    for(const notice of ['LICENSE.txt','ThirdPartyNotices.txt'])
      assert.ok((await fs.stat(path.join(output,notice))).size>0);
    const second=spawnSync('python3',[helper,'--source',source,'--output',output,'--arch','x86_64','--offline'],{encoding:'utf8'});
    assert.equal(second.status,0,second.stderr);
    assert.match(second.stdout,/already current/);
  });
});
