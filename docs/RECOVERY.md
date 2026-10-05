# Восстановление и перенос

## Сохранить с рабочей VM

Git хранит наш конвейер и патч, но не весь изменённый checkout ACE-Step, модели и
Python-окружение. Для точного восстановления сохраните также:

```bash
mkdir -p ~/music_gen_backup
git -C ~/ACE-Step-1.5 rev-parse HEAD > ~/music_gen_backup/acestep_commit.txt
git -C ~/ACE-Step-1.5 diff --binary > ~/music_gen_backup/acestep_local_changes.patch
~/ACE-Step-1.5/.venv/bin/python -m pip freeze > ~/music_gen_backup/python_packages.txt
```

Если в uv-окружении нет pip, используйте `uv pip freeze --python
~/ACE-Step-1.5/.venv/bin/python` с перенаправлением в тот же файл.
Отдельно сохраните незакоммиченные новые файлы ACE-Step, если добавляли их:
`git -C ~/ACE-Step-1.5 ls-files --others --exclude-standard` покажет список.
Дифф не содержит такие файлы.

Сохраните каталоги `~/lofi_auto/music`, `episodes`, `config.env` (если есть),
старые `~/lofi_queue/runs` и checkpoints ACE-Step. Кеш анимаций можно пересоздать.
`catalog.json` и манифесты очереди нужны для метаданных и продолжения.

## Установка патча VAE

На уже работающей VM повторная установка не нужна. При необходимости сначала
остановите генерацию и сервер. Затем:

```bash
~/ACE-Step-1.5/.venv/bin/python ~/music_gen/patches/vae_gpu_fp32/install.py ~/ACE-Step-1.5
```

Установщик принимает только точный исходник, по которому готовился патч,
или уже установленный патч. Проверяет хеш вспомогательного декодера и сохраняет
резервную копию. На чистом другом checkout может появиться `STOP` — не отключайте
эту проверку. Сначала восстановите сохранённый checkout и его локальные изменения
либо адаптируйте патч к фактическим исходникам.

`checks.json` — совместимость, а не универсальная поддержка всех версий ACE-Step.
Git-репозиторий сам по себе пока не является образом всей VM.

## Проверка после восстановления

```bash
bash ~/music_gen/scripts/install.sh
systemctl --user start lofi-ace
journalctl --user -u lofi-ace -f
```

Ищите `[VAE GPU FP32] DiT device=cpu; VAE=cuda:0; dtype=torch.float32; chunk=128`.
В конце — `VAE restored to CPU`. Проверяйте один трек до длинной очереди:

```bash
~/ACE-Step-1.5/.venv/bin/python ~/lofi_auto/lofi_queue.py run \
  --jobs ~/music_gen/examples/music_jobs_180_test.json \
  --output ~/lofi_auto/runs/recovery_test --limit 1 \
  --music-dir ~/lofi_auto/music
```

## Откат и журналы

После остановки сервера верните резервную копию `generate_music_decode.py`, путь
которой напечатал установщик патча. Старые файлы самого конвейера находятся в
`~/lofi_auto/backups/`. Журналы монтажа — `episodes/ИМЯ/work/*.log`.

```bash
systemctl --user status lofi-ace
journalctl --user -u lofi-ace -n 100
```

Служба управляет только сервером, который сама запустила. Сервер в старом терминале
останавливается в этом терминале; второй одновременно не запускайте.
