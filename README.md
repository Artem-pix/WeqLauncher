# WeqLauncher

Лаунчер Minecraft: Vanilla, Fabric, Forge, NeoForge, Quilt; аккаунты Ely.by и офлайн; установка модов из Modrinth.

## Запуск из исходников
```
pip install -r requirements.txt
python weqlauncher.py
```

## Сборка exe
- **Локально (Windows):** запустите `build.bat` → `dist\WeqLauncher.exe`.
- **На GitHub:** залейте репозиторий, затем `git tag v1.0.0 && git push --tags` — Actions соберёт exe и приложит его к Release. Либо запустите workflow вручную (Actions → Build WeqLauncher.exe) и скачайте артефакт.

Перед публикацией замените `YOUR_NAME` в `USER_AGENT` (weqlauncher.py) на свой GitHub-ник — Modrinth просит идентифицирующий User-Agent.

## Где хранятся данные
Windows: `%APPDATA%\WeqLauncher` — там `minecraft/` (общие файлы игры), `instances/<id>/` (моды, миры, настройки каждой сборки), настройки и аккаунты в `.json`. Лог последнего запуска — `instances/<id>/weqlauncher-latest.log`.
