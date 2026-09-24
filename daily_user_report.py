"""按北京时间生成昨日用户日报；共享分析库只读，成功发送后持久化去重。"""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo

import httpx

TZ = ZoneInfo("Asia/Shanghai")


def yesterday(now=None):
    """按北京时间确定完整统计日，不使用过去 24 小时滑动窗口。"""
    return ((now or dt.datetime.now(TZ)).astimezone(TZ).date() - dt.timedelta(days=1))


def collect(db, day):
    """复用 Grafana 分析视图；跨平台去重，排除测试身份与匿名身份。"""
    date = day.isoformat()
    previous = (day - dt.timedelta(days=1)).isoformat()
    deadline = time.monotonic() + 30
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    try:
        # 临时表仅在此连接内存中，避免多项指标反复执行历史视图。
        db.execute("PRAGMA temp_store=MEMORY")
        db.execute("""CREATE TEMP TABLE daily_activity AS
            SELECT DISTINCT a.user_id, a.client_platform, a.activity_day
            FROM analytics_activity a LEFT JOIN user_sessions u ON u.user_id=a.user_id
            WHERE a.activity_day IN (?,?) AND a.client_platform!='editor'
              AND a.is_development_build=0 AND COALESCE(u.is_developer,0)=0
              AND a.user_id NOT IN ('','unknown','anonymous','anonymous_user')""", (date, previous))
        db.execute("""CREATE TEMP TABLE daily_cohorts AS
            SELECT user_id,first_day FROM analytics_first_entry
            WHERE cohort_scope='production' AND first_day IN (?,?)""", (date, previous))
        dau = db.execute("SELECT COUNT(DISTINCT user_id) FROM daily_activity WHERE activity_day=?", (date,)).fetchone()[0]
        platforms = dict(db.execute("SELECT client_platform,COUNT(DISTINCT user_id) FROM daily_activity WHERE activity_day=? GROUP BY client_platform", (date,)))
        new = db.execute("SELECT COUNT(*) FROM daily_cohorts WHERE first_day=?", (date,)).fetchone()[0]
        cohort, returned = db.execute("""SELECT COUNT(*),COALESCE(SUM(EXISTS(
            SELECT 1 FROM daily_activity a WHERE a.user_id=c.user_id AND a.activity_day=?)),0)
            FROM daily_cohorts c WHERE c.first_day=?""", (date, previous)).fetchone()
        sessions = db.execute("""SELECT COUNT(*) FROM analytics_sessions
            WHERE date(started_at,'+8 hours')=? AND client_platform!='editor'
              AND is_development_build=0 AND is_developer=0""", (date,)).fetchone()[0]
        return dict(date=date, dau=dau, new_users=new, sessions=sessions,
                    platforms=platforms, d1_cohort_date=previous,
                    d1_cohort=cohort, d1_returned=returned)
    finally:
        db.set_progress_handler(None, 0)
        db.execute("DROP TABLE IF EXISTS temp.daily_activity")
        db.execute("DROP TABLE IF EXISTS temp.daily_cohorts")


def report_text(data):
    """缺少留存样本时显示无样本，不伪造 0% 留存。"""
    names = {"webgl":"WebGL", "windows":"Windows", "unknown":"未知/历史未上报", "other":"其他"}
    platform = "、".join(f"{names.get(k, k)} {v}" for k,v in data['platforms'].items()) or "无活跃记录"
    cohort = data['d1_cohort']
    retention = f"{data['d1_returned']}/{cohort}（{data['d1_returned']/cohort:.1%}）" if cohort else "无新增样本"
    return (f"📊 用户日报 | {data['date']}（北京时间）\n"
            f"日活用户：{data['dau']}\n新增用户：{data['new_users']}\n"
            f"当日开始的会话：{data['sessions']}\n平台日活：{platform}\n"
            f"次日留存（{data['d1_cohort_date']} 新增）：{retention}\n\n"
            "口径：登录/前台心跳活跃，跨平台按用户去重；各平台人数不可相加。"
            "排除 Editor、开发包和开发者；保留未知平台。新增按正式用户首次入岛。\n"
            "仅统计已入库数据；未上报或延迟上报不计入本次结果。\n"
            "详情：http://47.89.135.221:9001/d/gameplay-overview")


def send(text, day):
    """发送到已配置的报表群，仅在飞书业务码成功时返回消息 ID。"""
    app_id = os.environ['FEISHU_BOT_API_KEY']
    secret = os.environ['FEISHU_BOT_API_SECRET']
    group = os.environ['FEISHU_CHAT_ID']
    if not group.startswith('oc_'):
        raise ValueError('FEISHU_CHAT_ID must be a group chat ID')
    with httpx.Client(timeout=20) as client:
        r = client.post('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
                        json={'app_id':app_id, 'app_secret':secret})
        r.raise_for_status()
        token = r.json()
        if token.get('code') != 0:
            raise RuntimeError(f"Feishu authentication failed: code={token.get('code')}")
        # 相同统计日与接收群重试使用同一 UUID，覆盖发送成功但本地落盘中断的窗口。
        key = hashlib.sha256(f'user-daily:{day}:{group}'.encode()).hexdigest()[:32]
        r = client.post('https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id',
                        headers={'Authorization':f"Bearer {token['tenant_access_token']}"},
                        json={'receive_id':group, 'msg_type':'text', 'uuid':key,
                              'content':json.dumps({'text':text}, ensure_ascii=False)})
        r.raise_for_status()
        result = r.json()
        if result.get('code') != 0:
            raise RuntimeError(f"Feishu send failed: code={result.get('code')}")
        return result['data']['message_id']


def run(day, db_path, output, dry_run=False):
    """串行发送、保留报表和成功回执；失败非零退出供定时任务重试。"""
    output.mkdir(parents=True, exist_ok=True)
    with (output/'daily-user-report.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        group = os.getenv('FEISHU_CHAT_ID', '')
        suffix = hashlib.sha256(group.encode()).hexdigest()[:12]
        marker = output/f'user-daily-{day}-{suffix}.sent.json'
        if marker.exists() and not dry_run:
            print(f'already_sent date={day}', flush=True)
            return
        started = time.monotonic()
        with sqlite3.connect(f'{Path(db_path).resolve().as_uri()}?mode=ro', uri=True, timeout=5) as db:
            data = collect(db, day)
        print(f'query_seconds={time.monotonic() - started:.3f}', flush=True)
        text = report_text(data)
        if dry_run:
            print(text)
            return
        (output/f'user-daily-{day}.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        message_id = send(text, day)
        temp = marker.with_suffix('.tmp')
        temp.write_text(json.dumps({'date':str(day), 'message_id':message_id}), encoding='utf-8')
        temp.replace(marker)
        print(f'sent date={day} message_id={message_id}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', type=dt.date.fromisoformat, default=yesterday())
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    run(args.date, os.getenv('DB_PATH','/app/data/conversations.db'),
        Path(os.getenv('REPORT_OUTPUT','/app/output/user-reports')), args.dry_run)
