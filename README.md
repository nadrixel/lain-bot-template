<div align="center">

# 🤖 lain-core

**Открытый модульный шаблон Telegram-бота с интеграцией Google Gemini API**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-2CA5E0?style=flat-square&logo=telegram&logoColor=white)](https://aiogram.dev)
[![Gemini](https://img.shields.io/badge/Google_Gemini-API-4285F4?style=flat-square&logo=google&logoColor=white)](https://aistudio.google.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)

</div>

---

## ✨ Особенности

| Возможность | Описание |
|---|---|
| **Zero-Token Background** | Ежедневные сводки и планировщик работают **без вызовов LLM** — никаких токенов в холостую |
| **Локальная SQLite** | Все данные хранятся локально, зависимость от облачных БД отсутствует |
| **PDF / DOCX** | Бот умеет читать и резюмировать загруженные документы |
| **Три режима поведения** | Админ (краткий и деловой), Обычный (дружелюбный ИИ-ассистент), ЧС (саркастичный тролль-режим) |
| **Авто-фоллбэк моделей** | При исчерпании квоты одной модели Gemini бот автоматически переключается на следующую |
| **Inline Task Tracker** | Встроенный трекер задач и дедлайнов с ежедневной сводкой по расписанию |

---

## 🚀 Быстрый старт

### 1. Клонировать репозиторий

```bash
git clone https://github.com/your-username/lain-core.git
cd lain-core
```

### 2. Создать и активировать виртуальное окружение

```bash
# Linux / macOS / Raspberry Pi
python3 -m venv venv
source venv/bin/activate

# Windows
python -m venv venv
venv\Scripts\activate
```

Установить зависимости:

```bash
pip install -r requirements.txt
```

### 3. Настроить переменные окружения

```bash
cp .env.example .env
nano .env   # или откройте в любом редакторе
```

Заполните все поля в `.env` (см. комментарии внутри файла).

### 4. Запустить бота

```bash
python main.py
```

---

## 🧩 Структура проекта

```
lain-core/
├── main.py          # Весь код бота (единый файл)
├── requirements.txt # Зависимости
├── .env.example     # Шаблон конфигурации
└── .gitignore       # Список исключений для Git
```

---

## ⚙️ Архитектура Zero-Token

Ежедневная сводка задач отправляется по расписанию через APScheduler.  
Данные берутся **напрямую из SQLite** — ни один токен Gemini не тратится на фоновые процессы.  
LLM вызывается **только в ответ на явное обращение пользователя**.

---

## 🤖 Режимы поведения

- **Admin mode** — лаконично, профессионально, с лёгкой иронией
- **Normal mode** — дружелюбный ИИ-ассистент, отвечает на вопросы и работает с документами
- **Blacklist mode** — саркастичные ответы для заблокированных пользователей (тролль-режим)

Управление через команды `/ban`, `/unban`, `/bans` (только для ADMIN_ID).

---

## 🛠️ Команды бота

| Команда | Кто | Описание |
|---|---|---|
| `/help` | Все | Показать справку |
| `/menu` | Все | Открыть главное меню |
| `/ban [ID]` | Админ | Добавить пользователя в ЧС |
| `/unban [ID]` | Админ | Убрать из ЧС |
| `/bans` | Админ | Список ЧС |
| `/say <текст>` | Админ | Отправить сообщение в группу от имени бота |
| `/summary_on` | Админ | Включить ежедневную сводку |
| `/summary_off` | Админ | Выключить ежедневную сводку |
| `/system_test` | Админ | Диагностика компонентов системы |

---

## 🐧 Запуск как демон на Linux / Raspberry Pi

Чтобы бот работал постоянно и автоматически перезапускался после перезагрузки — создайте systemd-службу.

**1. Узнайте путь к Python в вашем виртуальном окружении:**

```bash
which python  # после активации venv
# Пример: /home/pi/lain-core/venv/bin/python
```

**2. Создайте файл службы:**

```bash
sudo nano /etc/systemd/system/lain-bot.service
```

Вставьте содержимое (замените пути и пользователя на свои):

```ini
[Unit]
Description=Lain Telegram Bot
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/lain-core
ExecStart=/home/pi/lain-core/venv/bin/python main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**3. Активируйте и запустите службу:**

```bash
sudo systemctl daemon-reload
sudo systemctl enable lain-bot
sudo systemctl start lain-bot
```

**4. Проверьте статус:**

```bash
sudo systemctl status lain-bot
# Логи в реальном времени:
sudo journalctl -u lain-bot -f
```

---

## 📋 Требования

- Python 3.11+
- Telegram Bot Token ([@BotFather](https://t.me/BotFather))
- Google Gemini API Key ([Google AI Studio](https://aistudio.google.com))

---

## 📄 Лицензия

MIT — используйте, форкайте, улучшайте.
