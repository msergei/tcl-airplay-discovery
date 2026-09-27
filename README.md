# tcl-airplay-discovery

Делает AirPlay телевизора TCL (Google TV) видимым в сети, когда телевизор подключён по кабелю через USB-LAN переходник.

## Проблема

Если подключить телевизор TCL к сети через USB-LAN переходник, AirPlay на нём продолжает работать (порт 7000 отвечает), но сам телевизор перестаёт объявлять `_airplay._tcp` в mDNS/Bonjour. Chromecast и Android TV Remote при этом анонсируются нормально. В итоге Mac и iPhone не видят телевизор в «Повторе экрана», пока его не подключишь по Wi-Fi.

## Решение

`tcl-airplay-proxy.py` объявляет AirPlay вместо телевизора:

1. Раз в `POLL` секунд запрашивает у телевизора `http://<tv>:7000/info` с квалификаторами `txtAirPlay` и `txtRAOP`. Телевизор возвращает готовые TXT-записи, включая публичный ключ `pk`.
2. Рассылает в сеть mDNS-объявления `_airplay._tcp` и `_raop._tcp` с этими TXT-записями и отвечает на mDNS-запросы о них. Хост `tcl-airplay-proxy.local` указывает на IP телевизора.
3. Если телевизор перестал отвечать, ищет его заново через `_googlecast._tcp` и `_androidtvremote2._tcp` и сверяет найденные адреса по `deviceID`. Смена IP подхватывается сама.
4. Если телевизор выключен (не отвечает `FAILS_TO_DROP` проверок подряд), отправляет «goodbye», и телевизор пропадает из списков.
5. Если телевизор анонсирует AirPlay сам (например, подключён по Wi-Fi), дубликат не публикуется.
6. Переименование телевизора и другие изменения TXT подхватываются на следующей проверке.

Скрипт написан на чистом Python 3 без зависимостей и работает на Linux и macOS.

> Почему не `dns-sd -P` или `avahi-publish`: AirPlay на macOS игнорирует сервисы, зарегистрированные локальным mDNSResponder того же Mac. Поэтому скрипт сам шлёт mDNS-пакеты в сеть. На отдельном сервере это не важно, но так один код работает везде.

## Настройка

Переменные окружения:

| Переменная     | Обязательна | Описание |
|----------------|-------------|----------|
| `TV_DEVICE_ID` | да          | `deviceID` телевизора, например `AA:BB:CC:DD:EE:FF` |
| `TV_IP`        | нет         | Последний известный IP. Без него телевизор найдётся через mDNS |
| `POLL`         | нет         | Интервал проверки в секундах (по умолчанию 15, в примерах 60) |
| `TV_PORT`      | нет         | Порт AirPlay телевизора (по умолчанию 7000) |

Узнать `TV_DEVICE_ID` (телевизор должен быть включён):

```sh
python3 tcl-airplay-proxy.py --find
# 192.168.10.69    TV_DEVICE_ID=AA:BB:CC:DD:EE:FF  name='TCL'
```

## Запуск на сервере в Docker

Нужен постоянно включённый Linux-хост в той же сети, что и телевизор. Контейнер работает в `network_mode: host`, потому что mDNS-multicast из bridge-сети Docker не выходит. avahi-daemon на хосте не мешает: порт 5353 используется совместно.

```sh
git clone git@github.com:msergei/tcl-airplay-discovery.git ~/repos/tcl-airplay-discovery
cd ~/repos/tcl-airplay-discovery

docker compose build
docker compose run --rm tcl-airplay-proxy --find    # узнать TV_DEVICE_ID

cp .env.example .env    # вписать TV_DEVICE_ID
docker compose up -d
docker compose logs -f
```

Обновление: `git pull && docker compose up -d --build`.

## Запуск на macOS

Вариант с LaunchAgent (работает, только пока Mac не спит) лежит в ветке [`macos`](../../tree/macos).

## Проверка

```sh
# macOS
dns-sd -B _airplay._tcp local.
# Linux
avahi-browse -rt _airplay._tcp
```

После этого телевизор должен появиться в Пункте управления → «Повтор экрана» на Mac и iPhone.
