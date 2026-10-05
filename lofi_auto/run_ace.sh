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
# Refuse CPU-only/unpatched installations: do not silently change the proven setup.
if ! grep -q '_decode_generate_music_vae_cuda_fp32' acestep/core/generation/handler/generate_music_decode.py; then
  echo 'Missing the tested VAE GPU FP32 patch. Install ACE_VAE_GPU_FP32_Patch first.' >&2
  exit 1
fi
export CUDA_VISIBLE_DEVICES=0 ACESTEP_DTYPE=float32
export ACESTEP_VAE_ON_CPU=1 ACESTEP_VAE_ON_GPU=1 ACESTEP_VAE_GPU_CHUNK=128
export OMP_NUM_THREADS=12 MKL_NUM_THREADS=12 OPENBLAS_NUM_THREADS=12
exec .venv/bin/acestep \
  --server-name 0.0.0.0 --port 7860 --language en \
  --device cpu --init_service true --config_path acestep-v15-turbo \
  --init_llm false --backend pt --offload_to_cpu false \
  --offload_dit_to_cpu false --quantization none \
  --use_flash_attention false --batch_size 1
