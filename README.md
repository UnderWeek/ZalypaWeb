# Auralis Browser

Auralis — самостоятельный настольный браузер на Python 3.12+, PySide6 и Qt WebEngine с оригинальной дизайн‑системой в духе Material 3. Это не HTML‑макет: приложение создаёт постоянные Chromium‑профили, хранит пользовательские данные в SQLite и проходит реальный WebEngine smoke‑запуск.

## Что уже работает

- Вкладки: создание, закрытие, drag & drop, закрепление, группы, hover‑предпросмотр, дублирование, восстановление закрытой вкладки и всей сессии.
- Навигация: назад/вперёд, reload/stop, домашняя страница, универсальная адресная строка, поиск, подсказки из истории и закладок.
- Qt WebEngine: JavaScript, cookies, local storage, дисковый cache, PDF viewer, fullscreen, popup‑окна, уведомления, разрешения сайтов и DevTools.
- Закладки: папки, поиск, редактируемый диалог, импорт/экспорт в переносимый JSON.
- История: посещения и поисковые запросы, даты, подсказки, отдельное удаление и очистка по периоду.
- Загрузки: постоянный журнал, прогресс, скорость, pause/resume/cancel текущей сессии, открытие файла и папки.
- Профили: реестр пользователей, аватары в модели, отдельные SQLite, cookies, cache, WebEngine storage, разрешения, расширения и сессии. Новый профиль открывается в отдельном процессе.
- Настройки: стартовая страница, поисковик, язык, приватность, HTTPS‑only, тема, динамический accent, плотность UI, масштаб, производительность и каталог загрузок.
- Material 3 UI: семантические цветовые роли, генерируемая светлая/тёмная палитра, системная тема, elevation, карточки, собственные switches/buttons, анимации и адаптивная стартовая страница.
- Безопасность: проверка URL/scheme/host, HTTPS‑индикатор, локальный blocklist, отказ при ошибке сертификата и постоянное управление разрешениями.
- Инструменты: режим чтения, перевод по явному действию, QR‑код, share через буфер, печать в PDF, поиск по странице, zoom и DevTools.
- Реклама и трекеры: перехват до сетевого запроса, whitelist, пользовательские правила, стартовый набор и асинхронное обновление официальной EasyList без блокировки UI.
- Расширения: безопасная установка unpacked/ZIP, валидация Manifest V2/V3, permissions, content scripts и абстрактный runtime‑адаптер.
- Sync: асинхронные `SyncBackend`, `SyncDataAdapter`, курсоры, пакетный merge и тестовый in‑memory backend.

## Быстрый запуск

Требуется 64‑битный Python 3.12 или новее.

### Windows PowerShell

```powershell
.\install.ps1
.\run.ps1
```

Для окружения разработчика:

```powershell
.\install.ps1 -Dev
.venv\Scripts\python.exe -m pytest -q
```

### Linux

```bash
sh install.sh
sh run.sh
```

На минимальной Linux‑системе Qt WebEngine также потребуются системные библиотеки OpenGL/EGL, XCB и NSS. Их названия зависят от дистрибутива. Не запускайте браузер от `root`; если это неизбежно только для CI, передайте Chromium флаг `--no-sandbox` через `QTWEBENGINE_CHROMIUM_FLAGS`.

Ручная установка:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m browser.main
```

Открыть URL, профиль или приватное окно можно из CLI:

```bash
python -m browser.main https://www.python.org
python -m browser.main --profile profile-xxxxxxxxxxxx
python -m browser.main --incognito
```

## Проверка

```bash
python -m pytest -q
ruff check browser tests
ruff format --check browser tests
python -m browser.main --data-dir .auralis-data/smoke --smoke-test
```

`--smoke-test` создаёт настоящее окно, Chromium profile/page/view, загружает `auralis://newtab` и закрывается автоматически. В CI без дисплея можно использовать `QT_QPA_PLATFORM=offscreen`, но некоторые Windows GPU‑драйверы не поддерживают offscreen Chromium; обычный оконный smoke‑тест остаётся эталонным.

Добавить демонстрационные записи истории:

```bash
python -m browser.main --demo-data
```

Стартовые закладки создаются один раз в новом профиле. Повторный запуск не дублирует данные.

## Архитектура

```text
browser/
├── main.py                         # запуск, logging, DI и asyncio/Qt loop
├── core/
│   ├── browser_engine.py           # WebEngine profiles/pages/views/downloads
│   ├── tabs.py                     # состояние вкладок, групп и session restore
│   ├── profiles.py                 # изолированные пользовательские профили
│   └── security.py                 # URL policy и разрешения сайтов
├── ui/
│   ├── main_window.py              # координатор пользовательских сценариев
│   ├── material_theme.py           # собственная Material 3 design system
│   ├── navigation_bar.py           # omnibox и навигация
│   ├── tabs_bar.py                 # вкладки, drag/drop, pin/group/preview
│   ├── panels.py                   # закладки, история и загрузки
│   ├── settings.py                 # полноценный settings experience
│   ├── dialogs.py                  # Material‑диалоги
│   ├── find_bar.py                 # поиск по странице
│   ├── overlays.py                 # snackbar и tab preview
│   └── start_page.py               # нативный вариант стартовой страницы
├── database/
│   ├── connection.py               # SQLite WAL, migrations и транзакции
│   ├── history.py
│   ├── bookmarks.py
│   ├── downloads.py
│   └── settings.py
├── services/
│   ├── adblock.py                  # EasyList‑совместимый network matcher
│   ├── filter_updater.py           # HTTPS/atomic/async subscriptions
│   ├── extensions.py               # manifest и lifecycle расширений
│   └── sync.py                     # абстрактный sync backend
└── resources/
    ├── newtab.html                 # доверенная auralis:// страница
    ├── starter_filters.txt
    └── app_icon.svg
```

Основные правила архитектуры:

1. `TabManager` не владеет `QWebEngineView`; поэтому сессия сериализуется независимо от GUI/renderer process.
2. Каждый профиль получает отдельный Chromium `QWebEngineProfile` и единый SQLite с короткоживущими thread‑safe соединениями в WAL‑режиме.
3. UI зависит от семантических интерфейсов репозиториев, а sync/extension runtimes — от абстрактных протоколов.
4. Внутренние страницы обслуживает защищённая схема `auralis://`; локальный HTTP‑сервер не запускается.

## Горячие клавиши

| Действие | Клавиши |
|---|---|
| Новая / закрыть / вернуть вкладку | `Ctrl+T` / `Ctrl+W` / `Ctrl+Shift+T` |
| Адресная строка | `Ctrl+L` |
| Закладка | `Ctrl+D` |
| История / загрузки / закладки | `Ctrl+H` / `Ctrl+J` / `Ctrl+Shift+B` |
| Поиск по странице | `Ctrl+F` |
| Масштаб | `Ctrl++` / `Ctrl+-` / `Ctrl+0` |
| Переключение вкладок | `Ctrl+Tab`, `Ctrl+1…9` |
| Приватное окно | `Ctrl+Shift+N` |
| Полный экран / DevTools | `F11` / `F12` |
| Настройки | `Ctrl+,` |

## Данные профиля

По умолчанию:

- Windows: `%LOCALAPPDATA%\AuralisBrowser`
- Linux: `$XDG_DATA_HOME/auralis-browser` или `~/.local/share/auralis-browser`

Путь можно полностью переопределить через `--data-dir`. Пароли сейчас не сохраняются: перед добавлением password manager потребуется системное шифрование Windows Credential Manager/libsecret.

## Границы Qt WebEngine

Auralis — полноценная продуктовая основа, но Qt WebEngine не предоставляет все закрытые API Chrome/Edge:

- Chrome Web Store и бинарная совместимость со всеми Chrome Extensions недоступны. Реализованы manifest loader, lifecycle, разрешения и точка подключения content‑script runtime; background service workers/native messaging требуют отдельного runtime.
- Продолжение частичной загрузки после перезапуска зависит от поддержки сервера и Qt; pause/resume гарантированы в текущей сессии, журнал остаётся после перезапуска.
- Встроенный adblock применяет сетевые правила EasyList. Cosmetic filters (`##`) намеренно не исполняются как произвольный CSS до появления изолированного cosmetic engine.
- Sync не привязан к чужому облаку: реализован проверяемый backend‑контракт, а credentials/OAuth и сервер выбираются при развёртывании продукта.
- Перевод открывается только по команде пользователя и передаёт адрес выбранному сервису перевода.

Официальная EasyList загружается с <https://easylist.to/easylist/easylist.txt> и имеет собственные условия GPLv3/CC BY‑SA. В репозиторий включён только небольшой оригинальный стартовый набор; полная подписка скачивается пользователем командой в меню блокировщика.

## Лицензия

Код Auralis распространяется по MIT License. Содержимое сторонних подписок и посещаемых страниц остаётся под лицензиями их владельцев.

