"""Offline synthetic performance acceptance. Never reads or writes the production database."""
import sys, json, time, sqlite3, statistics, math
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tests.test_behavior_analytics import BehaviorTests
from behavior_analytics import refresh_behavior,mark_behavior_dirty,METRICS
from analytics_facts import COLUMNS

case=BehaviorTests();case.setUp();db=case.db
case.emit(0,'journey_world_ready')
# Full permitted history, checkpoint-heavy with constant activity.
for n in range(1,20000):
 case.emit(n*30,'journey_quit' if n==19999 else 'journey_checkpoint',totals={'active':n*30,'fishing':n*30})
measure={}
def bench(name,fn):
 samples=[]
 for _ in range(5):
  t=time.perf_counter();fn();samples.append((time.perf_counter()-t)*1000)
 measure[name]={'runs':len(samples),'p95_ms':sorted(samples)[math.ceil(.95*len(samples))-1],'max_ms':max(samples)}
def replay():
 mark_behavior_dirty(db,'u');db.commit();refresh_behavior(db,limit=1)
bench('20000_point_replay',replay)
# Irrelevant automatic traffic must use the event-kind index, not be JSON-decoded.
prototype=list(db.execute('SELECT '+','.join(COLUMNS)+' FROM analytics_event_facts LIMIT 1').fetchone())
rows=[]
for n in range(100000):
 r=prototype.copy();r[0]='automatic-'+str(n);r[1]='auto';r[COLUMNS.index('event_type')]='cat_agent_response';rows.append(r)
db.executemany('INSERT INTO analytics_event_facts VALUES ('+','.join('?' for _ in COLUMNS)+')',rows)
def rejected():
 mark_behavior_dirty(db,'auto');db.commit();refresh_behavior(db,limit=1)
bench('100000_automatic_events_excluded',rejected)
# 1000 player cohort projection with the large history still present.
cur=db.execute("SELECT * FROM behavior_players WHERE user_id='u'");cols=[x[0] for x in cur.description];template=list(cur.fetchone())
rows=[]
for n in range(999):
 r=template.copy();r[0]='cohort-'+str(n);rows.append(r)
db.executemany('INSERT INTO behavior_players VALUES ('+','.join('?' for _ in cols)+')',rows)
db.commit()
bench('1000_player_cohort_query',lambda:case.query('gameplay-behavior-cohorts',1))
bench('selected_player_paged_intervals',lambda:case.query('gameplay-behavior-timeline',2,**{'user_id:sqlstring':"'u'"}))
bench('repeat_clean_refresh',lambda:refresh_behavior(db))
Path('/tmp/paw-behavior-performance.json').write_text(json.dumps({'measurements':measure,'scope':'Local SQLite, 20000 relevant points + 100000 automatic points + 1000 cohort players; 5 samples, nearest-rank P95 equals max. Not production network or browser performance.'},indent=2))
print(json.dumps(measure,indent=2))
assert measure['20000_point_replay']['max_ms']<2000
assert measure['1000_player_cohort_query']['max_ms']<1000
