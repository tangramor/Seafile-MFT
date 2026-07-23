"""
文件传输模块

核心功能：
1. 从内网 Seafile 下载文件（使用 Seafile REST API）
2. 上传到外网 Seafile（使用 Seafile REST API / seafile-python-sdk）

Seafile API 文档：https://download.seafile.com/published/seafile-user-manual/develop/web_api_v2.1.md

关于 seafhttp URL 重写：
  - Seafile 的 FILE_SERVER_ROOT 可能配置为浏览器可访问的地址（如 localhost:8001）
  - 但 MFT 容器内需要通过 Docker 内部域名访问 seafhttp
  - 本模块会自动将 seafhttp URL 的 host 重写为 base_url 的 host
"""
import io
import os
import tempfile
from typing import Tuple
from urllib.parse import urlparse, urlunparse

import httpx

from .config import get_settings
from .models import ReviewTask, RepoPair, get_db


class SeafileClient:
    """Seafile REST API 客户端封装

    base_url 必须为容器内可访问的地址（如 http://intranet.local），
    所有 seafhttp 链接中的 host 会被自动重写为此地址。
    """

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.base_host = urlparse(self.base_url).netloc  # 用于重写 seafhttp URL
        self.headers = {"Authorization": f"Token {token}"}

    def _rewrite_seafhttp_url(self, url: str) -> str:
        """将 seafhttp URL 的 host 重写为 base_url 的 host

        例如：http://localhost:8001/seafhttp/upload-api/xxx
          →  http://intranet.local/seafhttp/upload-api/xxx
        """
        if not url:
            return url
        parsed = urlparse(url)
        # 只重写 seafhttp 路径的 URL
        if parsed.netloc == self.base_host:
            return url  # 无需重写
        return urlunparse(parsed._replace(netloc=self.base_host))

    async def get_download_link(self, repo_id: str, file_path: str) -> str:
        """获取文件下载链接"""
        url = f"{self.base_url}/api2/repos/{repo_id}/file/"
        params = {"p": file_path}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params=params, headers=self.headers)
            resp.raise_for_status()
            raw_url = resp.text.strip('"')
            return self._rewrite_seafhttp_url(raw_url)

    async def download_file(self, repo_id: str, file_path: str) -> bytes:
        """下载文件内容到内存"""
        dl_url = await self.get_download_link(repo_id, file_path)
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.get(dl_url, headers=self.headers)
            resp.raise_for_status()
            return resp.content

    async def get_upload_link(self, repo_id: str, target_dir: str = "/") -> str:
        """获取文件上传链接"""
        url = f"{self.base_url}/api2/repos/{repo_id}/upload-link/"
        params = {"p": target_dir}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params=params, headers=self.headers)
            resp.raise_for_status()
            raw_url = resp.text.strip('"')
            return self._rewrite_seafhttp_url(raw_url)

    async def upload_file(
        self,
        repo_id: str,
        file_name: str,
        file_content: bytes,
        target_dir: str = "/",
    ) -> str:
        """
        上传文件到指定目录

        Returns:
            str: 上传后的文件路径
        """
        upload_url = await self.get_upload_link(repo_id, target_dir)

        async with httpx.AsyncClient(timeout=300) as client:
            files = {
                "file": (file_name, io.BytesIO(file_content), "application/octet-stream"),
                "filename": (None, file_name),
                "parent_dir": (None, target_dir),
            }
            resp = await client.post(upload_url, files=files, headers=self.headers)
            resp.raise_for_status()

        return f"{target_dir.rstrip('/')}/{file_name}"

    async def ensure_dir(self, repo_id: str, dir_path: str):
        """确保目录存在，不存在则创建（递归）"""
        if dir_path == "/" or not dir_path:
            return
        url = f"{self.base_url}/api2/repos/{repo_id}/dir/"
        params = {"p": dir_path}
        async with httpx.AsyncClient(timeout=30) as client:
            # 先检查是否存在
            resp = await client.get(url, params=params, headers=self.headers)
            if resp.status_code == 200:
                return
            # 不存在则创建
            data = {"operation": "mkdir"}
            resp = await client.post(url, params=params, data=data, headers=self.headers)
            if resp.status_code not in (200, 201):
                # 父目录可能不存在，递归创建
                parent = "/".join(dir_path.rstrip("/").split("/")[:-1]) or "/"
                await self.ensure_dir(repo_id, parent)
                resp = await client.post(url, params=params, data=data, headers=self.headers)
                resp.raise_for_status()

    async def list_repos(self) -> list:
        """列出当前账号下的所有仓库"""
        url = f"{self.base_url}/api2/repos/"
        async with httpx.AsyncClient(timeout=30, verify=False) as client:
            resp = await client.get(url, headers=self.headers)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                return data.get("repos", [])
            return data

    async def repo_exists(self, repo_id: str) -> bool:
        """
        校验指定 repo_id 的仓库是否存在于该 Seafile 实例。

        Returns:
            bool: 存在返回 True，不存在（404）返回 False。
        Raises:
            对其他非 2xx/404 状态（如 Token 鉴权失败）抛出 RuntimeError。
        """
        url = f"{self.base_url}/api2/repos/{repo_id}/"
        async with httpx.AsyncClient(timeout=30, verify=False) as client:
            resp = await client.get(url, headers=self.headers)
            if resp.status_code == 200:
                return True
            if resp.status_code == 404:
                return False
            if resp.status_code in (401, 403):
                raise RuntimeError(
                    f"Seafile 鉴权失败（HTTP {resp.status_code}），请检查 Token 是否有效"
                )
            raise RuntimeError(
                f"校验仓库存在性失败（HTTP {resp.status_code}）: {resp.text[:200]}"
            )

    async def ensure_repo(self, name: str) -> str:
        """
        确保指定名称的仓库存在，返回其 repo_id。
        若已存在同名仓库则直接返回其 id；否则新建并返回。
        """
        repos = await self.list_repos()
        for r in repos:
            if r.get("name") == name:
                return r.get("id") or r.get("repo_id")

        # 不存在则创建
        url = f"{self.base_url}/api2/repos/"
        data = {"name": name, "desc": f"MFT 配对仓库 {name}"}
        async with httpx.AsyncClient(timeout=30, verify=False) as client:
            resp = await client.post(url, data=data, headers=self.headers)
            resp.raise_for_status()
            result = resp.json()
            repo_id = result.get("repo_id") or result.get("id")
            if not repo_id:
                raise RuntimeError(f"创建仓库失败，响应中无 repo_id：{result}")
            return repo_id


async def transfer_file_to_extranet(
    task: ReviewTask,
) -> Tuple[bool, str, str]:
    """
    将内网文件传输到外网 Seafile

    Args:
        task: 审核任务对象

    Returns:
        Tuple[bool, str, str]: (成功标志, 错误信息, 外网文件路径)
    """
    settings = get_settings()

    # 解析任务所属配对，确定内外网目标仓库
    intranet_repo_id = task.repo_id
    extranet_repo_id = settings.extranet_repo_id  # 兜底：无配对时用全局外网仓库
    if task.repo_pair_id:
        with get_db() as db:
            pair = db.query(RepoPair).filter(RepoPair.id == task.repo_pair_id).first()
            if pair:
                intranet_repo_id = pair.intranet_repo_id
                extranet_repo_id = pair.extranet_repo_id

    # 初始化内外网客户端
    intranet_client = SeafileClient(
        settings.intranet_seafile_url,
        settings.intranet_seafile_token,
    )
    extranet_client = SeafileClient(
        settings.extranet_seafile_url,
        settings.extranet_seafile_token,
    )

    try:
        print(f"[Transfer] Downloading {task.file_path} from intranet repo {intranet_repo_id}...")
        file_content = await intranet_client.download_file(intranet_repo_id, task.file_path)
        print(f"[Transfer] Downloaded {len(file_content)} bytes")

        # 保持原始目录结构
        target_dir = os.path.dirname(task.file_path) or "/"
        if not target_dir.startswith("/"):
            target_dir = "/" + target_dir

        # 确保目标目录存在
        await extranet_client.ensure_dir(extranet_repo_id, target_dir)

        print(f"[Transfer] Uploading to extranet repo {extranet_repo_id}, dir={target_dir}...")
        extranet_path = await extranet_client.upload_file(
            extranet_repo_id,
            task.file_name,
            file_content,
            target_dir=target_dir,
        )
        print(f"[Transfer] Upload success: {extranet_path}")

        return True, "", extranet_path

    except httpx.HTTPStatusError as e:
        error_msg = f"HTTP Error {e.response.status_code}: {e.response.text[:200]}"
        print(f"[Transfer] Failed: {error_msg}")
        return False, error_msg, ""

    except Exception as e:
        error_msg = str(e)
        print(f"[Transfer] Failed: {error_msg}")
        return False, error_msg, ""
