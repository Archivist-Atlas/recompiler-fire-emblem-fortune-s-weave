"""Fast selective extractor for Fire Emblem: Fortune's Weave .nup containers.

Extracts:
  - <out>/exefs/*         (all ExeFS modules for lifting)
  - <out>/shader_romfs/*  (only BNSH/shader-bearing RomFS files for offline SPIR-V translation)

The 27 GiB RomFS itself is read directly from the .nup at runtime by fefw_rt.dll.
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import extract_nup as en  # noqa: E402

SHADER_EXTS = ('.bfsha', '.ptcl', '.arc', '.sarc', '.bnsh')


def is_shader_path(parts) -> bool:
    name = parts[-1].lower()
    if name.endswith('.zst'):
        name = name[:-4]
    return name.endswith(SHADER_EXTS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('nup', help='Path to .nup file')
    ap.add_argument('--out', required=True, help='Output directory')
    ap.add_argument('--shaders', action='store_true', help='Also extract shader-bearing RomFS files into <out>/shader_romfs')
    ap.add_argument('--no-verify', action='store_true')
    a = ap.parse_args()

    out = Path(a.out)
    zstd = en.Zstd()
    verify = not a.no_verify

    nup = en.Nup(a.nup)
    try:
        plan = en.install_plan(nup, 'latest')
        exefs_entries = en.merged_entries(nup, [plan['exefs']], 'exefs')
        print(f'[extract_nup_fefw] Extracting {len(exefs_entries)} ExeFS files...', flush=True)
        exefs_bytes = 0
        for parts, idx in exefs_entries:
            exefs_bytes += en.extract_file(nup, zstd, idx, out / 'exefs' / Path(*parts), verify)
        print(f'[extract_nup_fefw] ExeFS ready ({exefs_bytes / (1024 * 1024):.1f} MiB).', flush=True)

        if a.shaders:
            romfs_entries = en.merged_entries(nup, plan['romfs'], 'romfs')
            shader_entries = [(parts, idx) for parts, idx in romfs_entries if is_shader_path(parts)]
            print(f'[extract_nup_fefw] Extracting {len(shader_entries)} shader-bearing RomFS files (out of {len(romfs_entries)})...', flush=True)
            sh_bytes = 0
            for i, (parts, idx) in enumerate(shader_entries, 1):
                sh_bytes += en.extract_file(nup, zstd, idx, out / 'shader_romfs' / Path(*parts), verify)
                if i == len(shader_entries) or i % 200 == 0:
                    print(f'[extract_nup_fefw] Shaders extracted: {i}/{len(shader_entries)} ({sh_bytes / (1024 * 1024):.1f} MiB)', flush=True)
    finally:
        if getattr(nup, 'f', None):
            nup.f.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
