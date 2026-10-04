# Подключение к профилям Hermes

Локальное подключение Hermes Link использует штатный `mcp_servers` выбранного
профиля. Hermes сам запускает сервер по stdio, обнаруживает его инструменты и
закрывает процесс при завершении клиента. Включение directory plugin на вкладке
Plugins само по себе не подключает эти инструменты к агенту.

## Подключить профиль

После установки плагина:

```sh
cd "$HOME/.hermes/plugins/http-mcp"
"$HOME/.hermes/hermes-agent/venv/bin/python" -m hermes_bridge.profile_connection \
  --config "$HOME/Library/Application Support/HermesHTTPMCP/config.json" \
  --profile default
```

Замените `default` именем существующего профиля. Команда сохраняет подключение
`hermes-link` через native settings writer, с проверкой текущей ревизии и
приватными версиями на диске. Повторное подключение с теми же параметрами
ничего не меняет. Существующее чужое подключение с таким именем не заменяется.
Профиль должен входить в allowlist bridge config.

Начните новый чат выбранного профиля. В Kanban назначьте профиль исполнителем:
следующий worker использует те же MCP-настройки и список CLI tools. Изменение
подключения не пересобирает инструменты уже работающего чата или worker.

При вызове инструмента без `params.profile` используется подключённый
профиль. Явный `params.profile` позволяет работать с другим разрешённым профилем.
`agent` у запуска задачи и `board` у действий Kanban остаются параметрами
внутри `params`. Владелец политики bridge и его Kanban store по-прежнему задаются
через `board_profile`; подключение потребителя не переносит эти настройки.

Профиль получает тот же компактный набор из 10 инструментов, что и HTTP-клиент.
Например, `hermes_settings(action="get")` читает настройки выбранного профиля,
а `hermes_settings(action="help", params={"action":"update"})` возвращает
схему действия с этим профилем по умолчанию. Формат всех групп описан в
[compact-tools.md](compact-tools.md).

## Настройки в Desktop

В разделе возможностей откройте **Connectors**, выберите нужный профиль и
найдите `hermes-link`. Штатная форма позволяет проверить, включить или удалить
MCP. Для ручного добавления выберите local/stdio и используйте такую конфигурацию
с абсолютными путями и именем профиля:

```yaml
mcp_servers:
  hermes-link:
    command: /absolute/hermes-agent/venv/bin/python
    args:
      - -m
      - hermes_bridge.main
      - --config
      - /absolute/HermesHTTPMCP/config.json
      - --transport
      - stdio
      - --profile
      - my-profile
    cwd: /absolute/.hermes/plugins/http-mcp
    env:
      HERMES_HOME: /absolute/.hermes
      HERMES_KANBAN_DB: ""
      HERMES_KANBAN_BOARD: ""
      HERMES_KANBAN_HOME: ""
      HERMES_KANBAN_WORKSPACES_ROOT: ""
      HERMES_KANBAN_ATTACHMENTS_ROOT: ""
    enabled: true
    trust: full
```

Команда подключения заполняет эти пути автоматически. Отдельная установка
directory plugin в каждый профиль не требуется: MCP запускает общий установленный
пакет. Права и отключённые инструменты читаются из настроек владельца bridge.
Подключение очищает переданные worker пути доски: `board_profile` и явный
аргумент `board` сохраняют свою область действия. При наличии нестандартного
managed scope команда также переносит его путь `HERMES_MANAGED_DIR` в
окружение MCP-процесса, сохраняя ограничения администратора.
При ручной настройке добавьте этот путь в `env`, если он задан в установке
Hermes. Штатные ограничения действий дочерних агентов сохраняются.

По умолчанию `trust: full` позволяет выбранному агенту вызывать все разрешённые
инструменты, включая изменения при фоновом запуске из Kanban. Для запросов
с подтверждением используйте `--trust untrusted`: чтение работает сразу,
изменения требуют согласия через штатный механизм Hermes. У worker без
интерактивного ввода такие изменения могут быть отклонены.

Явное `no_mcp` или отключение `hermes-link` в `agent.disabled_toolsets`
сохраняются; команда сообщает, что подключение блокируется. Если профиль уже
ограничивает MCP явным списком серверов, команда добавляет в него только
`hermes-link`, сохраняя остальные инструменты.

Stdio доступен через процесс текущего пользователя; сетевого слушателя у него
нет. Проверка Bearer-токена по умолчанию остаётся включённой для HTTP-клиентов.
Allowlist профилей/досок, выключенные инструменты и запрет изменений действуют
для обоих транспортов. Каждый stdio-клиент имеет собственный лимит активных
запусков; `max_concurrent_runs` ограничивает процесс bridge, а не всю машину.

## Удаление и откат

```sh
"$HOME/.hermes/hermes-agent/venv/bin/python" -m hermes_bridge.profile_connection \
  --config "$HOME/Library/Application Support/HermesHTTPMCP/config.json" \
  --profile my-profile --disconnect
```

Другие серверы, настройки и `.env` сохраняются. Ревизия и версии до/после
изменения возвращаются в результате команды. Для точного отката используйте
`hermes_settings(action="versions", params={"profile":"my-profile"})`, затем
`hermes_settings(action="restore", params={...})` с `profile`, `version_id`
и `expected_revision`
через остающийся MCP-клиент. Версии хранятся в native `backups/config` профиля.
Если удаляется последний явно выбранный MCP-сервер, сохраняется `no_mcp`,
чтобы Hermes не включил вместо него остальные невыбранные серверы.

## Проверка

Проверить подключение конкретного профиля штатным Hermes CLI:

```sh
hermes -p my-profile mcp test hermes-link
```

Из исходного проекта можно повторить изолированную проверку обоих потребителей:

```sh
"$HOME/.hermes/hermes-agent/venv/bin/python" -m tests.native_profile_probe \
  --hermes-repo "$HOME/.hermes/hermes-agent"
```

Проверка использует временные профили A → B → A, реальный MCP-процесс и штатный
dispatch инструментов. Для чата и Kanban проверяются обнаружение всех 10
инструментов, чтение и изменение настройки выбранного профиля, ревизия и
сохранность соседних профилей. Модель и рабочие задачи не запускаются.
