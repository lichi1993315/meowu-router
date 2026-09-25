"""Journey projection. Raw events remain immutable evidence; only dirty players rebuild.

A refresh handles <= 16 players, <= 100000 points/player. Oversize histories are
explicitly withheld, never truncated into plausible durations. No per-request history
scan: intake only marks one indexed dirty key. The importer owns projection work.
"""
import json
import math
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from telemetry_time import parse_timestamp

FLOW_VERSION = 'journey-v1'
BACKFILL_MIGRATION = 'journey_projection_v1'
METRICS = ('effective', 'foreground', 'waiting', 'single', 'multiplayer_alone', 'multiplayer_together', 'unknown_mode')
CATALOG = json.loads(Path(__file__).with_name('journey_catalog.json').read_text())
TASKS = {str(n['task_id']): n for n in CATALOG if 'task_id' in n}
RANK = {n['node_id']: n['sort_order'] for n in CATALOG}
LABELS = {n['node_id']: n['title'] for n in CATALOG}


def ensure_journey_schema(conn):
    schema = '''
    CREATE TABLE IF NOT EXISTS journey_run_owners(run_id TEXT PRIMARY KEY,user_id TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_journey_owner ON journey_run_owners(user_id,run_id);
    CREATE TABLE IF NOT EXISTS journey_dirty(user_id TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS journey_projection_status(user_id TEXT PRIMARY KEY,status TEXT,point_count INTEGER,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS journey_players(
      user_id TEXT PRIMARY KEY,first_at TEXT,last_at TEXT,client_platform TEXT,release_version TEXT,
      is_development_build INTEGER,session_id TEXT,flow_version TEXT,cohort_kind TEXT,
      effective REAL,foreground REAL,waiting REAL,single REAL,multiplayer_alone REAL,multiplayer_together REAL,unknown_mode REAL,
      mode_group TEXT,quality TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS journey_nodes(
      user_id TEXT,archive_id TEXT,flow_version TEXT,node_id TEXT,title TEXT,sort_order INTEGER,
      started_at TEXT,completed_at TEXT,claimed_at TEXT,last_at TEXT,status TEXT,quality TEXT,
      effective REAL,foreground REAL,waiting REAL,single REAL,multiplayer_alone REAL,multiplayer_together REAL,unknown_mode REAL,
      cumulative_effective REAL,world_effective REAL,natural_seconds REAL,progress_value INTEGER,target_value INTEGER,
      PRIMARY KEY(user_id,archive_id,flow_version,node_id));
    CREATE INDEX IF NOT EXISTS idx_journey_nodes_start ON journey_nodes(started_at,node_id);
    CREATE TABLE IF NOT EXISTS journey_exits(
      event_id TEXT PRIMARY KEY,user_id TEXT,run_id TEXT,archive_id TEXT,node_id TEXT,page_id TEXT,
      occurred_at TEXT,reason TEXT,effective REAL,node_effective REAL,mode TEXT,quality TEXT,is_final INTEGER,island_level INTEGER);
    CREATE INDEX IF NOT EXISTS idx_journey_exit_user ON journey_exits(user_id,occurred_at);
    CREATE TABLE IF NOT EXISTS journey_intervals(
      event_id TEXT PRIMARY KEY,user_id TEXT,run_id TEXT,archive_id TEXT,start_at TEXT,end_at TEXT,mode TEXT,humans INTEGER,
      effective REAL,foreground REAL,waiting REAL,single REAL,multiplayer_alone REAL,multiplayer_together REAL,unknown_mode REAL,quality TEXT,node_id TEXT,page_id TEXT);
    CREATE INDEX IF NOT EXISTS idx_journey_intervals_user ON journey_intervals(user_id,start_at);
    CREATE TABLE IF NOT EXISTS journey_catalog(flow_version TEXT,node_id TEXT,title TEXT,sort_order INTEGER,prerequisite TEXT,
      PRIMARY KEY(flow_version,node_id));
    '''
    for statement in schema.split(';'):
        if statement.strip(): conn.execute(statement)
    if 'island_level' not in {r[1] for r in conn.execute('PRAGMA table_info(journey_exits)')}:
        conn.execute('ALTER TABLE journey_exits ADD COLUMN island_level INTEGER')
    columns={r[1] for r in conn.execute('PRAGMA table_info(journey_intervals)')}
    for name in ('node_id','page_id'):
        if name not in columns: conn.execute('ALTER TABLE journey_intervals ADD COLUMN '+name+' TEXT')
    conn.executemany('INSERT OR IGNORE INTO journey_catalog VALUES (?,?,?,?,?)',
                     [(FLOW_VERSION,n['node_id'],n['title'],n['sort_order'],n.get('prerequisite','')) for n in CATALOG])


def mark_dirty(conn, event, user, run):
    if not str(event.get('event_type','')).startswith('journey_'):
        return
    old = conn.execute('SELECT user_id FROM journey_run_owners WHERE run_id=?',(run,)).fetchone()
    # A run can link its own pre-login events; never map an entire shared installation.
    owner = user if not user.startswith('anon:') or not old else old[0]
    conn.execute('INSERT INTO journey_run_owners VALUES (?,?) ON CONFLICT(run_id) DO UPDATE SET user_id=excluded.user_id',(run,owner))
    conn.execute('INSERT OR IGNORE INTO journey_dirty VALUES (?)',(owner,))
    if old and old[0] != owner:
        conn.execute('INSERT OR IGNORE INTO journey_dirty VALUES (?)',(old[0],))


def backfill_existing_journeys(conn):
    """Queue historical journey facts once; projections remain importer-owned."""
    ensure_journey_schema(conn)
    conn.execute('CREATE TABLE IF NOT EXISTS analytics_migrations(name TEXT PRIMARY KEY)')
    if conn.execute('SELECT 1 FROM analytics_migrations WHERE name=?',(BACKFILL_MIGRATION,)).fetchone():
        return 0

    owners={}
    for user,run in conn.execute("""SELECT user_id,session_id FROM analytics_event_facts
            WHERE event_type GLOB 'journey_*' AND COALESCE(session_id,'')!='' ORDER BY rowid"""):
        user=str(user or '')
        if not user: continue
        current=owners.get(run)
        if current is None or not user.startswith('anon:'):
            owners[run]=user

    for run,owner in owners.items():
        old=conn.execute('SELECT user_id FROM journey_run_owners WHERE run_id=?',(run,)).fetchone()
        if old and old[0] != owner:
            conn.execute('INSERT OR IGNORE INTO journey_dirty VALUES (?)',(old[0],))
        conn.execute('INSERT INTO journey_run_owners VALUES (?,?) ON CONFLICT(run_id) DO UPDATE SET user_id=excluded.user_id',(run,owner))
        conn.execute('INSERT OR IGNORE INTO journey_dirty VALUES (?)',(owner,))
    conn.execute('INSERT INTO analytics_migrations(name) VALUES (?)',(BACKFILL_MIGRATION,))
    return len(owners)


def stamp(value):
    try:
        date = parse_timestamp(value)
        return date.timestamp() if date.tzinfo else None
    except (ValueError, AttributeError, TypeError):
        return None


def iso(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat() if value is not None else None


def number(value):
    return float(value) if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value >= 0 else None


def project(points):
    """Pure reducer used by replay tests. Durations follow observed client counters.

    Points at boundaries make within-run allocation exact. Gaps and concurrent clients
    are labelled incomplete instead of inventing continuous play or double counting.
    """
    points = sorted(points,key=lambda e:(e['t'],e['session_id'],e.get('sequence') or 0,e['event_id']))
    intervals=[]; nodes={}; exits=[]; previous={}; world={}; creation=set(); totals={m:0.0 for m in METRICS}
    quality='observed'; last_covered=-math.inf; last_exit_by_run={}; seen_archives=set(); left_runs=set(); levels={}
    def node(archive,version,nid,title=None):
        key=(archive,version,nid)
        if key not in nodes:
            nodes[key]={'archive_id':archive,'flow_version':version,'node_id':nid,'title':title or LABELS.get(nid,nid),
                'sort_order':RANK.get(nid,999),'start':None,'end':None,'claim':None,'last':None,'status':'in_progress',
                'quality':'observed','progress_value':None,'target_value':None}
        return nodes[key]
    def position(archive,version,p):
        if p.get('current_node') in ('player_create','cat_create','startup','loading','menu'):
            return p['current_node']
        active=[n for n in nodes.values() if n['archive_id']==archive and n['flow_version']==version
                and n['node_id'].startswith('task:') and n['node_id'].count(':')==1 and n['claim'] is None and n['status']!='baseline_claimed']
        if active:
            first=min(active,key=lambda n:n['sort_order'])
            return first['node_id']+(':claim' if first['end'] is not None or first['status']=='baseline_completed' else '')
        if any(n['node_id']=='task:3810' and (n['claim'] is not None or n['status']=='baseline_claimed') for n in nodes.values()):
            return 'mainline_complete'
        return 'world'
    for e in points:
        p=e['p']; run=e['session_id']; t=e['t']; archive=e.get('archive_id') or ''; v=p.get('flow_version',FLOW_VERSION)
        prev=previous.get(run)
        if prev:
            dt=t-prev['t']; diffs={m:None for m in METRICS}
            for m in METRICS:
                a=number(prev['p'].get(m+'_seconds')); b=number(p.get(m+'_seconds'))
                if a is not None and b is not None and b >= a: diffs[m]=b-a
            valid=(0 <= dt <= 180 and all(x is not None for x in diffs.values())
                   and diffs['effective'] <= diffs['foreground']+0.1 and diffs['foreground'] <= dt+1
                   and abs(sum(diffs[m] for m in ('single','multiplayer_alone','multiplayer_together','unknown_mode'))-diffs['effective'])<0.1)
            concurrent=dt>0 and prev['t'] < last_covered-0.001 and diffs.get('foreground',0) not in (0,None)
            q='observed' if valid and not concurrent else 'concurrent' if concurrent else 'gap'
            if q!='observed': quality='incomplete'
            iv={'event_id':e['event_id'],'run_id':run,'archive_id':prev.get('archive_id') or '',
                'start':prev['t'],'end':t,'mode':prev['p'].get('mode','unknown'),'humans':prev['p'].get('humans',0),'quality':q,'node_id':prev['p'].get('current_node'),'page_id':prev['p'].get('current_page',''),
                **{m:diffs[m] if q=='observed' else None for m in METRICS}}
            intervals.append(iv)
            if q=='observed':
                for m in METRICS: totals[m]+=diffs[m]
            if valid and diffs['foreground']>0: last_covered=max(last_covered,t)
        previous[run]=e
        kind=e['event_type']
        if kind=='journey_context':
            if 'island_level' in p: levels[archive]=p['island_level']
            for task in p.get('tasks',[]):
                tid=str(task.get('task_id')); nid='task:'+tid
                if tid not in TASKS: continue
                n=node(archive,v,nid,task.get('title')); n['last']=t
                n['progress_value']=task.get('value'); n['target_value']=task.get('target_value')
                if n['start'] is None and n['end'] is None:
                    n['status']={0:'baseline_open',1:'baseline_completed',2:'baseline_claimed'}.get(task.get('state'),'baseline_open')
                    n['quality']='missing_start'
            level=p.get('island_level',1) if 'tasks' in p else 1
            for level_id in range(2,min(8,level)+1):
                n=node(archive,v,'level:'+str(level_id))
                if n['end'] is None:
                    if n['last'] is None:
                        n['status']='observed_on_return' if archive in seen_archives else 'joined_existing'
                        n['last']=t;n['quality']='missing_start'
            if 'tasks' in p: seen_archives.add(archive)
        if kind=='journey_onboarding_eligible': creation.add(archive)
        if kind=='journey_node_started':
            nid=p.get('node_id')
            if nid not in ('startup','loading','player_create','cat_create'): continue
            if nid=='player_create': creation.add(archive)
            n=node(p.get('node_archive_id',archive),v,nid); n['start']=n['start'] if n['start'] is not None else t; n['last']=t
        if kind=='journey_node_ended':
            nid=p.get('node_id'); n=node(p.get('node_archive_id',archive),v,nid); n['last']=t
            if p.get('status')=='completed': n['end']=n['end'] if n['end'] is not None else t;n['status']='completed'
            elif p.get('status')=='skipped': n['status']='skipped';n['quality']='skipped'
        if kind=='journey_world_ready':
            world.setdefault(archive,t)
            if archive in creation:
                for n in nodes.values():
                    if n['archive_id']==archive and n['status']=='baseline_open':
                        n['start']=t;n['status']='in_progress';n['quality']='observed'
            current_level=p.get('island_level',1)
            if current_level<8:
                n=node(archive,v,'level:'+str(current_level+1)); n['start']=n['start'] if n['start'] is not None else t
        if kind=='journey_business':
            b=p.get('business') or {}; business=p.get('business_type','')
            if business.startswith('task_'):
                tid=str(b.get('task_id'))
                if tid not in TASKS: continue
                n=node(archive,v,'task:'+tid,b.get('task_title'));n['last']=t
                n['progress_value']=b.get('value');n['target_value']=b.get('target_value')
                if business=='task_started' and n['start'] is None and n['status'] not in ('baseline_completed','baseline_claimed'):
                    n['start']=t;n['status']='in_progress';n['quality']='observed'
                if business=='task_completed' and n['end'] is None:
                    n['end']=t;n['status']='completed'
                    claim=node(archive,v,'task:'+tid+':claim',n['title']+' · 领奖');claim['start']=t
                if business=='task_claimed' and n['claim'] is None:
                    n['claim']=t
                    claim=node(archive,v,'task:'+tid+':claim',n['title']+' · 领奖');claim['end']=t;claim['status']='completed'
                    if n['end'] is None: n['quality']='missing_completion'
            elif business=='island_level_up':
                level=b.get('after_level',0)
                if 2<=level<=8:
                    levels[archive]=level
                    n=node(archive,v,'level:'+str(level));n['end']=n['end'] if n['end'] is not None else t;n['last']=t;n['status']='completed'
                    if level<8:
                        nxt=node(archive,v,'level:'+str(level+1));nxt['start']=nxt['start'] if nxt['start'] is not None else t
        # The final point of every run is retained; explicit leaves are additional session exits.
        exit_row={'event_id':e['event_id'],'run_id':run,'archive_id':archive,'node_id':position(archive,v,p),
                  'page_id':p.get('current_page',''),'occurred_at':t,'reason':kind if kind in ('journey_leave','journey_quit','journey_background') else 'last_checkpoint',
                  'effective':totals['effective'],'mode':p.get('mode','unknown'),'quality':quality,'is_final':0,'island_level':levels.get(archive)}
        if kind=='journey_context' or p.get('current_node')=='loading': left_runs.discard(run)
        if run in left_runs and p.get('current_node')=='menu': continue
        if (p.get('app_state','foreground')=='foreground' and not p.get('afk',False)) or kind in ('journey_leave','journey_quit','journey_background'):
            last_exit_by_run[run]=exit_row
        if kind=='journey_leave': exits.append(exit_row);left_runs.add(run)
    for last in last_exit_by_run.values():
        if not any(x['event_id']==last['event_id'] for x in exits): exits.append(last)
    if exits: max(exits,key=lambda x:x['occurred_at'])['is_final']=1

    def duration(start,end,archive=None):
        result={m:0.0 for m in METRICS}; complete=True
        if start is None: return {m:None for m in METRICS},False
        for iv in intervals:
            if archive is not None and iv['archive_id']!=archive: continue
            overlap=max(0,min(end,iv['end'])-max(start,iv['start']))
            if overlap<=0: continue
            if iv['quality']!='observed': complete=False;continue
            ratio=overlap/(iv['end']-iv['start'])
            for m in METRICS: result[m]+=iv[m]*ratio
        return result,complete
    last=points[-1]['t'] if points else None
    for n in nodes.values():
        end=n['end'] if n['end'] is not None else last
        values,complete=duration(n['start'],end,None if n['node_id'] in ('startup','loading') else n['archive_id']);n.update(values)
        n['natural_seconds']=end-n['start'] if n['start'] is not None else None
        n['cumulative_effective']=duration(points[0]['t'],end)[0]['effective']
        n['world_effective']=duration(world.get(n['archive_id']),end,n['archive_id'])[0]['effective']
        if not complete:
            n['quality']='missing_start' if n['start'] is None else 'incomplete'
            for m in METRICS: n[m]=None
    for x in exits:
        n=next((n for n in nodes.values() if n['archive_id']==x['archive_id'] and n['node_id']==x['node_id'].removesuffix(':claim')),None)
        x['node_effective']=duration(n['start'],x['occurred_at'],x['archive_id'])[0]['effective'] if n else None
    return nodes,intervals,exits,totals,quality,creation


def refresh_journeys(conn, limit=16, max_points=100000):
    """Called by importer, not by ingestion. Idempotent and bounded per refresh."""
    users=[r[0] for r in conn.execute('SELECT user_id FROM journey_dirty ORDER BY rowid LIMIT ?',(limit,))]
    started=time.monotonic(); processed=0
    for user in users:
        if processed and time.monotonic()-started >= 2: break
        processed += 1
        rows=conn.execute('''SELECT f.* FROM journey_run_owners o JOIN analytics_event_facts f ON f.session_id=o.run_id
            WHERE o.user_id=? AND f.event_type GLOB 'journey_*' ORDER BY f.occurred_at,f.sequence LIMIT ?''',(user,max_points+1))
        cols=[c[0] for c in rows.description]; events=[dict(zip(cols,r)) for r in rows]
        if len(events)>max_points:
            for table in ('journey_players','journey_nodes','journey_exits','journey_intervals'):
                conn.execute('DELETE FROM '+table+' WHERE user_id=?',(user,))
            conn.execute('INSERT OR REPLACE INTO journey_projection_status VALUES (?,?,?,?)',(user,'history_limit',len(events),iso(datetime.now(timezone.utc).timestamp())))
            conn.execute('DELETE FROM journey_dirty WHERE user_id=?',(user,));continue
        points=[]
        for e in events:
            e['t']=stamp(e['occurred_at']);e['p']=json.loads(e['payload_json'])
            if e['t'] is not None: points.append(e)
        for table in ('journey_players','journey_nodes','journey_exits','journey_intervals'):
            conn.execute('DELETE FROM '+table+' WHERE user_id=?',(user,))
        if points:
            nodes,intervals,exits,totals,quality,creation=project(points)
            points.sort(key=lambda e:e['t']); first=points[0];last=points[-1]
            single=totals['single']>0;multi=totals['multiplayer_alone']+totals['multiplayer_together']>0
            group='mixed' if single and multi else 'single' if single else 'multiplayer' if multi else 'unknown'
            last_activity=max((x['occurred_at'] for x in exits),default=first['t'])
            server_session=next((e['p']['server_session_id'] for e in points if e['p'].get('server_session_id')),first['session_id'])
            values=(user,first['occurred_at'],iso(last_activity),first['client_platform'],first['release_version'],
                    first['is_development_build'],server_session,first['p'].get('flow_version',FLOW_VERSION),
                    'new' if creation else 'existing' if any(n['status'].startswith('baseline') for n in nodes.values()) else 'unclassified',
                    *(totals[m] for m in METRICS),group,quality,iso(datetime.now(timezone.utc).timestamp()))
            conn.execute('INSERT INTO journey_players VALUES ('+','.join('?' for _ in values)+')',values)
            for n in nodes.values():
                values=(user,n['archive_id'],n['flow_version'],n['node_id'],n['title'],n['sort_order'],iso(n['start']),iso(n['end']),iso(n['claim']),iso(n['last']),n['status'],n['quality'],
                        *(n[m] for m in METRICS),n['cumulative_effective'],n['world_effective'],n['natural_seconds'],n['progress_value'],n['target_value'])
                conn.execute('INSERT INTO journey_nodes VALUES ('+','.join('?' for _ in values)+')',values)
            for iv in intervals:
                values=(iv['event_id'],user,iv['run_id'],iv['archive_id'],iso(iv['start']),iso(iv['end']),iv['mode'],iv['humans'],*(iv[m] for m in METRICS),iv['quality'],iv['node_id'],iv['page_id'])
                conn.execute('INSERT INTO journey_intervals VALUES ('+','.join('?' for _ in values)+')',values)
            for x in exits:
                values=(x['event_id'],user,x['run_id'],x['archive_id'],x['node_id'],x['page_id'],iso(x['occurred_at']),x['reason'],x['effective'],x['node_effective'],x['mode'],x['quality'],x['is_final'],x['island_level'])
                conn.execute('INSERT OR IGNORE INTO journey_exits VALUES ('+','.join('?' for _ in values)+')',values)
        conn.execute('INSERT OR REPLACE INTO journey_projection_status VALUES (?,?,?,?)',(user,'ready',len(events),iso(datetime.now(timezone.utc).timestamp())))
        conn.execute('DELETE FROM journey_dirty WHERE user_id=?',(user,))
    return processed
