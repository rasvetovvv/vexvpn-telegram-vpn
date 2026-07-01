# Развёртывание на VPS (Ubuntu 22.04 / 24.04)

Полный путь: сервер → Docker → Marzban (панель + Xray) → SSL → бот.
Везде заменяй:

- `vpn.example.com` → твой домен (поддомен для VPN)
- `admin@example.com` → твой email (для Let's Encrypt)
- `123.123.123.123` → IP твоего сервера

---

## 0. Что нужно заранее

1. **VPS** с Ubuntu 22.04/24.04, минимум 1 vCPU / 1 ГБ RAM, root-доступ по SSH.
2. **Домен** (любой регистратор). В DNS добавь **A-запись**:
   `vpn.example.com → 123.123.123.123` (без Cloudflare-проксирования на первом
   этапе — «серое облако», DNS only).
3. **Токен бота** от [@BotFather](https://t.me/BotFather) и свой Telegram ID
   от [@userinfobot](https://t.me/userinfobot).

Подключись к серверу:

```bash
ssh root@123.123.123.123
```

---

## 1. Базовая настройка сервера

```bash
apt update && apt upgrade -y
apt install -y curl git ufw socat

# Фаервол
ufw allow 22/tcp      # SSH
ufw allow 80/tcp      # выдача SSL-сертификата
ufw allow 443/tcp     # VPN (Xray Reality)
ufw allow 8002/tcp    # панель Marzban + ссылки-подписки
ufw --force enable
```

---

## 2. Установка Docker

```bash
curl -fsSL https://get.docker.com | sh
docker --version
docker compose version
```

---

## 3. Установка Marzban

```bash
sudo bash -c "$(curl -sL https://github.com/Gozargah/Marzban-scripts/raw/master/marzban.sh)" @ install
```

После установки:
- конфиг: `/opt/marzban/.env`
- данные: `/var/lib/marzban/`
- команда управления: `marzban` (up / down / restart / logs / status / cli)

Проверь, что запустилось:

```bash
marzban status
```

---

## 4. SSL-сертификат (Let's Encrypt)

Сертификат нужен, чтобы ссылки-подписки работали по HTTPS (этого требует Happ
и Telegram).

```bash
apt install -y certbot
mkdir -p /var/lib/marzban/certs

certbot certonly --standalone --non-interactive --agree-tos \
  -m admin@example.com -d vpn.example.com \
  --deploy-hook "cp /etc/letsencrypt/live/vpn.example.com/fullchain.pem /var/lib/marzban/certs/ && cp /etc/letsencrypt/live/vpn.example.com/privkey.pem /var/lib/marzban/certs/ && marzban restart"

# первая ручная копия (deploy-hook отрабатывает только при продлении)
cp /etc/letsencrypt/live/vpn.example.com/fullchain.pem /var/lib/marzban/certs/
cp /etc/letsencrypt/live/vpn.example.com/privkey.pem  /var/lib/marzban/certs/
```

> Сертификат продлевается автоматически (systemd-таймер `certbot`), при продлении
> deploy-hook скопирует новый cert и перезапустит Marzban.

---

## 5. Конфиг Xray — VLESS + Reality

### 5.1 Сгенерируй ключи

```bash
# пара ключей Reality (Marzban сам вычислит публичный ключ для клиентов)
docker compose -f /opt/marzban/docker-compose.yml exec -T marzban xray x25519

# короткий идентификатор
openssl rand -hex 8
```

Запиши **Private key** и **short id** — они нужны ниже.

### 5.2 Создай файл конфигурации

```bash
nano /var/lib/marzban/xray_config.json
```

Вставь (подставь `PRIVATE_KEY_СЮДА` и `SHORT_ID_СЮДА`):

```json
{
  "log": { "loglevel": "warning" },
  "inbounds": [
    {
      "tag": "VLESS_REALITY",
      "listen": "0.0.0.0",
      "port": 443,
      "protocol": "vless",
      "settings": { "clients": [], "decryption": "none" },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "dest": "www.microsoft.com:443",
          "xver": 0,
          "serverNames": ["www.microsoft.com"],
          "privateKey": "PRIVATE_KEY_СЮДА",
          "shortIds": ["SHORT_ID_СЮДА"]
        }
      },
      "sniffing": { "enabled": true, "destOverride": ["http", "tls"] }
    }
  ],
  "outbounds": [
    { "protocol": "freedom", "tag": "DIRECT" },
    { "protocol": "blackhole", "tag": "BLOCK" }
  ]
}
```

> `clients` оставляем пустым — пользователей в inbound добавляет сам Marzban,
> когда бот создаёт подписку. `flow` у пользователей будет `xtls-rprx-vision`
> (см. `.env` бота) — это штатно для Reality + Vision.

---

## 6. Настройка Marzban (.env)

```bash
nano /opt/marzban/.env
```

Убедись, что заданы (раскомментируй/добавь строки):

```ini
UVICORN_HOST=0.0.0.0
UVICORN_PORT=8002
UVICORN_SSL_CERTFILE=/var/lib/marzban/certs/fullchain.pem
UVICORN_SSL_KEYFILE=/var/lib/marzban/certs/privkey.pem

XRAY_JSON=/var/lib/marzban/xray_config.json
XRAY_SUBSCRIPTION_URL_PREFIX=https://vpn.example.com:8002
```

Перезапусти и проверь логи:

```bash
marzban restart
marzban logs        # Ctrl+C чтобы выйти. Ошибок Xray быть не должно.
```

---

## 7. Админ Marzban (логин для бота)

```bash
marzban cli admin create --sudo
```

Введи **username** и **password** — запомни их, они пойдут в `.env` бота.

Проверь панель в браузере: `https://vpn.example.com:8002/dashboard/`
(войди созданным админом — замок должен быть «зелёным», без ошибок сертификата).

### Проверь Host в панели
Dashboard → раздел **Hosts** (или Host Settings). Для inbound `VLESS_REALITY`
адрес (Address) должен быть `vpn.example.com` или `123.123.123.123`, SNI —
`www.microsoft.com` (совпадает с `serverNames`). Обычно подставляется само —
просто убедись, что поле Address не пустое.

---

## 8. Деплой бота

### 8.1 Залей проект на сервер

Вариант A — **через git** (если выложишь проект в свой репозиторий):

```bash
cd /opt
git clone https://github.com/ТВОЙ_АККАУНТ/vpn-bot.git vpn-bot
cd /opt/vpn-bot
```

Вариант B — **скопировать с Windows** (выполнять в PowerShell на своём ПК):

```powershell
cd "C:\Users\losha\OneDrive\Рабочий стол"
scp -r .\vpn root@123.123.123.123:/opt/vpn-bot
```

### 8.2 Настрой .env бота

```bash
cd /opt/vpn-bot
cp .env.example .env
nano .env
```

Заполни:

```ini
BOT_TOKEN=123456789:AA...               # от @BotFather
ADMIN_IDS=ТВОЙ_TELEGRAM_ID

DATABASE_URL=postgresql+asyncpg://vpnbot:vpnbot@db:5432/vpnbot

MARZBAN_BASE_URL=https://vpn.example.com:8002
MARZBAN_USERNAME=АДМИН_ИЗ_ШАГА_7
MARZBAN_PASSWORD=ПАРОЛЬ_ИЗ_ШАГА_7
MARZBAN_PROXIES=vless
MARZBAN_FLOW=xtls-rprx-vision

SUPPORT_USERNAME=твой_username_поддержки
SERVERS_ONLINE=12
```

### 8.3 Дай контейнеру бота достучаться до Marzban

Бот обращается к Marzban по домену `vpn.example.com:8002`. Чтобы запрос из
контейнера гарантированно попадал на этот же сервер (а не «наружу и обратно»),
добавь в `docker-compose.yml` в сервис `bot` блок `extra_hosts`:

```bash
nano docker-compose.yml
```

```yaml
  bot:
    build: .
    container_name: vpnbot_app
    restart: unless-stopped
    env_file: .env
    extra_hosts:
      - "vpn.example.com:host-gateway"     # ← добавь эту пару строк
    depends_on:
      db:
        condition: service_healthy
```

### 8.4 Запуск

```bash
docker compose up -d --build
docker compose logs -f bot
```

В логах должно появиться: `Бот @ИМЯ запущен`.

---

## 9. Проверка «всё работает»

1. Напиши боту `/start` в Telegram → появится меню.
2. **🚀 Купить VPN** → выбери тариф `⭐ 10 — 1 день` (тест).
3. Оплати звёздами (для теста у тебя на аккаунте должны быть Stars; их можно
   купить в Telegram).
4. После оплаты бот пришлёт ссылку-подписку
   `https://vpn.example.com:8002/sub/...`.
5. В панели Marzban (`/dashboard/`) появится пользователь `tg_<твой_id>`.
6. Открой ссылку в браузере — отдаётся конфиг. Вставь её в **Happ** →
   подключись → проверь, что интернет идёт через VPN (например, на
   `whoer.net` IP должен быть серверным).

---

## 10. Полезные команды

```bash
# Бот
cd /opt/vpn-bot
docker compose logs -f bot          # логи бота
docker compose restart bot          # перезапуск
docker compose down                 # остановить
docker compose up -d --build        # пересобрать и поднять (после изменений)

# Marzban
marzban status
marzban logs
marzban restart
marzban cli admin create --sudo     # ещё один админ
docker compose -f /opt/marzban/docker-compose.yml exec marzban xray x25519

# База бота (Postgres внутри его compose)
docker compose -f /opt/vpn-bot/docker-compose.yml exec db psql -U vpnbot -d vpnbot -c "\dt"
```

---

## 11. Частые проблемы

| Симптом | Причина / решение |
|---|---|
| Бот: `Авторизация не удалась` в логах | Неверные `MARZBAN_USERNAME/PASSWORD` или недоступен `MARZBAN_BASE_URL`. Проверь шаг 7 и `extra_hosts` (8.3). |
| Оплата прошла, но бот пишет про поддержку | Marzban недоступен/упал Xray. Смотри `marzban logs` (опечатка в `xray_config.json`, неверный privateKey). |
| Ошибка сертификата при заходе на `:8002` | Cert не скопирован в `/var/lib/marzban/certs/` или `UVICORN_SSL_*` не заданы. Повтори шаги 4 и 6. |
| `VLESS_REALITY` не стартует | Порт 443 занят. Убедись, что панель на `8002`, а не на `443`. |
| Подписка не подключается в Happ | В панели Hosts проверь Address/SNI (шаг 7), и что A-запись домена указывает на сервер. |
| Бот не реагирует на `/start` | Неверный `BOT_TOKEN`, или контейнер не запущен: `docker compose logs bot`. |

---

## Итоговая топология

```
                 Telegram
                    │
        ┌───────────┴───────────┐
        │   бот (Docker)        │  /opt/vpn-bot  → Postgres (в том же compose)
        │   опрашивает Telegram │
        └───────────┬───────────┘
                    │ HTTPS API (vless создаётся/продляется)
                    ▼
        ┌───────────────────────┐
        │   Marzban :8002 (SSL)  │  /opt/marzban
        │   панель + /sub/...    │
        └───────────┬───────────┘
                    │ управляет
                    ▼
        ┌───────────────────────┐
        │   Xray-core :443       │  VLESS + Reality
        │   (сам VPN-трафик)     │
        └───────────────────────┘
```
