from pathlib import Path

from sync_music.yandex import YandexTarget, load_yandex_client

yt = YandexTarget(load_yandex_client(Path(r"C:\Users\2008d\working-space\spotify-yandex-sync\data\yandex_token.json")))

print(yt.get_likes()[0])