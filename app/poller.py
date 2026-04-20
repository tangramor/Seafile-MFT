"""
Seafile 活动轮询模块（适配 Seafile 6.x 及以上版本）

替代 Webhook，通过定时轮询 Seafile Commit History API 检测新增/修改文件。

轮询策略：
- 每隔 POLL_INTERVAL_SECONDS 秒执行一次
- 使用 PollerState 表持久化"上次已处理的 commit_id"，避免重启后重复处理
- 只处理 added / modified 文件，忽略 deleted

Seafile 6.x API 使用：
  GET /api2/repos/{repo_id}/history/?page=1&per_page=25
  返回按时间倒序的 commit 列表，每条 commit 含 id、ctime、creator_name、desc
  
  获取 commit 中的文件变更：
  GET /api2/repos/{repo_id}/commits/{commit_id}/
  返回 { "commit_info": {...}, "commit_diffs": [...] }
"""

import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from typing import List, Optional

import httpx
from sqlalchemy.orm import Session

from .config import get_settings
from .models import ReviewTask, ReviewStatus, PollerState, get_db
from .email_notify import send_review_notification

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Seafile API 封装（仅轮询需要的部分）
# ─────────────────────────────────────────────

class SeafilePoller:
    """内网 Seafile 轮询客户端"""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Token {token}"}

    async def list_commits(self, repo_id: str, page: int = 1, per_page: int = 25) -> List[dict]:
        """
        获取 repo 的 commit 列表（时间倒序）
        Seafile 6.x 使用 /history/ 端点
        GET /api2/repos/{repo_id}/history/
        """
        url = f"{self.base_url}/api2/repos/{repo_id}/history/"
        params = {"page": page, "per_page": per_page}
        async with httpx.AsyncClient(timeout=20, verify=False) as client:
            resp = await client.get(url, params=params, headers=self.headers)
            resp.raise_for_status()
            data = resp.json()
            # 调试：打印第一个 commit 的结构
            if data and isinstance(data, list) and len(data) > 0:
                logger.debug(f"[Poller] Commit 数据结构示例: {data[0]}")
            elif data and isinstance(data, dict) and data.get("commits"):
                logger.debug(f"[Poller] Commit 数据结构示例: {data['commits'][0]}")
            # 兼容返回格式：可能是列表，也可能是 {"commits": [...]}
            if isinstance(data, list):
                return data
            return data.get("commits", [])

    async def get_commit_dir(self, repo_id: str, commit_id: str, path: str = "/") -> List[dict]:
        """
        获取指定 commit/snapshot 中的目录内容
        尝试多种 Seafile 版本的 API 路径
        """
        # 尝试多种可能的路径
        paths_to_try = [
            (f"{self.base_url}/api/v2.1/repos/{repo_id}/commits/{commit_id}/dir/", {"path": path}),
            (f"{self.base_url}/api2/repos/{repo_id}/commits/{commit_id}/dir/", {"p": path}),
            (f"{self.base_url}/api2/repos/{repo_id}/commit/{commit_id}/dir/", {"p": path}),
        ]
        
        last_error = None
        for url, params in paths_to_try:
            try:
                async with httpx.AsyncClient(timeout=20, verify=False) as client:
                    resp = await client.get(url, params=params, headers=self.headers)
                    resp.raise_for_status()
                    data = resp.json()
                    logger.info(f"[Poller] 成功获取目录列表，使用路径: {url}")
                    # 可能是列表或 {"dirents": [...]}
                    if isinstance(data, list):
                        return data
                    return data.get("dirents", [])
            except Exception as e:
                last_error = e
                logger.debug(f"[Poller] 目录列表路径失败 {url}: {e}")
                continue
        
        raise last_error

    async def get_commit_diff(self, repo_id: str, commit_id: str) -> dict:
        """
        获取指定 commit 的文件变更详情
        尝试多种 Seafile 版本的路径格式
        """
        # 尝试多种可能的路径
        paths_to_try = [
            f"{self.base_url}/api2/repos/{repo_id}/commit/{commit_id}/",
            f"{self.base_url}/api2/repos/{repo_id}/commits/{commit_id}/",
            f"{self.base_url}/api2/repos/{repo_id}/history/{commit_id}/",
        ]
        
        last_error = None
        for url in paths_to_try:
            try:
                async with httpx.AsyncClient(timeout=20, verify=False) as client:
                    resp = await client.get(url, headers=self.headers)
                    resp.raise_for_status()
                    data = resp.json()
                    logger.info(f"[Poller] 成功获取 commit diff，使用路径: {url}")
                    return data
            except Exception as e:
                last_error = e
                logger.debug(f"[Poller] 路径失败 {url}: {e}")
                continue
        
        # 所有路径都失败，抛出最后一个错误
        raise last_error


# ─────────────────────────────────────────────
# 轮询核心逻辑（同步数据库）
# ─────────────────────────────────────────────

async def poll_once():
    """
    执行一次轮询：
    1. 从数据库读取上次处理的 commit_id
    2. 获取 Seafile 最新 commits
    3. 找出所有尚未处理的新 commit
    4. 遍历每个新 commit，获取文件变更，创建审核任务
    5. 更新数据库中的 last_seen_commit_id
    """
    settings = get_settings()
    poller = SeafilePoller(settings.intranet_seafile_url, settings.intranet_seafile_token)

    with get_db() as db:
        # 1. 读取上次处理进度
        last_commit_id = _get_last_commit_id(db, settings.intranet_repo_id)

        try:
            # 2. 拉取最新 commits（最多取 50 条，防止首次运行漏单）
            commits = await poller.list_commits(settings.intranet_repo_id, per_page=50)
        except Exception as e:
            logger.error(f"[Poller] 拉取 commits 失败: {type(e).__name__}: {e}")
            import traceback
            logger.error(f"[Poller] 错误详情: {traceback.format_exc()}")
            return

        if not commits:
            return

        # 首次运行：记录当前最新 commit，不产生审核任务（避免历史文件全部触发）
        if last_commit_id is None:
            newest_id = commits[0]["id"]
            _save_last_commit_id(db, settings.intranet_repo_id, newest_id)
            db.commit()
            logger.info(f"[Poller] 首次运行，记录起始 commit: {newest_id[:8]}，后续新上传文件将触发审核")
            return

        # 3. 找出 last_commit_id 之后（更新）的所有 commit（列表是倒序的）
        new_commits = []
        for c in commits:
            if c["id"] == last_commit_id:
                break
            new_commits.append(c)

        if not new_commits:
            return  # 没有新内容

        logger.info(f"[Poller] 发现 {len(new_commits)} 个新 commit，开始处理...")

        # 4. 从旧到新处理（翻转，保证时序）
        new_commits.reverse()
        expire_at = datetime.utcnow() + timedelta(hours=settings.review_token_expire_hours)

        for commit in new_commits:
            commit_id = commit["id"]
            creator = commit.get("creator_name", commit.get("creator", "unknown"))
            creator_email = commit.get("creator", "")
            if "@" not in creator_email:
                creator_email = ""

            # 尝试获取 commit 的文件变更
            target_files: List[str] = []
            
            try:
                diff = await poller.get_commit_diff(settings.intranet_repo_id, commit_id)
                # 解析 commit_diffs 列表，提取新增和修改的文件
                # 兼容多种返回格式:
                # 格式1: {"commit_info": {...}, "commit_diffs": [{"op_type": "new|modified", "path": "..."}]}
                # 格式2: {"added": [...], "modified": [...], "deleted": [...]}
                
                # 尝试格式1: commit_diffs 数组
                commit_diffs = diff.get("commit_diffs", [])
                if commit_diffs:
                    for item in commit_diffs:
                        op_type = item.get("op_type", "")
                        if op_type in ("new", "modified", "add", "edit"):
                            path = item.get("path", "")
                            if path:
                                target_files.append(path)
                else:
                    # 尝试格式2: 直接的 added/modified 数组
                    target_files = diff.get("added", []) + diff.get("modified", [])
                    
            except Exception as e:
                logger.warning(f"[Poller] 获取 commit {commit_id[:8]} diff 失败: {e}")
                logger.info(f"[Poller] 尝试使用目录列表 API 获取 commit {commit_id[:8]} 的文件...")
                
                # 备选方案1：使用目录列表 API 获取该 commit 中的所有文件
                try:
                    dirents = await poller.get_commit_dir(settings.intranet_repo_id, commit_id, "/")
                    # dirents 格式: [{"name": "file.txt", "type": "file", "size": 1234, ...}]
                    for item in dirents:
                        if item.get("type") == "file":
                            file_name = item.get("name", "")
                            if file_name:
                                target_files.append(f"/{file_name}")
                    logger.info(f"[Poller] 从目录列表获取到 {len(target_files)} 个文件")
                except Exception as e2:
                    logger.warning(f"[Poller] 目录列表 API 也失败: {e2}")
                    
                    # 备选方案2：从 commit desc 解析文件信息（Seafile 6.x 的 commit desc 可能包含文件名）
                    commit_desc = commit.get("desc", "")
                    logger.info(f"[Poller] 尝试从 commit desc 解析: {commit_desc}")
                    
                    # 如果 desc 包含 "Added" 或 "Modified" 等关键字，尝试提取文件名
                    if commit_desc and ("Added" in commit_desc or "Modified" in commit_desc or "上传" in commit_desc):
                        # 尝试从 desc 提取文件名（简单启发式）
                        import re
                        # 匹配引号中的文件名或路径
                        matches = re.findall(r'["\']([^"\']+)["\']', commit_desc)
                        if matches:
                            for match in matches:
                                if "." in match:  # 假设包含点的是文件
                                    target_files.append(match if match.startswith("/") else f"/{match}")
                            logger.info(f"[Poller] 从 commit desc 解析到 {len(target_files)} 个文件")
                    
                    if not target_files:
                        logger.error(f"[Poller] 无法获取 commit {commit_id[:8]} 的文件信息，跳过")
                        continue

            for file_path in target_files:
                # 过滤空值
                if not file_path:
                    continue

                file_name = file_path.rstrip("/").split("/")[-1]
                if not file_name:
                    continue

                # 去重：同路径+相近时间内不重复创建任务
                if _task_exists(db, settings.intranet_repo_id, file_path, commit_id):
                    logger.info(f"[Poller] 跳过重复任务: {file_path}")
                    continue

                token = secrets.token_urlsafe(32)
                task = ReviewTask(
                    token=token,
                    file_name=file_name,
                    file_path=file_path,
                    repo_id=settings.intranet_repo_id,
                    commit_id=commit_id,
                    uploader=creator,
                    uploader_email=creator_email,
                    status=ReviewStatus.PENDING,
                    expire_at=expire_at,
                )
                db.add(task)
                db.flush()  # 获取自增 ID
                db.refresh(task)

                logger.info(f"[Poller] 创建审核任务 #{task.id}: {file_path} (commit {commit_id[:8]})")

                # 发送审批邮件（异步）
                asyncio.create_task(send_review_notification(task))

        # 5. 更新进度到最新 commit（new_commits 已翻转，取最后一个即最新）
        latest_commit_id = new_commits[-1]["id"]
        _save_last_commit_id(db, settings.intranet_repo_id, latest_commit_id)
        db.commit()
        logger.info(f"[Poller] 进度更新至 commit: {latest_commit_id[:8]}")


def _get_last_commit_id(db: Session, repo_id: str) -> Optional[str]:
    """同步获取上次处理的 commit_id"""
    state = db.query(PollerState).filter(PollerState.repo_id == repo_id).first()
    return state.last_commit_id if state else None


def _save_last_commit_id(db: Session, repo_id: str, commit_id: str):
    """同步保存 commit_id"""
    state = db.query(PollerState).filter(PollerState.repo_id == repo_id).first()
    if state:
        state.last_commit_id = commit_id
        state.updated_at = datetime.utcnow()
    else:
        state = PollerState(repo_id=repo_id, last_commit_id=commit_id)
        db.add(state)


def _task_exists(db: Session, repo_id: str, file_path: str, commit_id: str) -> bool:
    """检查同一 commit + 文件路径是否已创建过任务"""
    task = db.query(ReviewTask).filter(
        ReviewTask.repo_id == repo_id,
        ReviewTask.file_path == file_path,
        ReviewTask.commit_id == commit_id,
    ).first()
    return task is not None


# ─────────────────────────────────────────────
# 后台轮询循环（由 main.py lifespan 启动）
# ─────────────────────────────────────────────

async def start_polling_loop():
    """
    持续后台轮询任务，在应用生命周期内运行。
    通过 asyncio.create_task 在 FastAPI lifespan 中启动。
    """
    settings = get_settings()
    interval = settings.poll_interval_seconds
    logger.info(f"[Poller] 后台轮询已启动，间隔 {interval} 秒，监控 repo: {settings.intranet_repo_id}")

    while True:
        try:
            await poll_once()
        except Exception as e:
            logger.error(f"[Poller] poll_once 异常: {e}")
        await asyncio.sleep(interval)
