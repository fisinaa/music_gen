#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$root/config.env" ]]; then
  set -a
  source "$root/config.env"
  set +a
fi
ace_root="${ACE_ROOT:-$HOME/ACE-Step-1.5}"
cd "$ace_root"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
export OMP_NUM_THREADS=12 MKL_NUM_THREADS=12 OPENBLAS_NUM_THREADS=12
profile="${LOFI_ACE_PROFILE:-rtx3060}"
# Existing config.env files used stable for the retired GTX CPU workaround.
# Preserve the config file, but migrate that legacy name to the current GPU.
if [[ "$profile" == stable ]]; then
  echo 'Legacy ACE profile stable now uses rtx3060 (CUDA).' >&2
  profile=rtx3060
fi
device=cuda; offload=true; dit_offload=true
case "$profile" in
  rtx3060) ;;
  rtx3060-resident) dit_offload=false ;;
  *) echo "Unknown LOFI_ACE_PROFILE: $profile" >&2; exit 1 ;;
esac
# The old GPU VAE patch requires CPU DiT; use native CUDA decode instead.
unset ACESTEP_DTYPE
export ACESTEP_VAE_ON_CPU=0 ACESTEP_VAE_ON_GPU=0
.venv/bin/python "$root/gpu_check.py" --require-3060-class
if [[ "${1:-}" == --check ]]; then
  echo "Profile=$profile device=$device offload=$offload dit_offload=$dit_offload quantization=none"
  exit 0
fi
echo "ACE profile=$profile device=$device offload=$offload dit_offload=$dit_offload"
exec .venv/bin/acestep \
  --server-name 0.0.0.0 --port 7860 --language en \
  --device "$device" --init_service true --config_path acestep-v15-turbo \
  --init_llm false --backend pt --offload_to_cpu "$offload" \
  --offload_dit_to_cpu "$dit_offload" --quantization none \
  --use_flash_attention false --batch_size 1
