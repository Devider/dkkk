Bugfix (вне story) · найдено при реализации S0.1 (`s0-1-pin-dependencies`, задача 5.1) · **Отложено** 2026-10-26

## Why

`ruff` находит в `src/aigw_service/context.py` пять неопределённых имён (F821). Два из них ломают запуск сервиса при включённых интеграциях: `AppContext.__init__` создаёт `PlatformVSearch(...)` при `secrets.platform_v_search.enabled` и `IDPService(...)` при `secrets.idp.enabled`, но эти классы не импортированы и в репозитории не существуют. При включении любой из интеграций сервис падает на старте с `NameError`.

Происхождение: код шаблона `rest_template` ссылается на классы закрытого пакета `sber-aigw`; локальные заглушки `src/aigw_modules/` (CLAUDE.md, «Private dep stub») их не содержат. Импорта не было с первого коммита (`57276dc`). Третье имя, `LangfuseClient`, используется только в аннотации и на работу не влияет; класс есть в `aigw_modules.hub_services.langfuse`.

## What Changes

- `LangfuseClient` — добавить импорт из `aigw_modules.hub_services.langfuse` (аннотация становится корректной).
- `PlatformVSearch`, `IDPService` — выбрать при реализации один из вариантов:
  - (а) добавить заглушки в `src/aigw_modules/hub_services/` по образцу существующих (`pangolin.py`, `langfuse.py`), чтобы включение интеграции давало понятную ошибку «недоступно вне контура», а не `NameError`;
  - (б) при `enabled=True` выбрасывать явную ошибку конфигурации в `AppContext.__init__`, не создавая объект.
  **Выбрано (б)** разработчиком 2026-10-26: включение интеграции останавливает старт сервиса явной ошибкой `IntegrationUnavailableError` с именем интеграции и переменной окружения. Неработающий код создания клиентов удаляется.
- Тест: включённая интеграция даёт ожидаемую ошибку, а не `NameError`.

**Вне рамок:** реальная реализация клиентов Platform V Search и IDP.

## Capabilities

### New Capabilities

—

### Modified Capabilities

—

Спецификации не меняются: это исправление существующего поведения инфраструктуры (`skip_specs: true`).

## Impact

- `src/aigw_service/context.py` — импорты.
- `src/aigw_modules/hub_services/` — новые заглушки (вариант а).
- `tests/unit/` — новый тест.
