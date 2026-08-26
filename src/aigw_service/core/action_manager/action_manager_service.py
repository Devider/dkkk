from abc import ABC, abstractmethod

from fastapi import Depends
from pydantic import BaseModel

from aigw_service.api.v1.utils import common_headers
from aigw_service.context import APP_CTX
from aigw_service.core.action_manager.models import ActionHistoryPoint, ActionsChain, ActionsHistory, ActionType

from .base_action import BaseAction

logger = APP_CTX.get_logger()


class ActionManagerProtocol(ABC):
    def __init__(self, request: BaseModel, headers: dict = Depends(common_headers)):
        self.request: BaseModel = request
        self.headers: dict = headers
        self.action_history: ActionsHistory = ActionsHistory()

    @abstractmethod
    async def execute(self, action: BaseAction, *args, **kwargs):
        pass

    @abstractmethod
    async def undo(self, action: BaseAction, *args, **kwargs):
        pass


class ActionManager(ActionManagerProtocol):
    """Action manager class"""

    def __init__(self, request: BaseModel, headers: dict = Depends(common_headers)):
        super().__init__(request, headers)

    async def execute(self, action: BaseAction, *args, **kwargs):
        """
        Метод для выполнения действия
        :param action:
        :param args:
        :param kwargs:
        :return:
        """
        try:
            logger.info(f"Executing action {action.__class__}")
            result = await action.execute()
            history_point = ActionHistoryPoint(
                x_trace_id=self.headers.get("x-trace-id"),
                x_client_id=self.headers.get("x-client-id"),
                x_session_id=self.headers.get("x-session-id"),
                x_request_time=self.headers.get("x-request-time"),
                x_user_id=self.headers.get("x-user-id"),
                action=action,
                type=ActionType.EXECUTE.value,
            )
            self.action_history.chain.append(history_point)
            logger.info(f"Action {action.__class__} point ID {history_point.id} executed successfully")
            if result:
                return result
        except Exception as e:
            logger.error(f"Error executing action {action.__class__}: {e}")
            pass

    async def undo(self, action: BaseAction, *args, **kwargs):
        """
        Метод для отката действия
        :param action:
        :param args:
        :param kwargs:
        :return:
        """
        try:
            logger.info(f"Undoing action {action.__class__.__name__}")
            result = action.undo()
            history_point = ActionHistoryPoint(
                x_trace_id=self.headers.get("x-trace-id"),
                x_client_id=self.headers.get("x-client-id"),
                x_session_id=self.headers.get("x-session-id"),
                x_request_time=self.headers.get("x-request-time"),
                x_user_id=self.headers.get("x-user-id"),
                action=action,
                type=ActionType.UNDO.value,
            )
            self.action_history.chain.append(history_point)
            logger.info(f"Action {action.__class__.__name__} point ID {history_point.id} undone successfully")
            if result:
                return result
        except Exception as e:
            logger.error(f"Error undoing action {action.__class__.__name__}: {e}")
            pass

    async def rollback_history(self):
        """
        Метод для отката истории действий. Будет вызваться обратное действие для каждого действия из истории
        ВНИМАНИЕ! Действия откатываются в обратном порядке, т.е. последнее выполненное действие будет отменено первым
        :return:
        """
        for action in reversed(self.action_history.chain):
            if action.type == ActionType.EXECUTE.value:
                await self.undo(action.action)
            elif action.type == ActionType.UNDO.value:
                await self.execute(action.action)

    async def action_chain_rollback(self, action_chain: ActionsChain, reverse: bool = False):
        """
        Метод для отката цепочки действий
        :param action_chain: цепочка действий
        :param reverse: флаг, указывающий на необходимость отката в обратном порядке
        :return:
        """
        if reverse:
            for action in reversed(action_chain.chain):
                await self.undo(action)
        else:
            for action in action_chain.chain:
                await self.undo(action)

    async def action_chain_execute(self, action_chain: ActionsChain, reverse: bool = False):
        """
        Метод для выполнения цепочки действий
        :param action_chain: цепочка действий
        :param reverse: флаг, указывающий на необходимость выполнения в обратном порядке
        :return:
        """
        if reverse:
            for action in reversed(action_chain.chain):
                await self.undo(action)
        else:
            for action in action_chain.chain:
                await self.undo(action)
