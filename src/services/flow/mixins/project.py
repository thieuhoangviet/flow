import asyncio
import json
import contextvars
import time
import uuid
import random
import base64
import ssl
from typing import TYPE_CHECKING, Any, Dict, Any, Optional, List, Union, Callable, Awaitable
from urllib.parse import quote
import urllib.error
import urllib.request
from curl_cffi.requests import AsyncSession
from src.core.logger import debug_logger
from src.core.config import config, get_yescaptcha_min_score
try:
    import httpx
except ImportError:
    pass

class FlowClientProjectMixin:
    if TYPE_CHECKING:
        def __getattr__(self, name: str) -> Any: ...

    async def create_project(self, st: str, title: str) -> str:
        """创建项目,返回project_id

        Args:
            st: Session Token
            title: 项目标题

        Returns:
            project_id (UUID)
        """
        url = f"{self.labs_base_url}/trpc/project.createProject"
        json_data = {
            "json": {
                "projectTitle": title,
                "toolName": "PINHOLE"
            }
        }
        max_retries = config.flow_max_retries
        request_timeout = max(self._get_control_plane_timeout(), min(self.timeout, 15))
        last_error: Optional[Exception] = None

        for retry_attempt in range(max_retries):
            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_st=True,
                    st_token=st,
                    timeout=request_timeout,
                )
                project_result = (
                    result.get("result", {})
                    .get("data", {})
                    .get("json", {})
                    .get("result", {})
                )
                project_id = project_result.get("projectId")
                if not project_id:
                    raise Exception("Invalid project.createProject response: missing projectId")
                return project_id
            except Exception as e:
                last_error = e
                retry_reason = "网络超时" if self._is_timeout_error(e) else self._get_retry_reason(str(e))
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[PROJECT] 创建项目失败，准备重试 ({retry_attempt + 2}/{max_retries}) "
                        f"title={title!r}, reason={retry_reason}: {e}"
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("创建项目失败")

    async def delete_project(self, st: str, project_id: str):
        """删除项目

        Args:
            st: Session Token
            project_id: 项目ID
        """
        url = f"{self.labs_base_url}/trpc/project.deleteProject"
        json_data = {
            "json": {
                "projectToDeleteId": project_id
            }
        }

        await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_st=True,
            st_token=st,
            timeout=self._get_control_plane_timeout(),
        )

