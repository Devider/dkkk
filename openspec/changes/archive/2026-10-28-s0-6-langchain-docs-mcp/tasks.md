# Tasks

## 1. Проверка и документация

- [x] 1.1 Проверить критерий приёмки: через `langchain-docs` (`search_docs_by_lang_chain`) получить ответ по API, который использует v2. Проверка: запрос «deepagents SkillsMiddleware pinned skills» вернул раздел `oss/python/deepagents/skills#pin-skills`; его содержание (одно сообщение `lc_source="pinned_skill"` на skill, инструменты skill открываются, неизвестное имя пропускается) совпадает с поведением, на которое опирается S3.5
- [x] 1.2 CLAUDE.md: раздел «LangChain documentation (MCP)» — сервер и файл настройки, инструменты, когда обращаться, сверка с закреплёнными версиями из `pyproject.toml`. Проверка: раздел есть, имена сервера и URL совпадают с `.mcp.json`
- [x] 1.3 AGENTS.md: тот же раздел, без завязки на конкретного ассистента. Проверка: раздел есть, сведения совпадают с CLAUDE.md
- [x] 1.4 `openspec/traceability.md`: S0.6 отмечена ✓ со ссылкой на change. Проверка: строка обновлена

## 2. Качество

- [x] 2.1 `ruff check --fix` и `ruff format` только по изменённым и новым `.py`-файлам change. Проверка: таких файлов нет (`git diff --name-only --diff-filter=AM HEAD -- '*.py'` пуст)
- [x] 2.2 `pylint src` > 7 и `pytest tests/unit`. Проверка: оценка > 7; падения только известные и не связанные со story (`TestNoHardcodedRetry`, `test_runtime::test_defaults` при локальном `.env` с `AGENT_ENGINE=v2`)
