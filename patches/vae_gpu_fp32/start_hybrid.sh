#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/ACE-Step-1.5"
exec systemd-run --user --scope --unit=acestep-lofi-hybrid \
  -p MemoryHigh=32G -p MemoryMax=36G -p MemorySwapMax=1G \
  taskset -c 0-11 nice -n 10 \
  env CUDA_VISIBLE_DEVICES=0 \
      ACESTEP_DTYPE=float32 \
      ACESTEP_VAE_ON_CPU=1 \
      ACESTEP_VAE_ON_GPU="${ACESTEP_VAE_ON_GPU:-1}" \
      ACESTEP_VAE_GPU_CHUNK="${ACESTEP_VAE_GPU_CHUNK:-128}" \
      OMP_NUM_THREADS=12 MKL_NUM_THREADS=12 OPENBLAS_NUM_THREADS=12 \
  .venv/bin/acestep \
  --server-name 0.0.0.0 --port 7860 --language en \
  --device cpu --init_service true --config_path acestep-v15-turbo \
  --init_llm false --backend pt --offload_to_cpu false \
  --offload_dit_to_cpu false --quantization none \
  --use_flash_attention false --batch_size 1
