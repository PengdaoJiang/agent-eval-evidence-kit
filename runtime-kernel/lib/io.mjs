import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

export class DeskError extends Error {
  constructor(message, status = 400, code = 'INVALID') { super(message); this.status = status; this.code = code; }
}
export const check = (condition, message, status, code) => { if (!condition) throw new DeskError(message, status, code); };
export const sha = value => crypto.createHash('sha256').update(value).digest('hex');
export const hashFile = p => sha(fs.readFileSync(p));
export const now = () => new Date().toISOString();
export const uid = () => crypto.randomUUID();
export const token = () => crypto.randomBytes(32).toString('hex');
export const readJSON = p => JSON.parse(fs.readFileSync(p, 'utf8').replace(/^\uFEFF/, ''));
export function canonical(x) {
  if (Array.isArray(x)) return '[' + x.map(canonical).join(',') + ']';
  if (x && typeof x === 'object') return '{' + Object.keys(x).sort().map(k => JSON.stringify(k) + ':' + canonical(x[k])).join(',') + '}';
  return JSON.stringify(x);
}
export const hashObject = x => sha(canonical(x));
export function id(x) { check(typeof x === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}$/.test(x), 'Invalid identifier'); return x; }
export function relative(p) {
  check(typeof p === 'string' && p.length < 240 && !/[\\:\x00-\x1f]/.test(p) && !path.isAbsolute(p), 'Only safe relative paths are accepted');
  check(p.split('/').every(x => x && x !== '.' && x !== '..' && !/[. ]$/.test(x) && !/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)/i.test(x)), 'Invalid path segment');
  return p;
}
export function inside(root, child) {
  const rel = path.relative(path.resolve(root), path.resolve(child));
  check(rel !== '..' && !rel.startsWith('..' + path.sep) && !path.isAbsolute(rel), 'Path escapes allowed root', 403, 'PATH_ESCAPE');
  return child;
}
// Reject junctions/reparse links in all components under the explicit root.
export function safePath(root, rel, exists = true) {
  relative(rel);
  let chain=path.resolve(root);
  while(true){check(!fs.lstatSync(chain).isSymbolicLink(),'Linked root paths are not allowed',403,'PATH_LINK');const parent=path.dirname(chain);if(parent===chain)break;chain=parent;}
  const base = fs.realpathSync(root); let p = base;
  for (const part of rel.split('/')) {
    p = path.join(p, part);
    if (fs.existsSync(p)) {
      check(!fs.lstatSync(p).isSymbolicLink(), 'Linked paths are not allowed', 403, 'PATH_LINK');
      inside(base, fs.realpathSync(p));
    } else if (exists) throw new DeskError('Required file is missing: ' + rel, 404, 'MISSING_FILE');
  }
  return inside(base, p);
}
export function atomicWrite(p, data) {
  fs.mkdirSync(path.dirname(p), {recursive: true});
  const temp = p + '.' + uid() + '.tmp'; const fd = fs.openSync(temp, 'wx', 0o600);
  try { fs.writeFileSync(fd, data); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  // Windows readers (including Python's registration poll) may briefly deny
  // delete-sharing. Retry the atomic replacement; never unlink the destination
  // or expose a partially written registration to make that replacement work.
  const attempts=process.platform==='win32'?26:1,wait=new Int32Array(new SharedArrayBuffer(4));
  for(let n=0;n<attempts;n++)try{fs.renameSync(temp,p);return;}catch(e){
    if(n+1<attempts&&['EPERM','EACCES','EBUSY'].includes(e.code)){Atomics.wait(wait,0,0,20);continue;}
    try{fs.unlinkSync(temp);}catch{}throw e;
  }
}
export const writeJSON = (p, x) => atomicWrite(p, JSON.stringify(x, null, 2) + '\n');
export function immutableWrite(p, data) {
  fs.mkdirSync(path.dirname(p), {recursive: true});
  if (fs.existsSync(p)) { check(hashFile(p) === sha(data), 'Immutable record differs', 409, 'IMMUTABLE_CONFLICT'); return; }
  const fd = fs.openSync(p, 'wx', 0o600);
  try { fs.writeFileSync(fd, data); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
}
export function listFiles(root, prefix = '') {
  const result = [];
  for (const e of fs.readdirSync(root, {withFileTypes:true})) {
    const r = prefix + e.name;
    check(!e.isSymbolicLink(), 'Linked input rejected: ' + r, 403);
    if (e.isDirectory()) result.push(...listFiles(path.join(root,e.name), r + '/'));
    else if (e.isFile()) result.push(r);
  }
  return result.sort();
}
export function acquireLock(p) {
  fs.mkdirSync(path.dirname(p), {recursive:true});
  try { const fd = fs.openSync(p,'wx'); fs.writeFileSync(fd, JSON.stringify({pid:process.pid,createdAt:now()})); fs.closeSync(fd); }
  catch (e) { if (e.code === 'EEXIST') throw new DeskError('Another writer owns this operation; inspect its PID before explicit recovery',409,'WRITER_BUSY'); throw e; }
  return () => fs.unlinkSync(p);
}
