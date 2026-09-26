"""Incremental canonical event projection; raw tables remain the audit trail."""
import json
from version_utils import release_version_from_client_version

COLUMNS = ('event_id','user_id','session_id','player_session_id','client_platform','client_version','release_version',
           'is_development_build','occurred_at','received_at','event_type','actor_id','actor_is_player','game_day',
           'sequence','payload_json','metadata_json','energy_cost','archive_id','schema_version','behavior_stat')


def ensure_facts(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS analytics_event_facts (
        event_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,session_id TEXT,player_session_id TEXT,
        client_platform TEXT,client_version TEXT,release_version TEXT,is_development_build INTEGER,
        occurred_at TEXT,received_at TEXT,event_type TEXT,actor_id TEXT,actor_is_player INTEGER,
        game_day INTEGER,sequence INTEGER,payload_json TEXT,metadata_json TEXT,energy_cost REAL,
        archive_id TEXT,schema_version INTEGER,behavior_stat INTEGER)''')
    from journey_analytics import ensure_journey_schema
    ensure_journey_schema(conn)
    from behavior_analytics import ensure_behavior_schema
    ensure_behavior_schema(conn)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_facts_archive_kind_time ON analytics_event_facts(archive_id,event_type,occurred_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_facts_session_kind ON analytics_event_facts(session_id,event_type)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_facts_user_kind_time ON analytics_event_facts(user_id,event_type,occurred_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_facts_kind_time ON analytics_event_facts(event_type,occurred_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_facts_archive_day ON analytics_event_facts(user_id,archive_id,game_day)')


def upsert_fact(conn, event, user, session, metadata, received, player_session=None, version=None, release=None):
    identity = event.get('event_id') or 'legacy:'+json.dumps([user,session,event.get('event_game_day'),event.get('sequence')],separators=(',',':'))
    actor = event.get('actor') or {}
    envelope = {k:v for k,v in event.items() if k != 'payload'}
    values = (identity,user,session,player_session,metadata.get('client_platform','unknown'),version,release,
              int(bool(metadata.get('is_development_build'))),event.get('event_real_time_iso'),received,event.get('event_type'),
              str(actor.get('agent_id','')),actor.get('is_player'),event.get('event_game_day'),event.get('sequence'),
              json.dumps(event.get('payload') or {},ensure_ascii=False),json.dumps(envelope,ensure_ascii=False),
              event.get('energy_cost'),event.get('archive_id'),event.get('schema_version'),event.get('behavior_stat'))
    assignments=','.join(
        "metadata_json=CASE WHEN json_extract(excluded.metadata_json,'$.archive_id') IS NULL AND json_extract(analytics_event_facts.metadata_json,'$.archive_id') IS NOT NULL THEN analytics_event_facts.metadata_json ELSE excluded.metadata_json END" if c=='metadata_json'
        else f'{c}=COALESCE(excluded.{c},analytics_event_facts.{c})' if c in ('archive_id','schema_version','behavior_stat','occurred_at')
        else f'{c}=excluded.{c}' for c in COLUMNS[1:])
    conn.execute(f"INSERT INTO analytics_event_facts ({','.join(COLUMNS)}) VALUES ({','.join('?' for _ in COLUMNS)}) ON CONFLICT(event_id) DO UPDATE SET {assignments}",values)
    from journey_analytics import mark_dirty
    mark_dirty(conn, event, user, session)
    from behavior_analytics import mark_behavior_dirty, upsert_behavior_action
    upsert_behavior_action(conn, identity, user, session, event)
    mark_behavior_dirty(conn, user, event.get('event_type', ''))
    if str(event.get('event_type', '')).startswith('journey_'):
        owner = conn.execute('SELECT user_id FROM journey_run_owners WHERE run_id=?', (session,)).fetchone()
        if owner and owner[0] != user:
            mark_behavior_dirty(conn, owner[0], event.get('event_type', ''))


def project_imported(conn, where='1', args=()):
    cursor=conn.execute('SELECT * FROM gameplay_events WHERE '+where,args)
    columns=[c[0] for c in cursor.description]
    for values in cursor.fetchall():
        row=dict(zip(columns,values));meta=json.loads(row.get('meta_json') or '{}')
        event={**meta,'event_type':row['event_type'],'event_real_time_iso':row['event_real_time_iso'],
               'event_game_day':row['game_day'],'sequence':meta.get('sequence') if meta.get('sequence') is not None else row['event_index'],
               'actor':{'agent_id':row['actor_id'],'is_player':row['actor_is_player']},
               'event_game_minutes':row['event_game_minutes'],'duration_minutes':row['duration_minutes'],'meowu_output':row['meowu_output'],
               'energy_cost':row['energy_cost'],'payload':json.loads(row['payload_json'] or '{}')}
        upsert_fact(conn,event,row['user_id'],row['session_id'],row,row['imported_at'],row['player_session_id'],row['client_version'],row['release_version'])


def migrate_facts(conn):
    ensure_facts(conn)
    conn.execute('CREATE TABLE IF NOT EXISTS analytics_migrations(name TEXT PRIMARY KEY)')
    if conn.execute("SELECT 1 FROM analytics_migrations WHERE name='event_facts_v1'").fetchone():
        normalize_live_release(conn)
        return
    # Bound the migration to the initial high-water marks. New batches already dual-write facts.
    # Read and write each page inside BEGIN IMMEDIATE: a long-lived SELECT cursor cannot
    # upgrade its old WAL snapshot after another connection commits (SQLITE_BUSY_SNAPSHOT).
    conn.commit()
    for table in ('gameplay_live_events','gameplay_events'):
        maximum=conn.execute('SELECT COALESCE(MAX(rowid),0) FROM '+table).fetchone()[0]
        after=0
        while after < maximum:
            conn.execute('BEGIN IMMEDIATE')
            ids=conn.execute('SELECT rowid FROM '+table+' WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT 1000',(after,maximum)).fetchall()
            if not ids:conn.commit();break
            end=ids[-1][0]
            if table=='gameplay_events':
                project_imported(conn,'rowid>? AND rowid<=?',(after,end))
            else:
                rows=conn.execute('SELECT user_id,session_id,event_json,received_at,player_session_id,client_platform,client_version,is_development_build FROM gameplay_live_events WHERE rowid>? AND rowid<=?',(after,end)).fetchall()
                for row in rows:
                    upsert_fact(conn,json.loads(row[2]),row[0],row[1],{'client_platform':row[5],'is_development_build':row[7]},row[3],row[4],row[6],release_version_from_client_version(row[6]))
            conn.commit();after=end
    conn.execute("INSERT INTO analytics_migrations VALUES ('event_facts_v1')")
    normalize_live_release(conn)


def normalize_live_release(conn):
    """Repair the initial backfill's live-only release keys once; preserve explicit releases."""
    if conn.execute("SELECT 1 FROM analytics_migrations WHERE name='fact_release_v1'").fetchone():return
    conn.create_function('normalize_fact_release',1,release_version_from_client_version,deterministic=True)
    conn.execute("UPDATE analytics_event_facts SET release_version=normalize_fact_release(client_version) WHERE release_version=client_version AND client_version LIKE 'unity-%'")
    conn.execute("INSERT INTO analytics_migrations VALUES ('fact_release_v1')")
