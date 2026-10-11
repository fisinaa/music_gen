"""Local token setup; never accepts credentials through chat or command arguments."""
import getpass
import os
from pathlib import Path
from vk_upload import api, VKError, save_json


def main():
    root = Path(__file__).resolve().parent
    folder = Path(os.environ.get('LOFI_WEB_DATA', str(root / 'web_data'))).expanduser() / 'vk'
    print('Нужен пользовательский токен VK API с правом video, полученный через своё доверенное приложение.')
    print('Обычный ключ сообщества и токен входа VK ID не заменяют токен доступа к video.save.')
    token = getpass.getpass('Токен (ввод скрыт): ').strip()
    if not token:
        raise VKError('Токен пуст')
    users = api(token, 'users.get')
    if not isinstance(users, list) or len(users) != 1:
        raise VKError('Нужен пользовательский токен VK API')
    user = users[0]
    group = input('Числовой ID сообщества без минуса (Enter — личная страница): ').strip()
    if group:
        if not group.isdigit() or int(group) <= 0:
            raise VKError('Нужен положительный числовой ID')
        result = api(token, 'groups.getById', group_ids=group, fields='is_admin')
        groups = result.get('groups', []) if isinstance(result, dict) else result
        if not groups or not groups[0].get('is_admin'):
            raise VKError('Нужны права администратора выбранного сообщества')
        target = {'id': -int(groups[0]['id']), 'title': groups[0]['name']}
    else:
        target = {'id': int(user['id']), 'title': user['first_name'] + ' ' + user.get('last_name', '')}
    # Read-only check: no placeholder video created during connection.
    api(token, 'video.get', owner_id=target['id'], count=1)
    print(f"Публикации: {target['title']} ({target['id']}). Видео будут открытыми с учётом доступа к сообществу.")
    if input('Сохранить подключение? Напиши YES: ').strip() != 'YES':
        print('Отменено'); return
    save_json(folder / 'token.json', {'access_token': token, 'target': target})
    print('Подключение сохранено. Открой вкладку VK Видео. Право загрузки окончательно проверяется при первой отправке.')

if __name__ == '__main__':
    try:
        main()
    except VKError as exc:
        raise SystemExit(str(exc))
    except (Exception, KeyboardInterrupt):
        raise SystemExit('Подключение не завершено. Проверь токен, права и сеть; секреты не выведены.')
