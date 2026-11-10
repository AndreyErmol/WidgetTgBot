# WidgetTgBot — котировки

Python 3.10+, без pip-зависимостей. `/widget` или `/prices` присылает HTML-виджет BTC, ETH, SOL и USD/RUB.

## Источники

Крипта: Coinbase Exchange `/products/{BTC,ETH,SOL}-USD/stats`, цена `last`, изменение за 24 ч `(last/open - 1)*100`. Это USD, не USDT, и цена одной биржи, не индекс.

USD/RUB: Coinbase Data API `/v2/exchange-rates?currency=USD`, поле `data.rates.RUB`. Справочный курс Coinbase, не ЦБ, не банковский курс и не P2P. API не гарантирует свежую котировку раз в 10 секунд и не возвращает её timestamp. Показываем время получения, не время сделки. Процент изменения для USD/RUB не придумываем. Ключ Coinbase не нужен; доступ зависит от сети/региона.

## Обновление

Фоновый поток раз в 10 секунд запускает четыре параллельных запроса (таймаут 8 с), публикует общее состояние через `editSharedWidgetData`, `cache_time=10`. Виджет вызывает `watchPublicData()`: клиент опрашивает Telegram, пока виджет виден. Это polling, не WebSocket: опросы независимы, возможны дополнительные задержки. Циклы не перекрываются; при долгом ответе следующий начинается после завершения предыдущего.

Ошибки отдельных источников не удаляют последние цены: строки помечаются устаревшими. Через 30 секунд без получения данных виджет сам показывает устаревание, даже если бот остановлен. LIVE означает недавние успешные опросы, не гарантированную свежесть исходных котировок. Состояние содержит ревизию для защиты от старых ответов. Время отображается в локальной зоне устройства.

## macOS / zsh

```zsh
git clone https://github.com/AndreyErmol/WidgetTgBot.git
cd WidgetTgBot
read -r -s 'BOT_TOKEN?Вставь токен бота: '; echo
export BOT_TOKEN
python3 bot.py
```

Токен получите у @BotFather. Не публикуйте его. Необязательно: `export ALLOWED_USER_ID=123456789` со своим ID. Без ограничения бот отвечает всем в ЛС. `.env` автоматически не читается.

Для обновления: Ctrl+C, `git pull --ff-only`, `python3 bot.py` в том же терминале. Отправьте новый `/widget`: старый HTML-счётчик не заменяется, HTML отправленного виджета неизменяем.

Бот должен работать постоянно. Первый снимок может появиться через несколько секунд. Только один процесс на токен/каталог. Webhook автоматически не удаляется. Production widgets доступны только в ЛС; если видна лишь подпись, нужен клиент с поддержкой widgets.

## Сохранение и диагностика

Первый `sendWidget(is_shared=True)` сохраняет ID в `runtime.json` (без токена, исключён из Git). Последующие команды используют `sendSharedWidget`: один поток для всех новых сообщений. Перезапуск возобновляет этот поток. Изменение HTML создаёт новый shared widget при следующей команде; старые макеты перестают обновляться. Потеря runtime.json также создаёт новый поток; автоматического восстановления старого ID нет.

Сетевые ошибки логируются с причиной; токен скрывается в ошибках Telegram. `getUpdates` повторяется через 5 секунд. Проверка TLS включена. Offset хранится в памяти, после сбоя возможна повторная обработка команды.

```sh
python3 -m unittest -v
```

Семь тестов с mock API не требуют сети/токена. Живой end-to-end тест нового shared widget при подготовке не выполнялся.

## Официальная документация

- https://core.telegram.org/bots/widgets
- https://core.telegram.org/bots/api#sendwidget
- https://core.telegram.org/bots/api#editsharedwidgetdata
- https://core.telegram.org/bots/api#sendsharedwidget
- https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-stats
- https://docs.cdp.coinbase.com/coinbase-business/track-apis/exchange-rates
