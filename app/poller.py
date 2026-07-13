"""
Seafile 活动轮询模块（适配 Seafile 6.x 及以上版本）

替代 Webhook，通过定时轮询 Seafile Commit History API 检测新增/修改文件。

轮询策略：
- 每隔 POLL_INTERVAL_SECONDS 秒执行一次
- 使用 PollerState 表持久化"上次已处理的 commit_id"，避免重启后重复处理

Seafile 6.x API（已确认可用）：
  GET /api2/repos/{repo_id}/history/          - 获取 commit 列表（时间倒序）
  GET /api2/repos/{repo_id}/dir/?p={path}     - 获取目录内容（当前版本）
  GET /api2/repos/{repo_id}/file/detail/?p={path} - 获取文件详情（含 last_modified、id）

轮询逻辑（绕开不可用的 commit diff API）：
  1. 拉取最新 commit 列表，找到比 last_commit_id 更新的提交
  2. 获取新 commit 的提交时间（ctime）
  3. 遍历仓库中所有文件，找出 last_modified >= last_ctime 的文件
  4. 为每个新增/修改文件创建审核任务
  5. 更新 last_commit_id
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
from .audit import log_action

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Seafile API 封装（仅轮询需要的部分）
# ─────────────────────────────────────────────

class SeafilePoller:
    """内网 Seafile 轮询客户端"""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Token {token}"}

    async def list_commits(self, repo_id: str, page: int = 1, per_page: int = 50) -> List[dict]:
        """
        获取 repo 的 commit 列表（时间倒序）
        Seafile 6.x: GET /api2/repos/{repo_id}/history/
        返回: {"commits": [...]} 或直接列表
        每个 commit 包含: id, ctime, creator_name, creator, desc
        """
        url = f"{self.base_url}/api2/repos/{repo_id}/history/"
        params = {"page": page, "per_page": per_page}
        async with httpx.AsyncClient(timeout=20, verify=False) as client:
            resp = await client.get(url, params=params, headers=self.headers)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            return data.get("commits", [])

    async def list_dir(self, repo_id: str, path: str = "/") -> List[dict]:
        """
        列出目录内容（当前最新版本）
        GET /api2/repos/{repo_id}/dir/?p={path}
        返回文件/目录条目列表，每条包含:
          - name: 文件/目录名
          - type: "file" | "dir"
          - size: 文件大小（bytes）
          - id: 对象 ID
          - mtime: 最后修改时间（Unix 时间戳）
        """
        url = f"{self.base_url}/api2/repos/{repo_id}/dir/"
        params = {"p": path}
        async with httpx.AsyncClient(timeout=20, verify=False) as client:
            resp = await client.get(url, params=params, headers=self.headers)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            return data.get("dirents", [])

    async def list_all_files(self, repo_id: str, path: str = "/") -> List[dict]:
        """
        递归列出仓库中所有文件（深度优先遍历目录树）
        返回: [{"path": "/dir/file.txt", "name": "file.txt", "mtime": ..., "size": ...}, ...]
        """
        all_files = []
        try:
            entries = await self.list_dir(repo_id, path)
        except Exception as e:
            logger.warning(f"[Poller] 列出目录 {path} 失败: {e}")
            return all_files

        for entry in entries:
            entry_name = entry.get("name", "")
            if not entry_name:
                continue

            entry_path = f"{path.rstrip('/')}/{entry_name}"
            entry_type = entry.get("type", "")

            if entry_type == "file":
                all_files.append({
                    "path": entry_path,
                    "name": entry_name,
                    "mtime": entry.get("mtime", 0),
                    "size": entry.get("size", 0),
                    "id": entry.get("id", ""),
                })
            elif entry_type == "dir":
                # 递归遍历子目录
                sub_files = await self.list_all_files(repo_id, entry_path)
                all_files.extend(sub_files)

        return all_files


# ─────────────────────────────────────────────
# 轮询核心逻辑（同步数据库）
# ─────────────────────────────────────────────

async def poll_once():
    """
    执行一次轮询：
    1. 从数据库读取上次处理的 commit_id 和 ctime
    2. 获取 Seafile 最新 commits，找出新 commit
    3. 遍历仓库所有文件，找出 mtime >= last_ctime 的文件
    4. 为每个新增/修改文件创建审核任务
    5. 更新数据库中的 last_commit_id
    """
    settings = get_settings()
    poller = SeafilePoller(settings.intranet_seafile_url, settings.intranet_seafile_token)

    with get_db() as db:
        # 1. 读取上次处理进度
        last_commit_id = _get_last_commit_id(db, settings.intranet_repo_id)

        try:
            # 2. 拉取最新 commits
            commits = await poller.list_commits(settings.intranet_repo_id)
        except Exception as e:
            logger.error(f"[Poller] 拉取 commits 失败: {type(e).__name__}: {e}")
            return

        if not commits:
            return

        # 首次运行：记录当前最新 commit，不产生审核任务
        if last_commit_id is None:
            newest_id = commits[0]["id"]
            _save_last_commit_id(db, settings.intranet_repo_id, newest_id)
            db.commit()
            logger.info(f"[Poller] 首次运行，记录起始 commit: {newest_id[:8]}，后续新上传文件将触发审核")
            return

        # 3. 找出 last_commit_id 之后的新 commit（列表是倒序的）
        new_commits = []
        for c in commits:
            if c["id"] == last_commit_id:
                break
            new_commits.append(c)

        if not new_commits:
            return  # 没有新内容

        logger.info(f"[Poller] 发现 {len(new_commits)} 个新 commit，开始处理...")

        # 4. 计算需要检查的时间范围
        # new_commits 是倒序的，最旧的新 commit 时间作为下限
        oldest_new_commit = new_commits[-1]
        # ctime 可能是 int（Unix 时间戳）或 str
        oldest_ctime = oldest_new_commit.get("ctime", 0)
        if isinstance(oldest_ctime, str):
            try:
                oldest_ctime = int(oldest_ctime)
            except Exception:
                oldest_ctime = 0

        # 上次 commit 的时间（用于筛选文件，稍微往前一点避免时间边界问题）
        last_commit_data = next((c for c in commits if c["id"] == last_commit_id), None)
        last_ctime = 0
        if last_commit_data:
            last_ctime = last_commit_data.get("ctime", 0)
            if isinstance(last_ctime, str):
                try:
                    last_ctime = int(last_ctime)
                except Exception:
                    last_ctime = 0

        # 使用最旧新 commit 时间（减1秒容错）作为文件筛选基准
        filter_after_ts = max(0, oldest_ctime - 1)

        logger.info(f"[Poller] 筛选 mtime >= {filter_after_ts} 的文件 (最旧新 commit ctime={oldest_ctime})")

        # 5. 遍历仓库所有文件
        try:
            all_files = await poller.list_all_files(settings.intranet_repo_id)
        except Exception as e:
            logger.error(f"[Poller] 遍历仓库文件失败: {e}")
            # 即使获取文件失败，也更新进度避免死循环
            latest_commit_id = new_commits[0]["id"]
            _save_last_commit_id(db, settings.intranet_repo_id, latest_commit_id)
            db.commit()
            return

        logger.info(f"[Poller] 仓库共 {len(all_files)} 个文件，开始筛选...")

        expire_at = datetime.utcnow() + timedelta(hours=settings.review_token_expire_hours)

        # 取最新 commit 的上传者信息（作为默认 creator）
        latest_new_commit = new_commits[0]
        creator = latest_new_commit.get("creator_name", latest_new_commit.get("creator", "unknown"))
        creator_email = latest_new_commit.get("creator", "")
        if "@" not in creator_email:
            creator_email = ""

        # 6. 筛选新增/修改的文件（mtime 在最旧新 commit 时间之后）
        new_files = [f for f in all_files if f.get("mtime", 0) >= filter_after_ts]

        if new_files:
            logger.info(f"[Poller] 筛选到 {len(new_files)} 个新增/修改文件")
        else:
            logger.info(f"[Poller] 未发现新增/修改文件（可能是删除/重命名操作）")

        for file_info in new_files:
            file_path = file_info["path"]
            file_name = file_info["name"]

            # 使用最新 commit id 关联
            commit_id = latest_new_commit["id"]

            # 去重1：同路径+commit_id 不重复创建任务
            if _task_exists(db, settings.intranet_repo_id, file_path, commit_id):
                logger.info(f"[Poller] 跳过重复任务: {file_path}")
                continue

            # 去重2：同一文件路径已有审核记录（不限 commit_id）
            # 防止 Seafile 内部操作（浏览文件库、生成缩略图等）创建新 commit
            # 导致已处理的文件被误判为"新文件"重复提交审核
            if _file_already_processed(db, settings.intranet_repo_id, file_path):
                continue

            token = secrets.token_urlsafe(32)
            task = ReviewTask(
                token=token,
                file_name=file_name,
                file_path=file_path,
                file_size=file_info.get("size", 0),
                repo_id=settings.intranet_repo_id,
                commit_id=commit_id,
                uploader=creator,
                uploader_email=creator_email,
                status=ReviewStatus.PENDING,
                expire_at=expire_at,
            )
            db.add(task)
            db.flush()
            db.refresh(task)
            logger.info(f"[Poller] 创建审核任务 #{task.id}: {file_path} (mtime={file_info.get('mtime')})")

            log_action("system", "task_created", "review_task", task.id,
                       {"file_name": file_name, "uploader": creator, "source": "poller"})

            # 发送审批邮件（异步）
            asyncio.create_task(send_review_notification(task))

        # 7. 更新进度到最新 commit
        latest_commit_id = new_commits[0]["id"]
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


def _file_already_processed(db: Session, repo_id: str, file_path: str) -> bool:
    """
    检查同一文件路径是否已有任何审核任务记录（不限 commit_id）。
    防止因 Seafile 内部操作（如浏览文件库生成缩略图、元数据更新等）
    创建新 commit 导致同一个文件被重复提交审核。
    """
    existing = db.query(ReviewTask).filter(
        ReviewTask.repo_id == repo_id,
        ReviewTask.file_path == file_path,
    ).first()
    if existing:
        logger.info(f"[Poller] 文件已存在审核记录，跳过: {file_path} (已有任务 #{existing.id}, 状态={existing.status.value})")
    return existing is not None


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
