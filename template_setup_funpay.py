"""Injected into the official FunPayCardinal image for Emerald Host first-run setup."""
import getpass
import os
import sys

import telebot

from first_setup import create_config_obj, default_config
from Utils.cardinal_tools import build_proxy, check_proxy, hash_password, validate_proxy

MARKER = 'EMERALD_TEMPLATE_SETUP_OK'


def secret(prompt):
    try:
        return getpass.getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print('\nНастройка отменена.')
        raise SystemExit(130)


def proxy(prompt):
    while True:
        value = secret(prompt)
        if not value:
            return ''
        try:
            scheme, login, password, ip, port = validate_proxy(value)
            result = build_proxy(scheme, login, password, ip, port)
            print('Проверяю прокси…')
            if not check_proxy({'http': result, 'https': result}):
                print('Прокси не прошёл проверку. Введите другой или оставьте пустым.')
                continue
            return result
        except Exception as error:
            print(f'Неверный формат прокси: {error}')


def main():
    token = str(os.environ.get('BOT_TOKEN') or '').strip()
    if not token:
        print('BOT_TOKEN не передан Emerald Host.')
        return 2

    config = create_config_obj(default_config)
    print('\n=== FunPay Cardinal · первичная настройка Emerald Host ===')
    print('BOT_TOKEN уже передан платформой и повторно вводить его не нужно.\n')

    while True:
        golden_key = secret('golden_key FunPay (32 символа): ')
        if len(golden_key) == 32 and golden_key == golden_key.lower() and ' ' not in golden_key:
            config.set('FunPay', 'golden_key', golden_key)
            break
        print('Неверный формат golden_key. Попробуйте ещё раз.')

    user_agent = input('User-Agent [Enter = стандартный]: ').strip()
    if user_agent:
        config.set('FunPay', 'user_agent', user_agent)

    telegram_proxy = proxy('IPv4-прокси Telegram [Enter = без прокси]: ')
    if telegram_proxy:
        telebot.apihelper.proxy = {'http': telegram_proxy, 'https': telegram_proxy}
        config.set('Telegram', 'proxy', telegram_proxy)

    try:
        username = telebot.TeleBot(token).get_me().username or ''
    except Exception as error:
        print(f'Не удалось проверить Telegram BOT_TOKEN: {error}')
        return 3
    if not username.lower().startswith('funpay'):
        print(f'Оригинальный Cardinal требует, чтобы @username бота начинался с "funpay". Сейчас: @{username}')
        return 4
    print(f'Telegram подключён: @{username}')

    while True:
        password = secret('Пароль Telegram-панели Cardinal (8+ символов, A/a/0): ')
        if len(password) >= 8 and password.lower() != password and password.upper() != password and any(ch.isdigit() for ch in password):
            break
        print('Пароль должен содержать минимум 8 символов, верхний/нижний регистр и цифру.')

    funpay_proxy = proxy('IPv4-прокси FunPay [Enter = без прокси]: ')
    if funpay_proxy:
        config.set('Proxy', 'proxy', funpay_proxy)
        config.set('Proxy', 'enable', '1')
        config.set('Proxy', 'check', '1')

    config.set('Telegram', 'enabled', '1')
    config.set('Telegram', 'token', token)
    config.set('Telegram', 'secretKeyHash', hash_password(password))
    os.makedirs('configs', exist_ok=True)
    with open('configs/_main.cfg', 'w', encoding='utf-8') as file:
        config.write(file)

    print('\nНастройка сохранена в постоянном volume. Вернитесь в панель Emerald Host и нажмите «Запустить».')
    print(MARKER, flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
