"""Read-only CUDA preflight using the ACE-Step virtual environment."""
import argparse
import json
import subprocess


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--require-3060-class', action='store_true')
    args = ap.parse_args()
    import torch
    info = {'torch': torch.__version__, 'cuda_runtime': torch.version.cuda,
            'cuda_available': torch.cuda.is_available()}
    try:
        info['ace_commit'] = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        info['ace_commit'] = 'unknown'
    if info['cuda_available']:
        prop = torch.cuda.get_device_properties(0)
        free, total = torch.cuda.mem_get_info(0)
        info.update(gpu=prop.name, capability=list(torch.cuda.get_device_capability(0)),
                    vram_gib=round(total / 2**30, 2), free_gib=round(free / 2**30, 2),
                    bf16=torch.cuda.is_bf16_supported())
    print(json.dumps(info, indent=2), flush=True)
    if args.require_3060_class and not (
            info['cuda_available'] and info['capability'][0] >= 8
            and info['vram_gib'] >= 11 and info['bf16']):
        raise SystemExit('GPU profile requires CUDA, Ampere or newer, BF16 and >=11 GiB VRAM. Check VM passthrough/driver; use stable to roll back.')


if __name__ == '__main__':
    main()
