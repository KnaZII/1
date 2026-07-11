---
title: Установка
description: Установите Meridian CLI на ваш локальный компьютер.
order: 2
section: guides
---

## Быстрая установка

```
curl -sSf https://getmeridian.org/install.sh | bash
```

Этот скрипт:
1. Устанавливает [uv](https://docs.astral.sh/uv/) если его нет (или использует pipx как резервный вариант)
2. Устанавливает `meridian-vpn` с PyPI
3. Создаёт символическую ссылку в `/usr/local/bin/meridian` для системного доступа
4. Мигрирует из старого bash-основанного CLI если он присутствует

## Ручная установка

С uv (рекомендуется):
```
uv tool install meridian-vpn
```

С pipx:
```
pipx install meridian-vpn
```

## Обновить

```
meridian update
```

Meridian проверяет обновления автоматически:
- **Версии patch** (исправления ошибок) — устанавливаются молча
- **Версии minor** (новые функции) — вам предложат обновиться
- **Версии major** (критические изменения) — вам предложат обновиться

## Требования

- **Python 3.10+** (устанавливается автоматически uv/pipx)
- **SSH ключ доступ** к целевому серверу
- **qrencode** (опционально, для QR-кодов в терминале): `brew install qrencode` или `apt install qrencode`

## Проверить установку

```
meridian --version
```
