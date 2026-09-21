from typing import Optional

from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from aigw_service.api.v1.schemas.llm_outputs import ClassifierOutput
from aigw_service.api.v1.subagents.utils import build_diagnostic_data, get_tokens
from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()

CLASSIFIER_PROMPT = """
Your task is to determine user intent.
You can achieve it to by following these steps:
1. Analyze user question
2. If user wants to calculate the inputs, by given output and date/year - return 'analyze_model_inputs_for_target'
3. If user wants to make an analysis of the model and see the potential outputs by selectint
diferent inputs and their ranges - select 'analyze_excel_model'
4. Identify filename, which user mentioned to perform analysis'. Write it in 'filename'
"""


def classify_user_query(llm, messages: list[BaseMessage], config: Optional[RunnableConfig] = None) -> ClassifierOutput:
    """Определяет намерение пользователя и возвращает, какой сабагент его обработает.

    Преподставляет системное сообщение с CLASSIFIER_PROMPT, запрашивает у LLM
    структурированный ответ (ClassifierOutput) и возвращает его верхнеуровневому графу.
    Содержит retry-логику: при валидации LLM-ответа — до 3 попыток, затем dummy.
    """
    import json
    from time import time as _time

    from aigw_service.api.v1.subagents.dummies import retry_structured_llm

    prompt = [SystemMessage(content=CLASSIFIER_PROMPT), *messages]

    start = _time()
    parsed, success, raw_response = retry_structured_llm(llm, ClassifierOutput, prompt)
    elapsed = _time() - start

    tokens = get_tokens(raw_response["raw"]) if (success and raw_response) else {}
    data = build_diagnostic_data("classifier", elapsed, tokens, extra={"next_agent": parsed.next_agent})

    logger.info(json.dumps(data, ensure_ascii=False))

    return parsed
