# Spotify → Yandex Music Sync

Локальный Python-проект без Soundiiz и других платных синхронизаторов.

Что умеет:

- Spotify **Liked Songs** → отдельный плейлист Яндекс.Музыки.
- Любой Spotify-плейлист, которым **владеешь ты** или где ты collaborator → Яндекс.Музыка.
- Spotify **Daily Mix** → Яндекс.Музыка через сохранённую локальную сессию Spotify Web (Playwright), потому что текущий Spotify Web API не отдаёт содержимое Spotify-owned плейлистов.
- SQLite-кэш `Spotify track ID -> Yandex track ID`, чтобы повторно не искать уже сопоставленные треки.
- fuzzy matching по названию, артистам и длительности.
- целевой плейлист создаётся автоматически и дальше обновляется как зеркало.
- история запусков лежит в `data/sync.db`.

> Проект переносит только метаданные/ID плейлистов. Аудио не скачивается.

## 1. Требования

- Python 3.10+ (сама yandex-music поддерживает 3.8+, но проект лучше запускать на 3.10+).
- Spotify Premium для владельца Development Mode приложения по актуальным правилам Spotify.
- Chromium для Playwright нужен только для Daily Mix.

## 2. Установка

### Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
python run.py init
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python run.py init
```

После `init` появится `config.yaml`.

## 3. Что взять в Spotify Developer Dashboard

Открой Spotify for Developers Dashboard и создай приложение.

Нужно взять только:

- **Client ID** — вставить в `config.yaml` в `spotify.client_id`.

Client Secret проекту **не нужен**: авторизация реализована через Authorization Code + PKCE.

В настройках приложения добавь Redirect URI **точно**:

```text
http://127.0.0.1:8787/callback
```

Не пиши `localhost`: Spotify требует для loopback явный IP `127.0.0.1`/`::1`.

Используемые scopes:

```text
user-library-read playlist-read-private
```

## 4. Авторизация

Spotify API:

```bash
python run.py auth spotify
```

Откроется браузер, подтверди доступ. Токен сохраняется локально в `data/spotify_token.json`.

Yandex Music:

```bash
python run.py auth yandex
```

Скрипт покажет URL и код Device Flow. После подтверждения токен сохранится в `data/yandex_token.json`.

Важно: библиотека Yandex Music сама не обновляет истёкший OAuth access token. Если когда-нибудь авторизация перестанет работать, просто снова выполни `python run.py auth yandex`.

## 5. Настройка Liked Songs → нативное «Мне нравится» Яндекса

В `config.yaml` уже есть:

```yaml
playlists:
  - name: liked
    source: spotify_liked
    target: yandex_likes
    sync_mode: mirror
```

Это **не отдельный плейлист**. Скрипт ставит и снимает настоящие лайки Яндекс.Музыки через `users_likes_tracks()`, `users_likes_tracks_add()` и `users_likes_tracks_remove()`. Поэтому треки появляются в штатном разделе «Мне нравится» клиента Яндекс.Музыки.

`sync_mode: mirror` работает безопасно: первый запуск не удаляет твои старые отдельные лайки Яндекса. Скрипт запоминает в SQLite только те Yandex-треки, которые соответствуют Spotify Liked Songs. Если потом убрать такой трек из Spotify Liked Songs, при следующем sync его управляемый лайк в Яндексе тоже снимется.

Если нужны только добавления и никогда не нужно снимать лайки автоматически, поставь `sync_mode: add_only`.

Запуск:

```bash
python run.py sync liked
```

Первый запуск может быть долгим, потому что каждый новый Spotify-трек ищется в Яндекс.Музыке. После этого соответствия кэшируются в SQLite.

### Обновление со старой версии проекта

Старый `config.yaml` можно не пересоздавать: если у binding `source: spotify_liked` поле `target` отсутствует, новая версия автоматически считает целью `yandex_likes`. Старый `target_title: "Spotify — Мне нравится"` для этого binding больше не используется. Если же ты специально хочешь отдельный плейлист, явно укажи `target: yandex_playlist`.

## 6. Обычный Spotify-плейлист

После Spotify OAuth можешь вывести свои плейлисты и их ID:

```bash
python run.py spotify-playlists
```

Добавь:

```yaml
  - name: main
    source: spotify_playlist
    source_id: "37i9dQZF..."
    target: yandex_playlist
    target_title: "Spotify — Main"
    target_visibility: private
```

`source_id` — кусок URL после `/playlist/`.

Пример:

```text
https://open.spotify.com/playlist/ABC123XYZ?si=...
                                  ^^^^^^^^^
                                  source_id
```

Из-за ограничений Spotify Web API 2026 это должен быть плейлист, которым владеешь ты, либо collaborative playlist, где ты участник. Spotify-owned редакционные плейлисты дадут HTTP 403.

## 7. Daily Mix

У Daily Mix делаем отдельный источник через Spotify Web.

### Один раз сохранить браузерную сессию

```bash
python run.py auth spotify-web
```

Откроется Chromium. Войди в Spotify и после загрузки Web Player нажми Enter в терминале.

### Получить URL Daily Mix

В Spotify:

1. Открой нужный **Daily Mix**.
2. Нажми `...`.
3. `Share` / `Поделиться`.
4. `Copy link to playlist` / `Копировать ссылку`.

Вставь URL в `config.yaml`:

```yaml
  - name: daily_mix_1
    source: spotify_daily_mix
    source_url: "https://open.spotify.com/playlist/PASTE_HERE"
    target: yandex_playlist
    target_title: "Spotify — Daily Mix 1"
    target_visibility: private
```

Проверка:

```bash
python run.py sync daily_mix_1
```

Spotify Web — неофициальный источник для этой части. Если Spotify поменяет DOM, селектор находится в `sync_music/daily_mix.py` и его можно поправить отдельно от всей остальной системы.

## 8. Синхронизировать всё

```bash
python run.py sync all
```

## 9. Автозапуск

### Linux cron

Например Liked Songs каждые 30 минут, Daily Mix каждый день в 07:15:

```cron
*/30 * * * * cd /path/to/spotify-yandex-sync && /path/to/.venv/bin/python run.py sync liked >> data/cron.log 2>&1
15 7 * * * cd /path/to/spotify-yandex-sync && /path/to/.venv/bin/python run.py sync daily_mix_1 >> data/cron.log 2>&1
```

### Windows Task Scheduler

Создай две задачи и укажи программу:

```text
C:\path\spotify-yandex-sync\.venv\Scripts\python.exe
```

Аргументы, например:

```text
run.py sync liked
```

и `Start in`:

```text
C:\path\spotify-yandex-sync
```

## 10. SQLite

База создаётся сама: `data/sync.db`.

Основные таблицы:

- `track_map` — кэш сопоставлений Spotify/Yandex.
- `binding_track_state` — какие Yandex-треки управляются конкретным binding; используется для безопасного снятия лайков.
- `playlist_state` — состояние каждой привязки/цели.
- `sync_runs` — история запусков/ошибок.

Если какой-то трек был неправильно сопоставлен, можно удалить конкретную запись из `track_map` и при следующем sync поиск выполнится заново.

## 11. Как работает matching

Поиск в Яндекс.Музыке: `artist + title`.

Score:

- title: 52%
- artists: 36%
- duration: 12%

По умолчанию принимается `score >= 86`.

Настройки находятся в `config.yaml`:

```yaml
matching:
  min_score: 86
  candidate_limit: 30
  duration_tolerance_ms: 8000
```

Ненайденные треки не добавляются и записываются в SQLite со статусом `unmatched`.

## 12. Важные ограничения

1. Spotify Web API с 2026 года позволяет читать items плейлиста только если текущий пользователь — owner/collaborator. Поэтому Daily Mix через официальный endpoint не читается.
2. Spotify Development Mode требует Premium у владельца приложения.
3. Yandex Music библиотека неофициальная и использует reverse-engineered API.
4. Daily Mix scraper зависит от разметки Spotify Web и потенциально требует небольшого ремонта после изменений сайта.
5. Не коммить `config.yaml`, `data/*.json`, `data/*.db` и browser profile — `.gitignore` уже настроен.

Matcher v2
----------
Матчер умеет сопоставлять кириллицу и латинскую транслитерацию (например, Moya Mishel <-> Моя Мишель), использует fallback-поиск только по названию, проверяет до 30 кандидатов и учитывает длительность трека. Уже сохранённые matched-записи в data/sync.db используются как раньше; unmatched автоматически проверяются заново на следующем sync.

