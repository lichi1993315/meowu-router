-- Explicit one-off correction: 2026-09-24 00:00 Asia/Shanghai.
-- Only cohort metadata changes; raw events and historical sessions are retained.
BEGIN IMMEDIATE;
UPDATE analytics_playtests SET started_at='2026-09-23T16:00:00Z'
WHERE playtest_id='中秋playtest';
DELETE FROM analytics_session_playtests AS m
WHERE m.playtest_id='中秋playtest' AND NOT EXISTS (
    SELECT 1 FROM play_session_events e
    JOIN analytics_playtests p ON p.playtest_id=m.playtest_id
    WHERE e.user_id=m.user_id AND e.session_id=m.session_id
      AND e.id=(SELECT MIN(first.id) FROM play_session_events first
                WHERE first.user_id=m.user_id AND first.session_id=m.session_id)
      AND e.event_type='login'
      AND e.client_platform IN ('webgl','windows','editor','other')
      AND julianday(e.client_sent_at)>=julianday(p.started_at)
      AND julianday(e.received_at)>=julianday(p.started_at)
);
COMMIT;
