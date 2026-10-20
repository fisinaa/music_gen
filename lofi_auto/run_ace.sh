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
profile="${LOFI_ACE_PROFILE:-stable}"
device=cpu; offload=false; dit_offload=false
case "$profile" in
  stable)
    if ! grep -q '_decode_generate_music_vae_cuda_fp32' acestep/core/generation/handler/generate_music_decode.py; then
      echo 'Missing the tested VAE GPU FP32 patch. Install ACE_VAE_GPU_FP32_Patch first.' >&2
      exit 1
    fi
    export ACESTEP_DTYPE=float32
    export ACESTEP_VAE_ON_CPU=1 ACESTEP_VAE_ON_GPU=1 ACESTEP_VAE_GPU_CHUNK=128
    ;;
  rtx3060|rtx3060-resident)
    # The old VAE patch requires CPU DiT. Select the native CUDA decode path.
    unset ACESTEP_DTYPE
    export ACESTEP_VAE_ON_CPU=0 ACESTEP_VAE_ON_GPU=0
    device=cuda; offload=true; dit_offload=true
    if [[ "$profile" == rtx3060-resident ]]; then dit_offload=false; fi
    .venv/bin/python "$root/gpu_check.py" --require-3060-class
    ;;
  *) echo "Unknown LOFI_ACE_PROFILE: $profile" >&2; exit 1 ;;
esac
if [[ "${1:-}" == --check ]]; then
  .venv/bin/python "$root/gpu_check.py"
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
