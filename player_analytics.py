"""Player-centric analytics. Current state is a snapshot, never a sum of events."""
import copy


def extend_dashboards(result, dashboard, panel, scope, user, limit, time_range):
    # Apply origin/test filters before per-player aggregation. Dates only filter behaviors.
    facts = f"""WITH e0 AS NOT MATERIALIZED (SELECT * FROM analytics_events WHERE {scope}),
    s AS MATERIALIZED (SELECT * FROM analytics_sessions WHERE {scope}),
    ids AS (SELECT user_id FROM s UNION SELECT user_id FROM e0),
    states AS (SELECT *,ROW_NUMBER() OVER(PARTITION BY user_id ORDER BY julianday(occurred_at) DESC,sequence DESC,event_id DESC) rn
        FROM e0 WHERE event_type='player_state_snapshot'),
    legacy AS (SELECT g.*,ROW_NUMBER() OVER(PARTITION BY user_id ORDER BY julianday(real_time_started_iso) DESC,imported_at DESC) rn
        FROM gameplay_sessions g WHERE EXISTS(SELECT 1 FROM s WHERE s.user_id=g.user_id AND s.session_id=g.session_id)),
    days AS (SELECT user_id,COUNT(*) play_days FROM
        (SELECT DISTINCT user_id,archive_id,game_day FROM e0 WHERE NULLIF(archive_id,'') IS NOT NULL AND game_day IS NOT NULL) GROUP BY user_id),
    p AS (SELECT ids.user_id,COALESCE(NULLIF(json_extract(st.payload_json,'$.nickname'),''),NULLIF(u.nickname,''),NULLIF(u.player_name,''),ids.user_id) nickname,
        (SELECT SUM(duration_sec) FROM s WHERE s.user_id=ids.user_id) play_seconds,
        (SELECT first_entry_at FROM analytics_first_entry f WHERE f.user_id=ids.user_id AND f.cohort_scope=CASE WHEN '${{test_data}}' IN ('include','only') OR ('${{test_data}}'='auto' AND ('editor' IN (${{client_platform:sqlstring}}) OR '__all__' IN (${{client_platform:sqlstring}}))) THEN 'all' ELSE 'production' END) first_login,
        (SELECT MAX(started_at) FROM s WHERE s.user_id=ids.user_id) latest_login,
        d.play_days,COALESCE(json_extract(st.payload_json,'$.money'),l.money_end,l.money_start) money,
        COALESCE(json_extract(st.payload_json,'$.island_level'),l.island_level_max) island_level,
        json_array_length(json_extract(st.payload_json,'$.cats')) cat_count,
        st.payload_json state,st.occurred_at state_at,
        json_extract(st.payload_json,'$.cat_house_count') house_count,
        json_extract(st.payload_json,'$.cat_house_capacity') house_capacity,
        json_extract(st.payload_json,'$.cat_house_occupied') house_occupied
        FROM ids LEFT JOIN user_sessions u USING(user_id) LEFT JOIN states st ON st.user_id=ids.user_id AND st.rn=1
        LEFT JOIN legacy l ON l.user_id=ids.user_id AND l.rn=1 LEFT JOIN days d USING(user_id)),
    e AS (SELECT * FROM e0 WHERE (occurred_at IS NOT NULL AND {time_range('occurred_at')})
        OR (occurred_at IS NULL AND '${{behavior_period}}'='all')),
    players AS (SELECT * FROM p WHERE {user}),
    events AS (SELECT * FROM e WHERE {user}) """
    # The default is lifetime; explicit period mode honors Grafana's picker.
    facts = facts.replace(f"(occurred_at IS NOT NULL AND {time_range('occurred_at')})", f"('${{behavior_period}}'='all' OR {time_range('occurred_at')})")
    def q(pid,title,sql,description=''):
        return panel(pid,title,facts+sql,description=description or '累计按玩家去重；当前状态取最新快照。缺失显示未采集，数值零保留。行为可切换期间，日期为北京时间。')
    columns = "nickname 昵称,user_id,ROUND(play_seconds/60.0,2) 游玩分钟,play_days 游戏日数,island_level 岛屿等级,datetime(first_login,'+8 hours') 首次登录,datetime(latest_login,'+8 hours') 最新登录,money 金币余额,cat_count 猫数量,state_at 快照时间"
    overview = [q(100,'玩家数与新增',"""SELECT COUNT(*) 总玩家数,
        SUM(date(first_login,'+8 hours')=date('now','+8 hours','-1 day')) 昨日新增,
        SUM(date(first_login,'+8 hours')>=date('now','+8 hours','-7 days') AND date(first_login,'+8 hours')<date('now','+8 hours')) 七日新增,
        SUM(date(first_login,'+8 hours')>=date('now','+8 hours','-30 days') AND date(first_login,'+8 hours')<date('now','+8 hours')) 三十日新增 FROM players"""),
        q(101,'金币与猫总量',"SELECT SUM(money) 总金币余额,COUNT(money) 金币有效玩家数,SUM(cat_count) 拥有猫总数,COUNT(cat_count) 猫有效玩家数,COUNT(*) 总玩家数 FROM players")]
    for pid,title,col,div in [(102,'游玩时长','play_seconds',60),(103,'游玩天数（实际参与游戏日）','play_days',1),(104,'岛屿等级','island_level',1)]:
        overview.append(q(pid,title,f"""SELECT ROUND(AVG(v)/{div}.0,2) 平均数,
            ROUND(AVG(CASE WHEN rn IN ((n+1)/2,(n+2)/2) THEN v END)/{div}.0,2) 中位数,COUNT(*) 有效玩家数
            FROM (SELECT {col} v,ROW_NUMBER() OVER(ORDER BY {col}) rn,COUNT(*) OVER() n FROM players WHERE {col} IS NOT NULL)"""))
    overview += [q(105,'各职业平均等级',"SELECT j.key 职业,AVG(j.value) 平均等级,COUNT(j.value) 有效玩家数 FROM players,json_each(state,'$.careers') j GROUP BY j.key")]
    base = [q(100,'玩家基础数据',f'SELECT {columns} FROM players'),
        q(101,'各职业等级',"SELECT j.key 职业,j.value 等级 FROM players,json_each(state,'$.careers') j"),
        q(102,'拥有的猫（包括初始猫与猫舍）',"SELECT json_extract(c.value,'$.cat_uid') 猫UID,json_extract(c.value,'$.cat_name') 猫名,json_extract(c.value,'$.breed') 品种,json_extract(c.value,'$.template_id') 模板ID,json_extract(c.value,'$.level') 等级,json_extract(c.value,'$.skill_points') 剩余技能点,json_extract(c.value,'$.skills') 工作等级,json_extract(c.value,'$.location') 位置 FROM players,json_each(state,'$.cats') c"),
        q(103,'各部位当前穿搭',"SELECT json_extract(c.value,'$.slot') 部位,json_extract(c.value,'$.item_name') 服装 FROM players,json_each(state,'$.outfits') c"),
        q(104,'猫舍当前状态',"SELECT house_count 猫舍数量,house_capacity 容量,house_occupied 已占用,house_capacity-house_occupied 空余格子 FROM players")]
    rankings = [
        ('购买最多商品','shop_purchase',"COALESCE(json_extract(payload_json,'$.item_name'),json_extract(payload_json,'$.item.item_name'))",'1'),
        ('最常招募的猫','cat_adopted',"COALESCE(json_extract(payload_json,'$.cat.breed'),json_extract(payload_json,'$.cat.skin'))",'1'),
        ('服装店购买','shop_purchase',"json_extract(payload_json,'$.item_name')","json_extract(payload_json,'$.shop_type')='clothing'"),
        ('杂货店购买','shop_purchase',"json_extract(payload_json,'$.item_name')","json_extract(payload_json,'$.shop_type')='variety'"),
        ('服装店冻结','shop_freeze_changed',"json_extract(payload_json,'$.item_name')","json_extract(payload_json,'$.shop_type')='clothing' AND json_extract(payload_json,'$.frozen')=1"),
        ('杂货店冻结','shop_freeze_changed',"json_extract(payload_json,'$.item_name')","json_extract(payload_json,'$.shop_type')='variety' AND json_extract(payload_json,'$.frozen')=1"),
        ('种植种子','farming_plant',"json_extract(payload_json,'$.seed_name')",'1'),
        ('做菜','cooking_completed',"COALESCE(json_extract(payload_json,'$.dish_name'),json_extract(payload_json,'$.recipe_name'))",'1'),
        ('建造建筑','building_placed',"json_extract(payload_json,'$.building_name')",'1')]
    for i,(title,event,name,where) in enumerate(rankings):
        identity={"cat_adopted":"$.template_id","farming_plant":"$.seed_id","cooking_completed":"$.recipe_id","building_placed":"$.building_id"}.get(event,"$.item_id")
        condition="event_type IN ('cooking_complete','cooking_completed')" if event=='cooking_completed' else f"event_type='{event}'"
        extra=",SUM(COALESCE(json_extract(payload_json,'$.quantity'),1)) 件数" if event=='shop_purchase' else ''
        sql=f"SELECT COALESCE(NULLIF({name},''),'未采集名称') 名称,COUNT(*) 次数,COUNT(DISTINCT user_id) 参与玩家数{extra} FROM events WHERE {condition} AND actor_is_player=1 AND {where} GROUP BY COALESCE(json_extract(payload_json,'{identity}'),{name}) ORDER BY 次数 DESC,名称 LIMIT 5"
        overview.append(q(120+i,title+' Top 5',sql));base.append(q(120+i,title+' Top 5',sql))
    behaviors = [
        (140,'玩家行为 Top 5',"SELECT event_type 行为,COUNT(*) 次数 FROM events WHERE actor_is_player=1 AND (behavior_stat=1 OR (behavior_stat IS NULL AND event_type IN ('farming_plant','farming_water','farming_till','farming_harvest','fishing_catch','cooking_completed','building_placed','shop_purchase','cat_conversation','player_sleep'))) GROUP BY 1 ORDER BY 次数 DESC,行为 LIMIT 5"),
        (141,'体力消耗 Top 5',"""SELECT action 行为,SUM(amount) 体力,COUNT(*) 扣除次数,SUM(legacy) 旧版样本数 FROM (
            SELECT json_extract(payload_json,'$.action') action,json_extract(payload_json,'$.amount') amount,0 legacy FROM events WHERE event_type='stamina_spent'
            UNION ALL SELECT CASE WHEN event_type='fishing_catch' THEN 'fishing' ELSE event_type END,energy_cost,1 FROM events old
            WHERE COALESCE(schema_version,0)<3 AND actor_is_player=1 AND energy_cost>0
            AND NOT(event_type='building_placed' AND COALESCE(json_extract(payload_json,'$.building_id')=6001,0)
                AND json_extract(payload_json,'$.position_x') IS NOT NULL
                AND EXISTS(SELECT 1 FROM e0 till WHERE till.user_id=old.user_id AND till.session_id=old.session_id AND till.game_day=old.game_day
                    AND till.event_type='farming_till' AND ABS(till.sequence-old.sequence)<=1 AND till.energy_cost=old.energy_cost
                    AND json_extract(till.payload_json,'$.position_x')=json_extract(old.payload_json,'$.position_x')
                    AND json_extract(till.payload_json,'$.position_y')=json_extract(old.payload_json,'$.position_y')))
        ) GROUP BY action ORDER BY 体力 DESC,行为 LIMIT 5"""),
        (142,'钓鱼区域与鱼影',"SELECT json_extract(payload_json,'$.region') 区域,json_extract(payload_json,'$.water_kind') 水域类型,COUNT(*) 结算次数,SUM(json_extract(payload_json,'$.outcome')='success') 成功数,SUM(json_extract(payload_json,'$.outcome')='failed') 失败数,SUM(json_extract(payload_json,'$.outcome')='cancelled') 取消数 FROM events WHERE event_type='fishing_finished' GROUP BY 1,2"),
        (143,'猫工作技能点分配',"SELECT json_extract(payload_json,'$.work') 工作,COUNT(*) 投入点数,ROUND(100.0*COUNT(*)/SUM(COUNT(*)) OVER(),2) 比例 FROM events WHERE event_type='cat_skill_allocated' GROUP BY 1"),
        (144,'升级前未使用技能点',"SELECT AVG(json_extract(payload_json,'$.remaining_before_reward')) 平均剩余点数,100.0*AVG(json_extract(payload_json,'$.remaining_before_reward')>0) 有剩余比例,COUNT(*) 升级样本数 FROM events WHERE event_type='cat_level_up'"),
        (145,'打字前最近猫行为 Top 5',"SELECT COALESCE(json_extract(payload_json,'$.preceding.action'),'未观察到近期可见行为') 行为,COUNT(*) 开始输入次数 FROM events WHERE event_type='player_typing_started' GROUP BY 1 ORDER BY 开始输入次数 DESC,行为 LIMIT 5"),
    ]
    for pid,title,sql in behaviors:
        overview.append(q(pid,title,sql));base.append(q(pid,title,sql))
    overview += [q(150,'猫各工作等级分布',"SELECT sk.key 工作,sk.value 等级,COUNT(*) 猫数量,100.0*COUNT(*)/SUM(COUNT(*)) OVER(PARTITION BY sk.key) 比例 FROM players,json_each(state,'$.cats') c,json_each(c.value,'$.skills') sk GROUP BY 1,2"),
        q(151,'猫舍使用率与空位',"""SELECT SUM(used=1) 放过猫玩家数,SUM(used=0) 未观察到放猫玩家数,
            100.0*AVG(used) 观察期使用率,AVG(house_capacity-house_occupied) 每玩家平均空位,
            AVG(CASE WHEN house_count>0 THEN house_capacity-house_occupied END) 有猫舍玩家平均空位,COUNT(house_capacity) 有效快照数
            FROM (SELECT p.*,EXISTS(SELECT 1 FROM e0 WHERE e0.user_id=p.user_id AND event_type='cat_housing_changed' AND json_extract(payload_json,'$.location')='cat_house') OR house_occupied>0 used FROM players p WHERE house_capacity IS NOT NULL)""",
            '从首次有效采集起观察；未观察到不代表旧版本从未放过。当前每玩家有一个个人猫舍，升级扩容量。'),
        q(152,'各部位穿搭 Top 5',"""SELECT 部位,服装,玩家数 FROM (SELECT json_extract(c.value,'$.slot') 部位,json_extract(c.value,'$.item_name') 服装,COUNT(*) 玩家数,
            ROW_NUMBER() OVER(PARTITION BY json_extract(c.value,'$.slot_id') ORDER BY COUNT(*) DESC,json_extract(c.value,'$.item_id')) n
            FROM players,json_each(state,'$.outfits') c GROUP BY json_extract(c.value,'$.slot_id'),json_extract(c.value,'$.item_id')) WHERE n<=5 ORDER BY 部位,n"""),
        q(153,'采集覆盖与历史限制',"SELECT COUNT(*) 玩家数,COUNT(state) 完整状态玩家数,COUNT(play_days) 可去重游戏日玩家数,(SELECT COUNT(*) FROM e0 WHERE occurred_at IS NULL) 缺真实时间事件数 FROM players")]
    overview.append(q(154,'钓鱼尝试覆盖（含未观察到结算）',"SELECT json_extract(payload_json,'$.region') 区域,COUNT(*) 抛竿次数,SUM(EXISTS(SELECT 1 FROM e0 f WHERE f.user_id=events.user_id AND f.session_id=events.session_id AND f.event_type='fishing_finished' AND json_extract(f.payload_json,'$.operation_id')=json_extract(events.payload_json,'$.operation_id'))) 已观察到结算 FROM events WHERE event_type='fishing_started' GROUP BY 1"))
    detail_types="'cat_skill_allocated','cat_level_up','fishing_started','fishing_finished','cat_adopted','shop_purchase','shop_freeze_changed','cat_housing_changed','player_typing_started','player_typing_finished','theater_view','theater_choice','theater_exit','theater_line'"
    base.append(q(160,'行为细节（加点、升级、钓鱼、招募、商店、猫舍、输入、小剧场）',f"SELECT occurred_at 时间,event_type 类型,metadata_json metadata,payload_json payload FROM events WHERE event_type IN ({detail_types}) ORDER BY julianday(occurred_at) DESC,sequence DESC,event_id DESC"+limit))
    # Keep dedicated theater panels, their invitation denominators and sample threshold unchanged.
    theater = result['gameplay-theater.json']
    for dest in (overview,base):
        for p in theater['panels']:
            if p.get('type')=='text' or (dest is overview and p['id'] != 2):continue
            clone=copy.deepcopy(p);clone['id']=200+len(dest)
            for t in clone.get('targets',[]):
                for key in ('queryText','rawQueryText'):
                    t[key]=t[key].replace(time_range('occurred_at'),"('${behavior_period}'='all' OR ("+time_range('occurred_at')+"))")
            dest.append(clone)
    old_overview = result['gameplay-overview.json']
    # Untimed raw history remains reachable and retains its existing contract/panel id.
    overview += [copy.deepcopy(p) for p in old_overview['panels'] if p['id'] in (30,31)]
    result['gameplay-overview.json']=dashboard('gameplay-overview','总览',overview)
    sort="CASE '${sort_field}' WHEN 'play_seconds' THEN play_seconds WHEN 'play_days' THEN play_days WHEN 'island_level' THEN island_level WHEN 'first_login' THEN julianday(first_login) WHEN 'latest_login' THEN julianday(latest_login) WHEN 'money' THEN money END"
    listing=q(100,'玩家一览',f"SELECT {columns.split(',cat_count')[0]},COUNT(*) OVER() 总行数 FROM players ORDER BY CASE WHEN '${{sort_direction}}'='asc' THEN {sort} END ASC NULLS LAST,CASE WHEN '${{sort_direction}}'!='asc' THEN {sort} END DESC NULLS LAST,user_id"+limit)
    result['gameplay-players.json']=dashboard('gameplay-players','玩家一览',[listing])
    result['gameplay-player-detail.json']=dashboard('gameplay-player-detail','玩家 · 基础数据',base)
    day_sql="""SELECT archive_id 存档,game_day 游戏日,MAX(occurred_at) 最后记录时间,COUNT(*) 事件数,GROUP_CONCAT(DISTINCT event_type) 事件类型,
        (SELECT payload_json FROM e0 x WHERE x.user_id=events.user_id AND x.archive_id=events.archive_id AND x.game_day=events.game_day AND x.event_type='player_state_snapshot' ORDER BY julianday(x.occurred_at) DESC,sequence DESC LIMIT 1) 最新状态metadata
        FROM events WHERE NULLIF(archive_id,'') IS NOT NULL GROUP BY user_id,archive_id,game_day ORDER BY game_day DESC,archive_id"""+limit
    raw_days=facts+f"SELECT d.session_id,d.game_day,d.day_meta_json metadata,(SELECT GROUP_CONCAT(DISTINCT event_type) FROM e0 WHERE e0.user_id=d.user_id AND e0.session_id=d.session_id AND e0.game_day=d.game_day) 事件类型 FROM gameplay_days d WHERE d.user_id=${{user_id:sqlstring}} AND EXISTS(SELECT 1 FROM s WHERE s.user_id=d.user_id AND s.session_id=d.session_id) ORDER BY game_day DESC,imported_at DESC"+limit
    result['gameplay-player-days.json']=dashboard('gameplay-player-days','玩家 · 日期一览',[q(100,'游戏日一览（按存档去重）',day_sql),panel(101,'原始日 metadata（旧数据保留来源会话）',raw_days)])
    result['gameplay-player-events.json']=dashboard('gameplay-player-events','玩家 · 事件一览',[q(100,'全部事件 metadata / payload',"SELECT occurred_at 时间,archive_id 存档,game_day 游戏日,event_type 类型,event_id,session_id,metadata_json metadata,payload_json payload,COUNT(*) OVER() 总行数 FROM events WHERE (${event_type:sqlstring}='' OR event_type=${event_type:sqlstring}) ORDER BY julianday(occurred_at) DESC,sequence DESC,event_id DESC"+limit)])
    new_uids={'gameplay-overview','gameplay-players','gameplay-player-detail','gameplay-player-days','gameplay-player-events'}
    for board in result.values():
        if board['uid'] not in new_uids:continue
        board['templating']['list'] += [
            {'name':'behavior_period','label':'行为范围','type':'custom','query':'历史累计 : all,所选时间 : range','current':{'text':'历史累计','value':'all'}},
            {'name':'event_type','label':'事件类型（空=全部）','type':'textbox','current':{'text':'','value':''}},
            {'name':'sort_field','label':'排序字段','type':'custom','query':'游玩时长 : play_seconds,游戏日数 : play_days,岛屿等级 : island_level,首次登录 : first_login,最新登录 : latest_login,金币余额 : money','current':{'text':'最新登录','value':'latest_login'}},
            {'name':'sort_direction','label':'排序方向','type':'custom','query':'倒序 : desc,正序 : asc','current':{'text':'倒序','value':'desc'}},
        ]
        for v in theater['templating']['list']:
            if v['name'] in ('theater_event_id','min_invitations'):board['templating']['list'].append(copy.deepcopy(v))
        for v in board['templating']['list']:
            if v['name']=='page':v.update(type='textbox',label='页码（从0起，每页200条）');v.pop('query',None)
        navigation=[('gameplay-overview','总览'),('gameplay-players','玩家一览')]
        if board['uid'].startswith('gameplay-player-'):
            navigation += [('gameplay-player-detail','基础数据'),('gameplay-player-days','日期一览'),('gameplay-player-events','事件一览')]
            # No selected user must never disclose the whole population in detail panels.
            for p in board['panels']:
                for t in p.get('targets',[]):
                    for key in ('queryText','rawQueryText'):
                        if key in t:t[key]=t[key].replace(user,"(${user_id:sqlstring}<>'' AND "+user+")")
        board['links']=[{'title':title,'type':'link','url':'/d/'+uid,'includeVars':True,'keepTime':True} for uid,title in navigation]+[{'title':'其他运营报表','type':'link','url':'/d/gameplay-retention','includeVars':True,'keepTime':True}]
        board['panels'][0]['options']['content']='**累计指标按玩家去重；当前状态按最新快照，空值=未采集。** 游戏日按玩家+存档+游戏日去重，旧数据缺存档标识不猜测。金币为当前余额。行为默认历史累计，可切换期间；未知平台保留。新指标需新版客户端/服务器。体力使用实际扣除事件；旧版失败钓鱼无法补采。'
        for variable in board['templating']['list']:
            name=variable['name']
            if name in ('sort_field','sort_direction') and board['uid']!='gameplay-players':variable['hide']=2
            if name=='event_type' and board['uid']!='gameplay-player-events':variable['hide']=2
            if name in ('theater_event_id','min_invitations') and board['uid'] not in ('gameplay-overview','gameplay-player-detail'):variable['hide']=2
            if name=='user_id' and board['uid'].startswith('gameplay-player-'):variable['label']='玩家 ID（必选）'
            if name in ('user_id','session_id','llm_request_id') and board['uid'] in ('gameplay-overview','gameplay-players'):variable['hide']=2
        for p in board['panels']:
            for t in p.get('targets',[]):
                for key in ('queryText','rawQueryText'):
                    if key not in t:continue
                    sql=t[key]
                    if board['uid'] in ('gameplay-overview','gameplay-players'):
                        from theater_analytics import optional_filter
                        sql=sql.replace(user,'1').replace(optional_filter('user_id','user_id'),'1').replace(optional_filter('session_id','session_id'),'1')
                    for variable in ('user_id','session_id','event_type','page'):
                        token='${'+variable+':sqlstring}'
                        # Existing theater optional_filter already safely wraps these variables.
                        if 'json_array('+token+')' in sql:continue
                        default='0' if variable=='page' else ''
                        sql=sql.replace(token,"COALESCE(json_extract(json_array("+token+"),'$[0]'),'"+default+"')")
                    if board['uid'].startswith('gameplay-player-'):
                        selected="COALESCE(json_extract(json_array(${user_id:sqlstring}),'$[0]'),'')<>''"
                        sql="SELECT * FROM ("+sql+") WHERE "+selected
                    t[key]=sql
            p.setdefault('fieldConfig',{}).setdefault('defaults',{}).update(noValue='未采集',unit='none')
            for override in p['fieldConfig'].get('overrides',[]):
                for prop in override.get('properties',[]):
                    if prop['id']=='links':
                        for link in prop['value']:
                            link['title']='查看玩家详情'
                            if override['matcher']['options']=='user_id':link['url']=link['url'].replace('&var-user_id=${__value.raw}','')
            if board['uid'] in ('gameplay-players','gameplay-player-days','gameplay-player-events') and p['type']=='table':p['gridPos']['h']=18
            if board['uid']=='gameplay-players' and p['type']=='table':
                user_link=copy.deepcopy(p['fieldConfig']['overrides'][0])
                user_link['matcher']['options']='昵称'
                for link in user_link['properties'][0]['value']:
                    link['title']='查看玩家详情'
                    link['url']=link['url'].replace('&var-user_id=${__value.raw}','')
                p['fieldConfig']['overrides'].append(user_link)
                for column,width in [('昵称',130),('user_id',315),('首次登录',175),('最新登录',175)]:
                    p['fieldConfig']['overrides'].append({'matcher':{'id':'byName','options':column},'properties':[{'id':'custom.width','value':width}]})

        if board['uid']=='gameplay-overview':
            y=0;x=0;row_height=0
            for p in board['panels']:
                w=24 if p['type']=='text' or p['id'] in (100,105,153,30,31) else 12
                h=4 if p['type']=='text' else 6 if p['id']<120 else 9
                if x+w>24:y+=row_height;x=0;row_height=0
                p['gridPos']={'x':x,'y':y,'w':w,'h':h};x+=w;row_height=max(row_height,h)
                if x==24:y+=row_height;x=0;row_height=0
