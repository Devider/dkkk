from abc import ABC, abstractmethod


class BaseAction(ABC):
    """
    Интерфейс Команды объявляет метод для выполнения и отмены команд.
    """

    @abstractmethod
    async def execute(self):
        pass

    @abstractmethod
    async def undo(self):
        pass
