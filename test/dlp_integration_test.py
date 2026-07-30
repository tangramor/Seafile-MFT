"""
DLP 集成核心逻辑测试（无需 scan-worker / 无需网络）

验证内容：
  1. migrate_db() 为 review_tasks 补齐全部 7 个 dlp_* 列
  2. handle_dlp_result(alert)  → dlp_state=alert，写入 severity/hits/checked_at，审计 dlp_alert，通知审核者
  3. handle_dlp_result(clean)  → dlp_state=cleared，触发 run_transfer_pipeline
  4. handle_dlp_result 对已处置任务（非 scanning/"" 状态）的回调应忽略（幂等/防串改）
  5. parse_task_id 正确解析 "mft-task-{id}"

运行：在 seafile-mft 镜像内执行（已含 fastapi/sqlalchemy/httpx 等依赖）。
"""
import os
import sys
import json
import asyncio
import tempfile

# ── 环境准备：临时 sqlite + 开启 DLP ──────────────────
TMPDB = os.path.join(tempfile.gettempdir(), "dlp_it.db")
if os.path.exists(TMPDB):
    os.remove(TMPDB)
os.environ["DATABASE_URL"] = f"sqlite:///{TMPDB}"
os.environ["DLP_ENABLED"] = "true"
os.environ["DLP_ALERT_SEVERITIES"] = "critical,high"

sys.path.insert(0, "/app")

from sqlalchemy import inspect
from datetime import datetime

from app.config import get_settings
from app import models
from app.models import ReviewTask, ReviewStatus, init_engine, migrate_db, get_db
import app.dlp as dlp

PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name)

# ── 1. 引擎 + 迁移 ────────────────────────────────
init_engine(os.environ["DATABASE_URL"])
migrate_db()   # 首次即建表；再跑一次验证幂等
migrate_db()

cols = {c["name"] for c in inspect(models._engine).get_columns("review_tasks")}
expected_dlp = {"dlp_state","dlp_severity","dlp_hits","dlp_reviewed_by",
                "dlp_reviewed_at","dlp_comment","dlp_checked_at"}
print("\n[1] 迁移：dlp_* 列")
check("7 个 dlp_* 列全部存在", expected_dlp.issubset(cols))

# ── stub：拦截真实传输与邮件 ───────────────────────
transferred = []
notified = []
async def _stub_transfer(task_id):
    transferred.append(task_id)
async def _stub_notify(task):
    notified.append(task.id)
dlp.run_transfer_pipeline = _stub_transfer
dlp.send_review_notification = _stub_notify

def make_task(dlp_state="scanning"):
    with get_db() as db:
        import uuid
        t = ReviewTask(
            token=uuid.uuid4().hex,
            file_name="secret_src.zip",
            file_path="/intranet/secret_src.zip",
            repo_id="repo-intra-001",
            uploader="alice",
            status=ReviewStatus.APPROVED,
            dlp_state=dlp_state,
        )
        db.add(t); db.commit(); db.refresh(t)
        return t.id

def get_task(tid):
    with get_db() as db:
        return db.query(ReviewTask).filter(ReviewTask.id == tid).first()

# ── 2. parse_task_id ─────────────────────────────
print("\n[2] parse_task_id")
check('解析 "mft-task-42" == 42', dlp.parse_task_id("mft-task-42") == 42)
check("解析非法前缀 == None", dlp.parse_task_id("garbage") is None)

# ── 3. alert 路径 ────────────────────────────────
print("\n[3] alert 命中 → 挂起等待审核")
tid_alert = make_task("scanning")
alert_payload = {
    "verdict": "alert",
    "uploader": f"mft-task-{tid_alert}",
    "severity": "critical",
    "hit_count": 2,
    "hits": [
        {"rule": "Source_Code_CPP", "meta": {"desc": "C++ 源码指纹"}},
        {"rule": "Internal_Secret", "meta": {"desc": "内部密钥模式"}},
    ],
    "filename": "secret_src.zip",
    "sha256": "deadbeef",
}
asyncio.run(dlp.handle_dlp_result(alert_payload))
t = get_task(tid_alert)
check("dlp_state == 'alert'", t.dlp_state == "alert")
check("dlp_severity == 'critical'", t.dlp_severity == "critical")
check("dlp_hits 存为 JSON 且含 2 条", len(json.loads(t.dlp_hits)) == 2)
check("dlp_checked_at 已写入", t.dlp_checked_at is not None)
check("已通知审核者", tid_alert in notified)
check("alert 未触发传输", tid_alert not in transferred)

# 审计校验
with get_db() as db:
    from app.models import AuditLog
    n = db.query(AuditLog).filter(AuditLog.action == "dlp_alert",
                                  AuditLog.target_id == tid_alert).count()
check("审计记录 dlp_alert 已写入", n >= 1)

# ── 4. clean 路径 ────────────────────────────────
print("\n[4] clean 通过 → 自动放行传输")
tid_clean = make_task("scanning")
clean_payload = {
    "verdict": "clean",
    "uploader": f"mft-task-{tid_clean}",
    "severity": "clean",
    "hit_count": 0,
    "hits": [],
    "filename": "report.pdf",
}
asyncio.run(dlp.handle_dlp_result(clean_payload))
t = get_task(tid_clean)
check("dlp_state == 'cleared'", t.dlp_state == "cleared")
check("clean 触发了传输管线", tid_clean in transferred)

# ── 5. 低危命中（未达告警阈值）→ 自动放行 ──────────
print("\n[5] 低危命中（low，未达阈值）→ 自动放行")
tid_low = make_task("scanning")
low_payload = {
    "verdict": "alert", "uploader": f"mft-task-{tid_low}",
    "severity": "low", "hit_count": 1, "hits": [{"rule": "x"}],
}
asyncio.run(dlp.handle_dlp_result(low_payload))
t = get_task(tid_low)
check("低危 dlp_state == 'cleared'", t.dlp_state == "cleared")
check("低危触发了传输", tid_low in transferred)

# ── 6. 幂等/防串改：已处置任务忽略回调 ─────────────
print("\n[6] 已处置任务的回调应被忽略")
tid_done = make_task("released")   # 已被审核者放行
before = get_task(tid_done).dlp_state
asyncio.run(dlp.handle_dlp_result({
    "verdict": "alert", "uploader": f"mft-task-{tid_done}",
    "severity": "critical", "hits": [{"rule": "y"}],
}))
after = get_task(tid_done).dlp_state
check("released 任务状态未被回调覆盖", before == after == "released")

# ── 汇总 ─────────────────────────────────────────
print("\n" + "="*48)
print(f"结果：PASS={len(PASS)}  FAIL={len(FAIL)}")
if FAIL:
    print("失败项：", FAIL)
    sys.exit(1)
print("全部通过 ✅")
