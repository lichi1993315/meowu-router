"""Channel identity stays explicit, session scoped and independent of OS/version."""
import unittest
from tests import test_player_analytics as helpers
from generate_analytics_dashboards import build
from telemetry_events import accept_batch
from telemetry_platform import client_metadata, record_session_channel
from playtime_store import record_play_session_event
from import_gameplay_telemetry import import_sample


class DistributionChannelTests(unittest.TestCase):
    setUp = helpers.PlayerAnalyticsTests.setUp
    tearDown = helpers.PlayerAnalyticsTests.tearDown
    emit = helpers.PlayerAnalyticsTests.emit
    query = helpers.PlayerAnalyticsTests.query

    def test_dashboard_defaults_to_mid_autumn_and_preserves_version_filter(self):
        for dashboard in build().values():
            variables={v['name']:v for v in dashboard['templating']['list']}
            self.assertEqual(variables['playtest_id']['current']['value'],'中秋playtest')
            self.assertIn('distribution_channel',variables)
            self.assertIn('release_version',variables)

    def test_no_channel_inference_from_historical_steam_or_taptap_versions(self):
        for version in ('unity-steam-0.1','unity-taptap-0.1'):
            self.assertEqual(client_metadata({'client_version':version,'client_platform':'windows'})['distribution_channel'],'unknown')
        self.assertEqual(client_metadata({}, {'X-Distribution-Channel':'steam'})['distribution_channel'],'steam')
        self.assertEqual(client_metadata({'gameplay_telemetry':{'session_meta':{'distribution_channel':'taptap'}}})['distribution_channel'],'taptap')
        self.assertEqual(client_metadata({'distribution_channel':'arbitrary'})['distribution_channel'],'unknown')

    def test_player_events_filter_by_explicit_channel_and_missing_does_not_erase(self):
        for i,channel in enumerate(('internal','steam','taptap','web')):
            self.emit('same-'+channel,'purchase',{'count':i+1})
            record_session_channel(self.db,'same-'+channel,'s-same-'+channel,{'distribution_channel':channel})
            record_session_channel(self.db,'same-'+channel,'s-same-'+channel,{'distribution_channel':'unknown'})
            self.db.commit()
        self.emit('legacy','purchase',{})
        self.db.commit()
        for channel in ('internal','steam','taptap','web'):
            result=self.query('gameplay-player-events',100,'same-'+channel,**{'distribution_channel:sqlstring':"'"+channel+"'"})
            self.assertEqual(len(result),1)
            self.assertEqual(self.query('gameplay-player-events',100,'legacy',**{'distribution_channel:sqlstring':"'"+channel+"'"}),[])
        self.assertEqual(len(self.query('gameplay-player-events',100,'legacy',**{'distribution_channel:sqlstring':"'unknown'"})),1)

    def test_all_ingestion_paths_record_channel(self):
        record_play_session_event(self.db,payload={'user_id':'same','session_id':'login-steam','distribution_channel':'steam'},headers={},event_type='login',received_at='2026-09-23T10:00:00Z')
        import_sample(self.db,source_file='channel-fixture',sample={'user_id':'same','gameplay_telemetry':{'session_meta':{'session_id':'snapshot-taptap','distribution_channel':'taptap'},'days':{}}},imported_at='2026-09-23T10:00:00Z')
        self.db.commit()
        event={'event_id':'channel-live','event_real_time_iso':'2026-09-23T10:00:00Z','event_type':'purchase','payload':{}}
        accept_batch(self.path,{'user_id':'same','session_id':'live-web','distribution_channel':'web','events':[event]}, {})
        rows=self.db.execute('SELECT session_id,distribution_channel FROM analytics_session_channels WHERE user_id=?',('same',)).fetchall()
        self.assertEqual(dict(rows),{'login-steam':'steam','snapshot-taptap':'taptap','live-web':'web'})
