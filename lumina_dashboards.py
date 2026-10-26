"""Grafana dashboard for the Lumina PawFishing (web) playtest: lumina-playtest.json.

Definitions: lumina_pawfishing docs/ANALYTICS.md. Run `python lumina_dashboards.py` to rewrite the
JSON; tests/test_lumina_analytics.py runs every panel query against a seeded database.
"""
import json
from pathlib import Path

from lumina_analytics import END_AFTER_S

OUT = Path(__file__).parent / 'grafana/provisioning/dashboards/lumina-playtest.json'
DS = {'type': 'frser-sqlite-datasource', 'uid': '${DS_SQLITE}'}

# The sessions in view: started in the time range, of the chosen batch, audience and scope.
BASE = """WITH RECURSIVE s AS (SELECT * FROM lumina_sessions_v
  WHERE julianday(first_at) BETWEEN julianday(${__from}/1000,'unixepoch') AND julianday(${__to}/1000,'unixepoch')
    AND (${tb:sqlstring}='__all__' OR COALESCE(tb,'(无)')=${tb:sqlstring})
    AND (${audience:sqlstring}='all' OR (${audience:sqlstring}='external' AND internal=0 AND fresh=0)
         OR (${audience:sqlstring}='internal' AND (internal=1 OR fresh=1)))
    AND (${scope:sqlstring}='all' OR session_n=1))"""

BUCKETS = "(VALUES (0,'加载中'),(1,'0–30s'),(2,'30s–1m'),(3,'1–3m'),(4,'3–5m'),(5,'5–10m'),(6,'10–20m'),(7,'20–30m'),(8,'30–60m'),(9,'60m+'))"
PLACE = "CASE WHEN ready_at IS NULL THEN '加载：'||COALESCE(stage,'boot') WHEN chain_done=1 THEN '主线已完成' ELSE COALESCE(last_task,'?')||' / '||COALESCE(last_step,'-') END"

DESCRIPTION = (f'有效时间=前台且距最近一次按键/点击不足 120 秒（鼠标移动不算），加载期不计。会话在最后一条事件 {END_AFTER_S // 60} 分钟后算结束；'
               '跳出率=在该时长段结束的会话 ÷ 进入该段的会话（风险率），只算已结束的会话。“首会话”是该浏览器第一次打开。'
               '离开位置是最后可观测的任务步骤 / 加载阶段，不等于离开原因（第 2 期归因）。样本少时看人数，不看比例。')


def median(expr, where):
    return (f"{BASE} SELECT ROUND(v,1) AS value FROM (SELECT {expr} v, ROW_NUMBER() OVER (ORDER BY {expr}) rn, COUNT(*) OVER () n "
            f"FROM s WHERE {where}) WHERE rn=(n+1)/2")


STATS = [
    ('会话', f"{BASE} SELECT COUNT(*) AS value FROM s", 'none'),
    ('玩家', f"{BASE} SELECT COUNT(DISTINCT player_id) AS value FROM s", 'none'),
    ('加载成功率', f"{BASE} SELECT ROUND(100.0*SUM(ready_at IS NOT NULL)/NULLIF(SUM(ended=1 OR ready_at IS NOT NULL),0),1) AS value FROM s", 'percent'),
    ('加载耗时中位（秒）', median('page_ms/1000.0', 'ready_at IS NOT NULL'), 's'),
    ('有效时长中位（分钟）', median('max_eff_s/60.0', 'ended=1 AND ready_at IS NOT NULL'), 'none'),
    ('回访率（首会话满 24h）', f"""{BASE} SELECT ROUND(100.0*SUM(EXISTS(SELECT 1 FROM lumina_sessions o WHERE o.player_id=s.player_id AND o.session_n>1))
        /NULLIF(COUNT(*),0),1) AS value FROM s WHERE session_n=1 AND julianday('now')-julianday(first_at)>=1""", 'percent'),
]

SURVIVAL = f"""{BASE}, m(x) AS (SELECT 0 UNION ALL SELECT x+1 FROM m WHERE x<60),
  e AS (SELECT max_eff_s FROM s WHERE ended=1 AND ready_at IS NOT NULL)
SELECT printf('%.1f', x/2.0) AS 有效分钟, ROUND(100.0*SUM(e.max_eff_s>=x*30)/COUNT(*),1) AS 仍在玩
FROM m JOIN e GROUP BY x ORDER BY x"""

HAZARD = f"""{BASE}, b(i,name) AS {BUCKETS}, e AS (SELECT * FROM s WHERE ended=1),
  t AS (SELECT b.i, b.name, (SELECT COUNT(*) FROM e WHERE bucket_i>=b.i) reached, (SELECT COUNT(*) FROM e WHERE bucket_i=b.i) gone FROM b),
  top AS (SELECT bucket_i, {PLACE} place, COUNT(*) n, ROW_NUMBER() OVER (PARTITION BY bucket_i ORDER BY COUNT(*) DESC) rn FROM e GROUP BY bucket_i, place)
SELECT t.name AS 时长段, t.reached AS 进入, t.gone AS 在此离开, ROUND(100.0*t.gone/NULLIF(t.reached,0),1) AS 跳出率,
  top.place AS 最多停在, top.n AS 人数
FROM t LEFT JOIN top ON top.bucket_i=t.i AND top.rn=1 ORDER BY t.i"""

FUNNEL = f"""{BASE}, st AS (SELECT t.* FROM lumina_steps t JOIN s USING(session_id)),
  g AS (SELECT task_i, step_i, task, step, MAX(label) label, COUNT(*) reached, SUM(done_eff IS NOT NULL) done FROM st GROUP BY step),
  d AS (SELECT step, dur_eff, ROW_NUMBER() OVER (PARTITION BY step ORDER BY dur_eff) rn, COUNT(*) OVER (PARTITION BY step) n
        FROM st WHERE dur_eff IS NOT NULL AND resumed=0),
  p AS (SELECT step, MAX(CASE WHEN rn=(n+1)/2 THEN dur_eff END) p50, MAX(CASE WHEN rn=(9*n+9)/10 THEN dur_eff END) p90, MAX(n) n FROM d GROUP BY step),
  gone AS (SELECT last_step step, COUNT(*) n FROM s WHERE ended=1 AND ready_at IS NOT NULL AND chain_done=0 GROUP BY last_step)
SELECT g.task_i+1 AS 任务序, COALESCE(g.label, g.task||' / '||g.step) AS 步骤, g.reached AS 到达会话, g.done AS 完成,
  ROUND(100.0*g.done/g.reached,1) AS 完成率, p.p50 AS P50有效秒, p.p90 AS P90有效秒, p.n AS 计时样本, COALESCE(gone.n,0) AS 停在此步离开
FROM g LEFT JOIN p USING(step) LEFT JOIN gone USING(step) ORDER BY g.task_i, g.step_i"""

PLACES = f"""{BASE} SELECT {PLACE} AS 离开位置, COALESCE(last_room,'岛上') AS 房间, COUNT(*) AS 离开会话,
  ROUND(AVG(max_eff_s)/60,1) AS 平均有效分钟, ROUND(AVG(max_afk_s)/60,1) AS 平均挂机分钟, SUM(errors>0) AS 有报错
FROM s WHERE ended=1 GROUP BY 1,2 ORDER BY 3 DESC LIMIT 40"""

ERRORS = f"""{BASE} SELECT json_extract(e.data_json,'$.message') AS 错误, json_extract(e.data_json,'$.at') AS 位置, e.type AS 类型,
  COUNT(*) AS 次数, COUNT(DISTINCT e.session_id) AS 会话, MAX(e.build) AS 构建
FROM lumina_events e JOIN s USING(session_id) WHERE e.type IN ('error','webgl_lost','load_fail') GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 30"""

SESSIONS = f"""{BASE} SELECT datetime(first_at,'+8 hours') AS 开始北京时间, substr(player_id,1,8) AS 玩家, session_n AS 第几次, COALESCE(tb,'') AS 批次,
  platform AS 平台, build AS 构建, ROUND(page_ms/1000.0,1) AS 加载秒, ROUND(max_eff_s/60,1) AS 有效分钟, ROUND(max_afk_s/60,1) AS 挂机分钟,
  bucket AS 时长段, {PLACE} AS 最后位置, act_type AS 最后动作, errors AS 错误, CASE WHEN ended=1 THEN '已结束' ELSE '进行中' END AS 状态,
  session_id AS 会话
FROM s ORDER BY first_at DESC LIMIT 200"""


def _target(sql, fmt='table'):
    return [{'refId': 'A', 'queryType': 'table', 'queryText': sql, 'rawQueryText': sql, 'timeColumns': [], 'format': fmt}]


def panels():
    out = [{'id': 1, 'title': '统计口径', 'type': 'text', 'gridPos': {'x': 0, 'y': 0, 'w': 24, 'h': 3},
            'options': {'mode': 'markdown', 'content': DESCRIPTION}}]
    for i, (title, sql, unit) in enumerate(STATS):
        out.append({'id': 10 + i, 'title': title, 'type': 'stat', 'datasource': DS, 'targets': _target(sql),
                    'gridPos': {'x': i * 4, 'y': 3, 'w': 4, 'h': 4},
                    'fieldConfig': {'defaults': {'unit': unit, 'noValue': '—'}, 'overrides': []},
                    'options': {'reduceOptions': {'calcs': ['lastNotNull'], 'fields': '', 'values': False}, 'colorMode': 'none', 'graphMode': 'none'}})
    out.append({'id': 20, 'title': '留存曲线：已结束会话按有效时长仍在玩的比例（%）', 'type': 'barchart', 'datasource': DS, 'targets': _target(SURVIVAL),
                'gridPos': {'x': 0, 'y': 7, 'w': 12, 'h': 9},
                'fieldConfig': {'defaults': {'unit': 'percent', 'min': 0, 'max': 100}, 'overrides': []},
                'options': {'xField': '有效分钟', 'orientation': 'vertical', 'showValue': 'never', 'legend': {'showLegend': False}, 'xTickLabelSpacing': 100}})
    out.append({'id': 21, 'title': '各时长段跳出率（风险率）', 'type': 'table', 'datasource': DS, 'targets': _target(HAZARD),
                'gridPos': {'x': 12, 'y': 7, 'w': 12, 'h': 9}})
    out.append({'id': 30, 'title': '任务步骤漏斗', 'description': 'P50/P90 只用本会话内从开到完成的步骤（不含读档时已开的）。停在此步离开=已结束、未完成主线、最后位置是这一步。',
                'type': 'table', 'datasource': DS, 'targets': _target(FUNNEL), 'gridPos': {'x': 0, 'y': 16, 'w': 24, 'h': 11}})
    out.append({'id': 40, 'title': '离开位置', 'type': 'table', 'datasource': DS, 'targets': _target(PLACES), 'gridPos': {'x': 0, 'y': 27, 'w': 12, 'h': 10}})
    out.append({'id': 41, 'title': '错误', 'type': 'table', 'datasource': DS, 'targets': _target(ERRORS), 'gridPos': {'x': 12, 'y': 27, 'w': 12, 'h': 10}})
    out.append({'id': 50, 'title': '会话列表（最近 200）', 'type': 'table', 'datasource': DS, 'targets': _target(SESSIONS), 'gridPos': {'x': 0, 'y': 37, 'w': 24, 'h': 12}})
    return out


def dashboard():
    return {
        'uid': 'lumina-playtest', 'title': 'Lumina 网页版测试：时长与跳出', 'schemaVersion': 39, 'version': 1,
        'timezone': 'Asia/Shanghai', 'refresh': '1m', 'time': {'from': 'now-7d', 'to': 'now'}, 'tags': ['Lumina', 'playtest'],
        'templating': {'list': [
            {'name': 'DS_SQLITE', 'type': 'datasource', 'query': 'frser-sqlite-datasource', 'current': {'text': 'SQLite', 'value': 'SQLite'}, 'hide': 2},
            {'name': 'tb', 'label': '测试批次', 'type': 'query', 'datasource': DS, 'refresh': 2,
             'query': "SELECT '全部' AS __text, '__all__' AS __value UNION ALL SELECT DISTINCT COALESCE(tb,'(无)'), COALESCE(tb,'(无)') FROM lumina_sessions",
             'current': {'text': '全部', 'value': '__all__'}},
            {'name': 'audience', 'label': '玩家', 'type': 'custom', 'query': '外部测试玩家 : external,内部 / 调试 : internal,全部 : all',
             'current': {'text': '外部测试玩家', 'value': 'external'}},
            {'name': 'scope', 'label': '会话', 'type': 'custom', 'query': '仅首会话 : first,全部会话 : all',
             'current': {'text': '仅首会话', 'value': 'first'}},
        ]},
        'panels': panels(),
    }


if __name__ == '__main__':
    OUT.write_text(json.dumps(dashboard(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(OUT)
