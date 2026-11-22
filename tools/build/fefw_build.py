# SPDX-License-Identifier: GPL-3.0-or-later
"""Build fefw.exe on the player's PC using MSYS2 Clang64 (clang++, clang, llvm-ar, llvm-rc).

Turns the player's own ExeFS dump into fefw.exe linked against prebuilt fefw_rt.dll / fefw_rt.lib.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

HERE = os.path.dirname(os.path.abspath(__file__))
RECOMP = os.path.join(HERE, 'recomp')
SDK = os.path.join(HERE, 'sdk')
WINDOWS = os.name == 'nt'
FORMAT = 3

LIFTER = ['nxrecomp', 'include/nxrecomp', 'games/fefw/CMakeLists.txt', 'games/fefw/config.py']
SITES = 'games/fefw/main.sites'
SDK_FILES = ['fefw_stub.cpp', 'fefw_hle.cpp', 'fefw_rt_api.h', 'fefw_fma.c', 'fefw.rc', 'fefw.manifest', 'fefw.ico']

MODULES = [
    ('main', 'main', 0x7100000000, 0, 153),
    ('sdk', 'sdk', 0x7500000000, 1, 50),
    ('subsdk0', 'subsdk0', 0x7600000000, 2, 5),
    ('subsdk1', 'subsdk1', 0x7640000000, 3, 14),
    ('subsdk2', 'subsdk2', 0x7680000000, 4, 3),
    ('rtld', 'rtld', 0x7700000000, 5, 1),
]
EXEFS_FILES = ['main', 'sdk', 'subsdk0', 'subsdk1', 'subsdk2', 'rtld', 'main.npdm']

CPU_VARIANTS = {
    'fma': {'mcpu': 'x86-64', 'extra_flags': ['-mcx16', '-msse4.2', '-mavx', '-mfma'], 'needs': 'FEFW_CPU_NEEDS_FMA_BUILD', 'link': []},
    'compat': {'mcpu': 'x86-64', 'extra_flags': ['-mcx16', '-msse4.2', '-mpopcnt'], 'needs': 'FEFW_CPU_NEEDS_COMPAT_BUILD', 'link': ['fefw_fma.c']},
}

CXXFLAGS_COMMON = [
    '-std=c++20', '-O2', '-w', '-fno-exceptions', '-fno-rtti', '-fno-strict-aliasing',
    '-ffp-contract=off', '-fno-math-errno', '-fno-threadsafe-statics', '-DNX_INLINE_INDIRECT',
]

CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL = 0x00004000


def find_tool(name):
    clang_bin = os.environ.get('FEFW_CLANG_BIN', r'C:\msys64\clang64\bin')
    cand = os.path.join(clang_bin, name + ('.exe' if WINDOWS else ''))
    if os.path.isfile(cand):
        return cand
    found = shutil.which(name)
    if found:
        return found
    raise Fail(f'Required compiler tool not found: {name} (looked in {clang_bin})')


def cxxflags(variant):
    return ['-march=' + CPU_VARIANTS[variant]['mcpu']] + CPU_VARIANTS[variant]['extra_flags'] + CXXFLAGS_COMMON


def say(kind, *parts):
    print('@' + kind + ' ' + ' '.join(str(p) for p in parts), flush=True)


def progress(phase, done, total, text):
    say('progress', phase, done, total, text)


class Fail(Exception):
    pass


class Cancelled(Exception):
    pass


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def tree_hash(root, rels):
    h = hashlib.sha256()
    for rel in rels:
        top = os.path.join(root, *rel.split('/'))
        if os.path.isfile(top):
            files = [rel]
        elif os.path.isdir(top):
            files = []
            for d, dirs, names in os.walk(top):
                dirs[:] = [x for x in dirs if x != '__pycache__']
                files += [os.path.relpath(os.path.join(d, n), root).replace('\\', '/') for n in names if not n.endswith('.pyc')]
        else:
            h.update(('%s\0missing\0' % rel).encode())
            continue
        for f in sorted(files):
            h.update(('%s\0%s\0' % (f, sha256(os.path.join(root, *f.split('/'))))).encode())
    return h.hexdigest()


def tool_stamp(variant):
    return {
        'lifter': tree_hash(RECOMP, LIFTER),
        'sites': tree_hash(RECOMP, [SITES]),
        'sdk': tree_hash(SDK, SDK_FILES),
        'runtime': sha256(os.path.join(SDK, 'fefw_rt.lib')),
        'cxxflags': cxxflags(variant),
        'cpu': variant,
        'modules': [list(m) for m in MODULES],
    }


def pick_variant(want):
    if want in (None, '', 'auto'):
        want = os.environ.get('FEFW_BUILD_CPU', '').strip().lower() or 'auto'
    if want in CPU_VARIANTS:
        return want, None
    try:
        clang = find_tool('clang')
        out = subprocess.run(
            [clang, '-march=native', '-dM', '-E', '-x', 'c', os.devnull],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW if WINDOWS else 0, timeout=30
        ).stdout.decode('utf-8', 'replace')
        has_fma = '__FMA__' in out and '__AVX__' in out
        return ('fma' if has_fma else 'compat'), None
    except Exception:
        return 'fma', None


def read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, value):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(value, f, indent=1)
    os.replace(tmp, path)


def verify(exefs):
    want = json.load(open(os.path.join(HERE, 'dump_hashes.json'), encoding='utf-8'))
    missing = [n for n in want['sha256'] if not os.path.isfile(os.path.join(exefs, n))]
    if missing:
        raise Fail('ExeFS folder %s is missing %s.' % (exefs, ', '.join(missing)))
    with open(os.path.join(exefs, 'main'), 'rb') as f:
        head = f.read(0x60)
    if head[:4] != b'NSO0':
        raise Fail('exefs/main is not a decrypted NSO.')
    bid = head[0x40:0x54].hex()
    if bid != want['main_build_id']:
        raise Fail('Unsupported game version (main build ID %s, expected %s).' % (bid, want['main_build_id']))
    names = list(want['sha256'])
    for i, n in enumerate(names):
        progress('verify', i, len(names), 'Checking game files: %s' % n)
        got = sha256(os.path.join(exefs, n))
        if n == 'main' and got == want.get('main_plain_sha256'):
            continue
        if got != want['sha256'][n]:
            raise Fail('exefs/%s does not match supported %s dump (sha256 %s).' % (n, want['version'], got))
    progress('verify', len(names), len(names), 'Game files verified (%s)' % want['version'])
    return want


def write_exefs(exefs, out):
    sys.path.insert(0, HERE)
    from nso_unpack import unpack
    os.makedirs(out, exist_ok=True)
    for n in EXEFS_FILES:
        src = os.path.join(exefs, n)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(out, n)
        if n == 'main':
            data = open(src, 'rb').read()
            flags = int.from_bytes(data[12:16], 'little')
            if flags & 0x87:
                progress('verify', 0, 1, 'Unpacking main (ZBIC)')
                data, _ = unpack(data)
            tmp = dst + '.tmp'
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, dst)
        else:
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.copyfile(src, dst)


def popen(cmd, **kw):
    if WINDOWS:
        kw['creationflags'] = CREATE_NO_WINDOW | BELOW_NORMAL
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw)


class Runner:
    def __init__(self, cancel_file):
        self.cancel_file = cancel_file
        self.procs = set()
        self.lock = threading.Lock()
        self.cancelled = False

    def check(self):
        if self.cancelled or os.path.exists(self.cancel_file):
            self.cancelled = True
            raise Cancelled()

    def run(self, cmd, env=None, cwd=None, on_line=None):
        self.check()
        p = popen(cmd, env=env, cwd=cwd)
        with self.lock:
            self.procs.add(p)
        tail = []
        for raw in p.stdout:
            line = raw.decode('utf-8', 'replace').rstrip()
            tail.append(line)
            del tail[:-30]
            if on_line:
                on_line(line)
        rc = p.wait()
        with self.lock:
            self.procs.discard(p)
        self.check()
        return rc, '\n'.join(tail)


def lift(runner, exefs, gen, jobs):
    sites = os.path.join(RECOMP, 'games', 'fefw', 'main.sites')
    for k, (name, nso, base, index, shards) in enumerate(MODULES):
        out = os.path.join(gen, name)
        marker = os.path.join(out, '.lifted')
        if os.path.isfile(marker):
            continue
        progress('lift', k, len(MODULES), 'Translating game code: %s (%d/%d)' % (name, k + 1, len(MODULES)))
        if os.path.isdir(out):
            shutil.rmtree(out)
        os.makedirs(out)
        args = ['lift', os.path.join(exefs, nso), out, '--name', name, '--base', hex(base), '--shards', str(shards),
                '--jobs', str(jobs), '--game', 'fefw', '--index', str(index)]
        if name == 'main':
            args += ['--sites', sites]
        cmd = [sys.executable, '-I', os.path.abspath(__file__), '--lift-one', json.dumps(args)]
        rc, tail = runner.run(cmd, cwd=RECOMP)
        if rc != 0:
            raise Fail('Translating %s failed (exit %d):\n%s' % (name, rc, tail))
        open(marker, 'w').write('ok\n')
    progress('lift', len(MODULES), len(MODULES), 'Game code translated')


def free_memory():
    if not WINDOWS:
        return None
    try:
        import ctypes
        class MS(ctypes.Structure):
            _fields_ = [('len', ctypes.c_uint32), ('load', ctypes.c_uint32)] + [(n, ctypes.c_uint64) for n in
                        ('total', 'avail', 'pagetotal', 'pageavail', 'virttotal', 'virtavail', 'extavail')]
        m = MS(len=ctypes.sizeof(MS))
        return m.avail if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)) else None
    except Exception:
        return None


def replace_retry(src, dst):
    delay = 0.1
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if delay > 3.2:
                raise
            time.sleep(delay)
            delay *= 2


def compile_all(runner, gen, obj, jobs, env, variant):
    clangxx = find_tool('clang++')
    units = []
    for name, _, _, _, _ in MODULES:
        d = os.path.join(gen, name)
        for f in sorted(os.listdir(d)):
            if f.endswith('.cpp'):
                units.append((name, os.path.join(d, f)))
    total = len(units)
    todo = []
    for name, src in units:
        o = os.path.join(obj, name, os.path.basename(src)[:-4] + '.o')
        if not os.path.isfile(o):
            todo.append((name, src, o))
    done = total - len(todo)
    progress('compile', done, total, 'Compiling: %d/%d' % (done, total))
    for name, _, _, _, _ in MODULES:
        os.makedirs(os.path.join(obj, name), exist_ok=True)
    todo.sort(key=lambda t: -os.path.getsize(t[1]))

    def one(t):
        name, src, o = t
        tmp = o + '.tmp'
        cmd = [clangxx] + cxxflags(variant) + ['-I', os.path.join(RECOMP, 'include'), '-I', os.path.dirname(src), '-c', src, '-o', tmp]
        rc, tail = runner.run(cmd, env=env)
        if rc != 0:
            return 'Compiling %s/%s failed (exit %d):\n%s' % (name, os.path.basename(src), rc, tail)
        try:
            replace_retry(tmp, o)
        except OSError as e:
            return 'Compiling %s/%s: %s' % (name, os.path.basename(src), e)
        return None

    per_job = 2 << 30
    failed = []
    queue = list(todo)
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        pending = {}
        try:
            while queue or pending:
                if queue and len(pending) < jobs:
                    avail = free_memory()
                    held = bool(pending) and avail is not None and avail < per_job
                    if not held:
                        t = queue.pop(0)
                        pending[ex.submit(one, t)] = t
                room = bool(queue) and len(pending) < jobs
                fin, _ = wait(pending, timeout=1.0 if room else None, return_when=FIRST_COMPLETED)
                for f in fin:
                    t = pending.pop(f)
                    if f.result():
                        failed.append(t)
                        continue
                    done += 1
                    if done == total or done % 10 == 0:
                        progress('compile', done, total, 'Compiling: %d/%d' % (done, total))
        except BaseException:
            for f in pending:
                f.cancel()
            raise
    for t in failed:
        err = one(t)
        if err:
            raise Fail(err)
        done += 1
        progress('compile', done, total, 'Compiling: %d/%d' % (done, total))


def clear_high_entropy_va(exe):
    with open(exe, 'r+b') as f:
        f.seek(0x3c)
        pe = int.from_bytes(f.read(4), 'little')
        f.seek(pe + 24)
        if f.read(2) != b'\x0b\x02':
            raise Fail('fefw.exe is not a PE32+ image')
        off = pe + 24 + 70
        f.seek(off)
        dc = int.from_bytes(f.read(2), 'little') & ~0x20
        f.seek(off)
        f.write(dc.to_bytes(2, 'little'))


def link(runner, obj, game, env, variant):
    clangxx = find_tool('clang++')
    clangc = find_tool('clang')
    llvmar = find_tool('llvm-ar')
    llvmrc = find_tool('llvm-rc')

    progress('link', 0, 1, 'Archiving and linking fefw.exe')
    libs = []
    for name, _, _, _, _ in MODULES:
        a = os.path.join(obj, 'fefw_%s.a' % name)
        if os.path.isfile(a):
            os.remove(a)
        objs = sorted(os.path.join(obj, name, f) for f in os.listdir(os.path.join(obj, name)) if f.endswith('.o'))
        rsp = os.path.join(obj, '%s.rsp' % name)
        with open(rsp, 'w', encoding='utf-8') as f:
            f.write('\n'.join('"%s"' % p.replace('\\', '/') for p in objs))
        rc, tail = runner.run([llvmar, 'rcs', a, '@' + rsp], env=env)
        if rc != 0:
            raise Fail('Archiving %s failed (exit %d):\n%s' % (name, rc, tail))
        libs.append(a)

    extra = []
    for src in CPU_VARIANTS[variant]['link']:
        o = os.path.join(obj, os.path.splitext(src)[0] + '.o')
        cmd = [clangc, '-march=' + CPU_VARIANTS[variant]['mcpu']] + CPU_VARIANTS[variant]['extra_flags'] + [
            '-O1', '-ffp-contract=off', '-fno-math-errno', '-c', os.path.join(SDK, src), '-o', o
        ]
        rc, tail = runner.run(cmd, env=env)
        if rc != 0:
            raise Fail('Compiling %s failed (exit %d):\n%s' % (src, rc, tail))
        extra.append(o)

    # Compile fefw_hle.cpp (native zstd multiframe acceleration)
    hle_o = os.path.join(obj, 'fefw_hle.o')
    rc, tail = runner.run([
        clangxx, '-std=c++20', '-O2', '-fno-exceptions', '-fno-rtti',
        '-I', os.path.join(RECOMP, 'include'),
        '-c', os.path.join(SDK, 'fefw_hle.cpp'), '-o', hle_o
    ], env=env)
    if rc != 0:
        raise Fail('Compiling fefw_hle.cpp failed (exit %d):\n%s' % (rc, tail))
    extra.append(hle_o)

    # Compile resource file (icon + UTF-8 manifest)
    res_o = os.path.join(obj, 'fefw.res')
    rc, tail = runner.run([llvmrc, '/FO', res_o, os.path.join(SDK, 'fefw.rc')], env=env, cwd=SDK)
    if rc != 0:
        raise Fail('Compiling fefw.rc failed (exit %d):\n%s' % (rc, tail))

    tmp = os.path.join(obj, 'fefw.exe')
    clang_lib_dir = os.path.join(os.path.dirname(os.path.dirname(clangxx)), 'lib')
    zstd_a = os.path.join(clang_lib_dir, 'libzstd.a')
    zstd_arg = [zstd_a] if os.path.isfile(zstd_a) else ['-lzstd']

    cmd = [
        clangxx, '-std=c++20', '-O1', '-fno-exceptions', '-fno-rtti', '-mcx16',
        '-I', SDK, '-DFEFW_CPU_NEEDS=' + CPU_VARIANTS[variant]['needs'],
        os.path.join(SDK, 'fefw_stub.cpp'), res_o
    ] + extra + libs + [
        os.path.join(SDK, 'fefw_rt.lib')
    ] + zstd_arg + [
        '-static-libgcc', '-static-libstdc++', '-Wl,-Bstatic', '-lwinpthread', '-Wl,-Bdynamic',
        '-o', tmp, '-Wl,--stack,134217728', '-Wl,--subsystem,windows'
    ]
    rc, tail = runner.run(cmd, env=env, cwd=SDK)
    if rc != 0:
        raise Fail('Linking fefw.exe failed (exit %d):\n%s' % (rc, tail))
    clear_high_entropy_va(tmp)
    dst = os.path.join(game, 'fefw.exe')
    replace_retry(tmp, dst)
    progress('link', 1, 1, 'fefw.exe built')
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exefs')
    ap.add_argument('--game')
    ap.add_argument('--jobs', type=int, default=max(1, min(12, (os.cpu_count() or 2) - 1)))
    ap.add_argument('--keep', action='store_true')
    ap.add_argument('--cpu', default='auto')
    ap.add_argument('--lift-one', help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.lift_one:
        sys.path.insert(0, RECOMP)
        from nxrecomp.__main__ import main as nx_main
        return nx_main(json.loads(a.lift_one)) or 0
    if not a.exefs or not a.game:
        ap.error('--exefs and --game are required')
    game = os.path.abspath(a.game)
    build = os.path.join(game, '_work', 'build')
    os.makedirs(build, exist_ok=True)
    cancel_file = os.path.join(build, 'cancel')
    if os.path.exists(cancel_file):
        os.remove(cancel_file)
    t0 = time.time()
    times = {}
    try:
        variant, why = pick_variant(a.cpu)
        if not variant:
            raise Fail(why)
        progress('verify', 0, 1, 'CPU build variant: %s' % variant)
        want = verify(a.exefs)
        plain = os.path.join(game, 'exefs')
        write_exefs(a.exefs, plain)
        stamp = {'format': FORMAT, 'main_build_id': want['main_build_id'], 'sha256': want['sha256'], 'tools': tool_stamp(variant)}
        state_p = os.path.join(build, 'state.json')
        if read_json(state_p) != stamp:
            for d in ('lift', 'obj'):
                shutil.rmtree(os.path.join(build, d), ignore_errors=True)
            write_json(state_p, stamp)
        times['verify'] = round(time.time() - t0, 1)
        runner = Runner(cancel_file)
        env = dict(os.environ)
        gen, obj = os.path.join(build, 'lift'), os.path.join(build, 'obj')
        t = time.time()
        lift(runner, plain, gen, a.jobs)
        times['lift'] = round(time.time() - t, 1)
        t = time.time()
        compile_all(runner, gen, obj, a.jobs, env, variant)
        times['compile'] = round(time.time() - t, 1)
        t = time.time()
        exe = link(runner, obj, game, env, variant)
        times['link'] = round(time.time() - t, 1)
        if not a.keep:
            progress('finish', 0, 1, 'Cleaning temporary build objects')
            for d in ('lift', 'obj'):
                shutil.rmtree(os.path.join(build, d), ignore_errors=True)
        done = dict(stamp, exe_sha256=sha256(exe), seconds=times, finished=time.strftime('%Y-%m-%d %H:%M:%S'))
        write_json(os.path.join(build, 'done.json'), done)
        progress('finish', 1, 1, 'fefw.exe ready (%d s)' % (time.time() - t0))
        return 0
    except Cancelled:
        say('cancelled', 'Build cancelled.')
        return 2
    except Fail as e:
        say('error', str(e))
        return 1
    except Exception as e:
        say('error', '%s: %s' % (type(e).__name__, str(e)))
        return 1


if __name__ == '__main__':
    sys.exit(main())
