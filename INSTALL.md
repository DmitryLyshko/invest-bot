# Установка и запуск на Linux-сервере

---

## Содержание

1. [Требования](#требования)
2. [Установка системных зависимостей](#установка-системных-зависимостей)
3. [Создание пользователя](#создание-пользователя)
4. [Установка MySQL](#установка-mysql)
5. [Установка бота](#установка-бота)
6. [Конфигурация](#конфигурация)
7. [Запуск через systemd](#запуск-через-systemd)
8. [Проверка работы](#проверка-работы)
9. [Обновление бота](#обновление-бота)
10. [Полезные команды](#полезные-команды)

---

## Требования

- Ubuntu 22.04 / Debian 12 (или совместимый дистрибутив)
- Python 3.11+
- MySQL 8.0+
- Минимум 512 МБ RAM, 1 ГБ свободного места
- T-Invest API токен с правами **Торговые операции**

---

## Установка системных зависимостей

```bash
sudo apt update && sudo apt upgrade -y

# Python 3.11 и утилиты
sudo apt install -y python3.11 python3.11-venv python3.11-dev python3-pip

# Прочие зависимости
sudo apt install -y git curl build-essential pkg-config libssl-dev default-libmysqlclient-dev
```

Проверить версию Python:

```bash
python3.11 --version
# Python 3.11.x
```

---

## Создание пользователя

Запускать бота от отдельного непривилегированного пользователя — обязательно.

```bash
sudo useradd -m -s /bin/bash tradingbot
sudo su - tradingbot
```

Все дальнейшие команды выполняются от имени пользователя **tradingbot**.

---

## Установка MySQL

```bash
# Вернуться в root или sudo-пользователя
exit

sudo apt install -y mysql-server
sudo systemctl enable --now mysql

# Безопасная первичная настройка
sudo mysql_secure_installation
```

Создать базу данных и пользователя:

```bash
sudo mysql -u root -e "
CREATE DATABASE trading_bot CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'trading_user'@'localhost' IDENTIFIED BY 'ЗАМЕНИТЕ_НА_СВОЙ_ПАРОЛЬ';
GRANT ALL PRIVILEGES ON trading_bot.* TO 'trading_user'@'localhost';
FLUSH PRIVILEGES;
"
```

> Запомните пароль — он понадобится в `.env`.

---

## Установка бота

```bash
sudo su - tradingbot

# Клонировать репозиторий
git clone <url-репозитория> ~/invest-bot
cd ~/invest-bot

# Создать виртуальное окружение
python3.11 -m venv .venv
source .venv/bin/activate

# Установить зависимости
pip install --upgrade pip
pip install -r requirements.txt
```

---

## Конфигурация

### .env

```bash
cp .env.example .env
nano .env
```

Заполнить все поля:

```dotenv
# T-Invest API токен
TINKOFF_TOKEN=t.ваш_токен_здесь

# MySQL
MYSQL_URL=mysql+pymysql://trading_user:ЗАМЕНИТЕ_НА_СВОЙ_ПАРОЛЬ@localhost:3306/trading_bot

# Flask (веб-дашборд)
WEB_SECRET_KEY=случайная_строка_минимум_32_символа
WEB_USERNAME=admin
WEB_PASSWORD=сложный_пароль_для_дашборда

# Хост и порт дашборда
# 127.0.0.1 — только локально (если есть nginx-прокси)
# 0.0.0.0   — доступен снаружи напрямую
WEB_HOST=127.0.0.1
WEB_PORT=5000

# Риск: лимит дневных убытков (отрицательное число, рублей)
DAILY_LOSS_LIMIT_RUB=-500.0

# Уровень логов
LOG_LEVEL=INFO

# Sandbox: true = тестовые ордера, false = реальная торговля
SANDBOX=true
```

> Для генерации `WEB_SECRET_KEY`:
> ```bash
> python3 -c "import secrets; print(secrets.token_hex(32))"
> ```

### instruments.yaml

Настройки торговых инструментов находятся в `trading_bot/config/instruments.yaml`.
Пример уже есть в репозитории. Для добавления нового тикера — только туда, без правок кода.

---

## Запуск через systemd

Создать unit-файл (от sudo-пользователя, не tradingbot):

```bash
exit  # вернуться из tradingbot

sudo nano /etc/systemd/system/trading-bot.service
```

Содержимое файла:

```ini
[Unit]
Description=Trading Bot (OFI + Large Print)
After=network.target mysql.service
Requires=mysql.service

[Service]
Type=simple
User=tradingbot
Group=tradingbot
WorkingDirectory=/home/tradingbot/invest-bot
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/tradingbot/invest-bot/.venv/bin/python -m trading_bot.main
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Включить и запустить:

```bash
sudo systemctl daemon-reload
sudo systemctl enable trading-bot
sudo systemctl start trading-bot
```

---

## Проверка работы

### Статус сервиса

```bash
sudo systemctl status trading-bot
```

### Логи в реальном времени

```bash
sudo journalctl -u trading-bot -f
```

### Логи бота (файл)

```bash
tail -f /home/tradingbot/invest-bot/logs/bot.log
```

### Веб-дашборд

Если `WEB_HOST=127.0.0.1`, дашборд доступен только с самого сервера.
Для внешнего доступа — настроить nginx (см. ниже) или временно:

```bash
# SSH-туннель с локальной машины:
ssh -L 5000:127.0.0.1:5000 user@адрес_сервера
# Затем открыть http://127.0.0.1:5000 в браузере
```

### Nginx (опционально, для постоянного доступа)

```bash
sudo apt install -y nginx

sudo nano /etc/nginx/sites-available/trading-bot
```

```nginx
server {
    listen 80;
    server_name ваш_домен_или_ip;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/trading-bot /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

---

## Обновление бота

```bash
sudo su - tradingbot
cd ~/invest-bot

# Остановить сервис перед обновлением
sudo systemctl stop trading-bot

git pull origin main
source .venv/bin/activate
pip install -r requirements.txt

sudo systemctl start trading-bot
sudo systemctl status trading-bot
```

---

## Полезные команды

| Действие | Команда |
|---|---|
| Запустить бота | `sudo systemctl start trading-bot` |
| Остановить бота | `sudo systemctl stop trading-bot` |
| Перезапустить | `sudo systemctl restart trading-bot` |
| Статус | `sudo systemctl status trading-bot` |
| Логи (live) | `sudo journalctl -u trading-bot -f` |
| Логи за сегодня | `sudo journalctl -u trading-bot --since today` |
| Войти в MySQL | `mysql -u trading_user -p trading_bot` |
| P&L за сегодня | `SELECT SUM(pnl_rub) FROM trades WHERE DATE(close_at) = CURDATE();` (в MySQL) |

---

## Примечания по безопасности

- Файл `.env` должен быть доступен только владельцу: `chmod 600 .env`
- Не открывать порт 5000 напрямую в интернет — только через nginx с паролем или SSH-туннель
- Использовать `SANDBOX=true` до тех пор, пока стратегия не проверена на реальных данных
- Дашборд защищён паролем из `WEB_USERNAME` / `WEB_PASSWORD`, но HTTPS настраивается отдельно (через certbot + nginx)
