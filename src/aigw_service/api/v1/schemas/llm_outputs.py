"""
Pydantic-модели для структурированного вывода LLM.

Используются с llm.with_structured_output() для валидации ответов от LLM.
Эти же модели инлайн-импортируются в TypedDict-стейты через поля.
"""

from pydantic import BaseModel, Field


class RangeConfig(BaseModel):
    """Диапазон изменения входного параметра, упомянутого пользователем"""

    start_value: float = Field(description="Начальное значение диапазона")
    end_value: float = Field(description="Конечное значение диапозона")
    step: float | None = Field(default=None, description="Шаг изменения параметра")


class OutputItem(BaseModel):
    """
    Упомянутый пользователем выходной показатель
    """

    mentioned_output_name: str = Field(description="Выходной показатель, упомянутый пользователем")
    explanation: str = Field(description="Краткое объяснение, как ты выбираешь эквивалент")
    equivalent_output_name: str = Field(
        description=(
            "Эквивалент выходного показателя из СТАНДАРТНОГО СПИСКА ВЫХОДНЫХ ПОКАЗАТЕЛЕЙ (OUTPUTS). "
            "Запиши так как дано в СТАНДАРТНОМ СПИСКЕ, не переводи, не расшифровывай"
        ),
    )


class InputItem(BaseModel):
    """
    Упомянутый пользователем входной параметр и его эквивалент из СТАНДАРТНОГО СПИСКА ВХОДНЫХ ПАРАМЕТРОВ (INPUTS)
    """

    mentioned_input_name: str = Field(description="Входной параметр, упомянутый пользователем")
    explanation: str = Field(description="Объяснение, почему выбран именно этот эквивалент")
    equivalent_input_name: str = Field(
        description=(
            "Эквивалент входного параметра из СТАНДАРТНОГО СПИСКА ВХОДНЫХ ПАРАМЕТРОВ (INPUTS). "
            "Запиши так как дано в СТАНДАРТНОМ СПИСКЕ, не переводи, не расшифровывай"
        ),
    )
    equivalent_input_id: str = Field(
        description="Идентификатор эквивалента входного параметра из СТАНДАРТНОГО СПИСКА в формате INid"
    )
    range_config: RangeConfig | None = Field(
        default=None,
        description="Диапазон изменения данного параметра, который указал пользователь. Запиши его только если пользователь указал диапазон",
    )
    lookup_more: bool = Field(
        description="Нужно ли сделать более детальный поиск? Если не удалось найти эквивалент — True"
    )
    what_lookup: str | None = Field(
        default=None, description="Какой параметр искать при детальном поиске (только если lookup_more=True)"
    )


class LookupResult(BaseModel):
    """
    Результат более детального поиска эквивалентов из СТАНДАРТНОГО СПИСКА ВХОДНЫХ ПАРАМЕТРОВ (INPUTS)
    """

    mentioned_inputs: list[InputItem] = Field(
        description="Список ВСЕХ входных параметров, упомянутых пользователем, как есть"
    )


class QueryAnalysisIFT(BaseModel):
    """
    Анализ запроса пользователя для подбора значений входных параметров,
    при которых выходной показатель достигает целевого значения.
    """

    analysis: str = Field(
        description="Саммари: какие показатели и на основании каких параметров пользователь хочет посчитать",
    )
    mentioned_output_name: str = Field(
        description="Извлечённый выходной показатель, упомянутый пользователем, как есть"
    )
    mentioned_inputs: list[InputItem] = Field(description="Список ВСЕХ входных параметров, упомянутых пользователем")
    output_name: str = Field(
        description=(
            "Подобранный аналог для упомянутого выходного показателя из "
            "СТАНДАРТНЫЙ СПИСОК ВЫХОДНЫХ ПОКАЗАТЕЛЕЙ (OUTPUTS)"
        ),
    )
    target_value: float = Field(
        description="Целевое значение для выходного параметра (без единиц измерения и суффиксов)"
    )
    output_year: int = Field(
        description="Год, упомянутый пользователем, на который должен быть рассчитан целевой показатель"
    )


class QueryAnalysisEMA(BaseModel):
    """
    Анализ запроса пользователя для расчета
    нескольких выходных показателей в условиях изменения указанных параметров
    """

    analysis: str = Field(
        description="Саммари: какие показатели и на основании каких параметров пользователь хочет посчитать",
    )
    mentioned_outputs: list[OutputItem] = Field(
        description="Список ВСЕХ выходных показателей, которые пользовтель хочет пересчитать"
    )
    mentioned_inputs: list[InputItem] = Field(
        description="Список ВСЕХ входных параметров, упомянутых пользователем, как есть"
    )
    year: int = Field(description="Год, упомянутый пользователем, на который должен быть рассчитан целевые показатели")
