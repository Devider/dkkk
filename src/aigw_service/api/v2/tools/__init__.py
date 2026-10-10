"""Инструменты /api/v2.

- `excel_calc` — LibreOffice-расчёты, которые вызывают subagent'ы через `OrchestratorTools`.
- `storage`, `workbook` — инструменты, которые deep agent вызывает напрямую (`DEEP_AGENT_TOOLS`).
"""

from aigw_service.api.v2.tools.storage import check_uploaded_file
from aigw_service.api.v2.tools.workbook import list_excel_sheets

DEEP_AGENT_TOOLS = [check_uploaded_file, list_excel_sheets]
