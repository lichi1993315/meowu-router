"""Theater reports use client observations; missing observations are never refusals."""

def optional_filter(column, variable):
    # Grafana sqlstring formats an empty textbox as no SQL tokens, not ''.
    value = "COALESCE(json_extract(json_array(${" + variable + ":sqlstring}), '$[0]'),'')"
    return f"({value}='' OR {column}={value})"


def panels(panel, events, limit):
    user_filter = optional_filter('user_id', 'user_id')
    session_filter = optional_filter('session_id', 'session_id')
    event_filter = optional_filter("json_extract(payload_json,'$.theater_event_id')", 'theater_event_id')
    base = events + f""", t AS (
 SELECT *,json_extract(payload_json,'$.theater_event_id') theater_event_id,
 json_extract(payload_json,'$.theater_type') theater_type,
 json_extract(payload_json,'$.view_id') view_id,
 json_extract(payload_json,'$.decision_id') decision_id,
 json_extract(payload_json,'$.phase') phase,
 json_extract(payload_json,'$.choice_class') choice_class,
 json_extract(payload_json,'$.reason') reason
 FROM e WHERE event_type IN ('theater_view','theater_choice','theater_line','theater_exit')
 AND {user_filter} AND {session_filter} AND {event_filter}
) """
    # Only the first invitation in each viewing attempt measures immediate refusal.
    funnel = base + """, invitations AS (
 SELECT *,ROW_NUMBER() OVER (PARTITION BY user_id,session_id,view_id ORDER BY julianday(occurred_at),sequence,event_id) rn
 FROM t WHERE event_type='theater_view' AND phase='invitation'
), choices AS (
 SELECT *,ROW_NUMBER() OVER (PARTITION BY user_id,session_id,view_id,decision_id ORDER BY julianday(occurred_at),sequence,event_id) rn
 FROM t WHERE event_type='theater_choice'
), attempts AS (
 SELECT i.user_id,i.session_id,i.view_id,i.theater_type,json_extract(i.payload_json,'$.title') title,
 c.choice_class, MAX(CASE WHEN x.reason='escape' AND json_extract(x.payload_json,'$.playback_started')=1 THEN 1 ELSE 0 END) escape_midway
 FROM invitations i LEFT JOIN choices c ON c.user_id=i.user_id AND c.session_id=i.session_id
 AND c.view_id=i.view_id AND c.decision_id=i.decision_id AND c.rn=1
 LEFT JOIN t x ON x.user_id=i.user_id AND x.session_id=i.session_id AND x.view_id=i.view_id AND x.event_type='theater_exit'
 WHERE i.rn=1 GROUP BY i.user_id,i.session_id,i.view_id
), rates AS (
 SELECT theater_type,MAX(title) 小剧场,COUNT(*) 邀请展示数,COUNT(DISTINCT user_id) 玩家数,
 SUM(CASE WHEN choice_class='continue' THEN 1 ELSE 0 END) 选择继续数,SUM(CASE WHEN choice_class='decline' THEN 1 ELSE 0 END) 直接拒绝数,
 SUM(choice_class IS NULL) 未观察到选择数,SUM(escape_midway) 开演后Esc退出数,
 ROUND(100.0*SUM(CASE WHEN choice_class='continue' THEN 1 ELSE 0 END)/COUNT(*),2) 继续率,
 ROUND(100.0*SUM(CASE WHEN choice_class='decline' THEN 1 ELSE 0 END)/COUNT(*),2) 直接拒绝率
 FROM attempts GROUP BY theater_type
) """
    meaning = '按玩家、会话和本次观看去重，只取首次邀请及其首次点击；返回邀请页不会再次计入。继续率=选择继续/邀请展示数；未选择、Esc、超时均不推算为拒绝。记录点击意图，不表示服务端成功开演。'
    return [
        panel(1,'采集覆盖与最近记录',base+"SELECT CASE WHEN COUNT(*)=0 THEN '所选范围尚无小剧场埋点，不能计算玩家比例' ELSE '已收到小剧场埋点' END 采集状态,COUNT(*) 事件数,COUNT(DISTINCT user_id) 玩家数,MIN(occurred_at) 首条时间,MAX(occurred_at) 最近时间 FROM t",description='旧版本没有这些埋点。空数据不代表拒绝率或退出率为零。'),
        panel(2,'最愿意继续的 5 种小剧场',funnel+"SELECT * FROM rates WHERE 邀请展示数>=MAX(1,CAST(COALESCE(json_extract(json_array(${min_invitations:sqlstring}),'$[0]'),'10') AS INTEGER)) ORDER BY 继续率 DESC,选择继续数 DESC,邀请展示数 DESC,theater_type LIMIT 5",description=meaning+' 默认至少 10 次邀请；可调整样本门槛。'),
        panel(3,'所有类型：邀请、直接拒绝与未选择',funnel+'SELECT * FROM rates ORDER BY 邀请展示数 DESC,theater_type',description=meaning),
        panel(4,'退出方式与阶段',base+"SELECT theater_type,reason 退出方式,phase 退出阶段,json_extract(payload_json,'$.playback_started') 已经开演,COUNT(*) 退出次数,COUNT(DISTINCT user_id) 玩家数,ROUND(AVG(json_extract(payload_json,'$.elapsed_seconds')),2) 平均观看秒 FROM t WHERE event_type='theater_exit' GROUP BY 1,2,3,4 ORDER BY 退出次数 DESC",description='escape 是按 Esc；exit_button 是退出按钮；其他原因单独展示。Esc 只退出本地观看，不等于服务端剧情取消。'),
        panel(5,'词汇分类分布与其他占比',base+"SELECT theater_type,json_extract(payload_json,'$.prompt_text') 原问题,COUNT(*) 分类选择次数,COUNT(DISTINCT user_id) 玩家数,SUM(json_extract(payload_json,'$.vocab_type')='person') 人物,SUM(json_extract(payload_json,'$.vocab_type')='activity') 活动,SUM(json_extract(payload_json,'$.vocab_type')='object') 物品,SUM(json_extract(payload_json,'$.vocab_type')='other') 其他,ROUND(100.0*SUM(json_extract(payload_json,'$.vocab_type')='other')/COUNT(*),2) 其他占比 FROM t WHERE event_type='theater_choice' AND phase='vocab_category' GROUP BY 1,2 ORDER BY 分类选择次数 DESC",description='只统计玩家明确选择分类的操作；普通提交中默认的 Other 不计入。频繁选择其他是排查问题文案和分类覆盖的线索，不直接证明设计错误。'),
        panel(6,'逐条输入、输出与选择',base+"SELECT occurred_at 时间,theater_type 小剧场类型,event_type 记录类型,json_extract(payload_json,'$.speaker_name') 发言者,json_extract(payload_json,'$.prompt_text') 问题,json_extract(payload_json,'$.content') 原文,json_extract(payload_json,'$.option_label') 所选选项,json_extract(payload_json,'$.vocab_type') 词汇分类,json_extract(payload_json,'$.source') 来源,user_id,theater_event_id,session_id,view_id,decision_id,phase 阶段,json_extract(payload_json,'$.action') 操作,json_extract(payload_json,'$.options') 当时选项,payload_json FROM t ORDER BY julianday(occurred_at) DESC,sequence DESC,event_id DESC"+limit,description='theater_view=客户端展示的问题/选项；theater_choice=玩家提交；theater_line=服务端实际开播台词，不保证客户端收到或玩家阅读；theater_exit=退出。原文不截断。可按玩家、会话、小剧场事件 ID 筛选并翻页。'),
    ]
