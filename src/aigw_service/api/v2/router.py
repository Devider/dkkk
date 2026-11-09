"""Minimal API v2 backed by a LangChain Deep Agent."""

import zipfile
from functools import lru_cache
from io import BytesIO
from uuid import uuid4

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from aigw_service.api.v1.router import upload_file
from aigw_service.api.v1.utils import common_headers
from aigw_service.api.v2.agent import create_v2_agent
from aigw_service.context import APP_CTX

router = APIRouter()
logger = APP_CTX.get_logger()


class DeepAgentRequest(BaseModel):
    """Request body for the v2 agent."""

    message: str = Field(min_length=1, max_length=2000)


@lru_cache(maxsize=1)
def get_deep_agent():
    """Create the v2 GigaChat client and Deep Agent once per process."""
    return create_v2_agent()


router.add_api_route("/upload", upload_file, methods=["POST"], include_in_schema=False)


@router.post(
    "/invoke-agent",
    status_code=200,
    response_description="Успешный ответ от агента в виде ZIP-архива",
)
async def invoke_agent_v2(
    request: DeepAgentRequest,
    headers: dict = Depends(common_headers),
) -> StreamingResponse:
    """Run the minimal Deep Agent with the existing Excel tools."""
    user_id = headers.get("x-user-id", None)
    client_id = headers.get("x-client-id", None)
    session_id = headers.get("x-session-id") or str(uuid4())
    thread_id = f"{user_id}:{session_id}"
    config = {
        "configurable": {"thread_id": thread_id, "user_id": user_id},
        "metadata": {
            "langfuse_user_id": user_id,
            "client_id": client_id,
            "langfuse_session_id": thread_id,
            "endpoint": "/api/v2/invoke-agent",
        },
    }
    if APP_CTX.tracing is not None and hasattr(APP_CTX.tracing, "callback_handler"):
        config["callbacks"] = [APP_CTX.tracing.callback_handler]
    else:
        logger.warning("Langfuse callback handler is unavailable; v2 traces will not be reported")

    try:
        agent = get_deep_agent()
        pending_interrupt = False
        get_state = getattr(agent, "aget_state", None)
        if get_state is not None:
            snapshot = await get_state(config)
            pending_interrupt = any(getattr(task, "interrupts", ()) for task in getattr(snapshot, "tasks", ()))
        invocation = (
            Command(resume=request.message)
            if pending_interrupt
            else {
                "messages": [HumanMessage(content=request.message)],
            }
        )
        result = await agent.ainvoke(invocation, config=config)
        interruptions = result.get("__interrupt__", ())
        if interruptions:
            content = "\n\n".join(
                str(interruption.value.get("question", interruption.value))
                if isinstance(interruption.value, dict)
                else str(interruption.value)
                for interruption in interruptions
            )
        else:
            last_message = result["messages"][-1]
            content = last_message.content
            if not isinstance(content, str):
                content = str(content)
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            zip_file.writestr("txt_response.txt", content)
            excel_buffer = BytesIO()
            pd.DataFrame().to_excel(excel_buffer, index=False)
            zip_file.writestr("generated/agent_output.xlsx", excel_buffer.getvalue())

        zip_buffer.seek(0)
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename=agent_response_{session_id}.zip"},
        )
    except Exception as exc:
        logger.opt(exception=True).error("Deep Agent invocation failed: {}", str(exc))
        raise HTTPException(status_code=424, detail=str(exc)) from exc
