import csv
import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import scout

AT = datetime(2026,10,17,12,tzinfo=timezone.utc)

def row(**changes):
    base={'videoId':'abcdefghijk','videoTitle':'История', 'channelId':'UC_test', 'channelTitle':'Канал',
          'videoPublishedAt':(AT-timedelta(days=5)).isoformat(),'viewCount':150000,
          'subscriberCount':2000,'videoDuration':3600,'vph':None}
    base.update(changes)
    return base

class DataTests(unittest.TestCase):
    def test_trending_null_vph_stays_unknown(self):
        v=scout.normalize({'videos':[row()]},'pl')[0]
        self.assertIsNone(v.vph)
        self.assertEqual(v.duration,3600)

    def test_search_iso_duration(self):
        v=scout.normalize({'results':[{'id':'abcdefghijk','kind':'video','title':'Test','duration':'PT1H2M3S',
                'publishedAt':'2026-10-10T00:00:00Z','viewCount':1234}]})[0]
        self.assertEqual(v.duration,3723)
        self.assertIsNone(v.subscribers)

    def test_millisecond_timestamp(self):
        v=scout.normalize({'videos':[row(videoPublishedAt=int(AT.timestamp()*1000))]})[0]
        self.assertEqual(scout.stamp(v.published),AT)

    def test_filter_excludes_old_future_unknown_and_short(self):
        rows=[row(),row(videoId='bcdefghijkl',subscriberCount=None),row(videoId='cdefghijklm',videoDuration=None),
              row(videoId='defghijklmn',videoPublishedAt=(AT-timedelta(days=22)).isoformat()),
              row(videoId='efghijklmno',videoPublishedAt=(AT+timedelta(days=1)).isoformat()),
              row(videoId='fghijklmnop',viewCount=None),row(videoId='ghijklmnopq',videoDuration=600)]
        result=scout.select(scout.normalize({'videos':rows}),21,100000,50000,20,AT)
        self.assertEqual([v.video_id for v in result],['abcdefghijk'])

    def test_zero_subscribers_valid(self):
        videos=scout.normalize({'videos':[row(subscriberCount=0)]})
        self.assertEqual(len(scout.select(videos,21,100000,50000,20,AT)),1)

    def test_deduplicate_newest_observation(self):
        a=scout.normalize({'videos':[row(viewCount=200000)]},observed_at='2026-10-17T00:00:00Z')
        b=scout.normalize({'videos':[row(viewCount=100000)]},observed_at='2026-10-16T00:00:00Z')
        self.assertEqual(scout.unique(a+b)[0].views,200000)

    def test_persisted_roundtrip(self):
        rows=scout.normalize({'videos':[row()]},'pl','topic',observed_at=AT.isoformat())
        exported={'videos':[scout.asdict(v) for v in rows]}
        self.assertEqual(scout.normalize(exported),rows)

    def test_import_mcp_wrappers(self):
        raw={'content':[{'type':'text','text':json.dumps({'videos':[row()]})}]}
        self.assertEqual(len(scout.normalize(raw)),1)
        self.assertEqual(len(scout.normalize({'structuredContent':{'videos':[row()]}})),1)

    def test_error_not_empty_success(self):
        with self.assertRaises(RuntimeError): scout.unwrap({'isError':True})
        with self.assertRaises(ValueError): scout.normalize({'something':[]})

    def test_nonvideo_excluded(self):
        self.assertEqual(scout.normalize({'results':[{'kind':'channel','id':'abcdefghijk'}]}),[])

    def test_invalid_and_nonfinite_numbers(self):
        for value in [None,'NaN','Infinity',-1,True,'bad']:
            self.assertIsNone(scout.number(value))

    def test_romanian_fallback(self):
        tasks=scout.plan('ro',['povești de viață'],21,100000,50000,at=AT)
        name,args,topic,evidence=tasks[0]
        self.assertEqual(name,'vidiq_youtube_search')
        self.assertEqual(args['query'],'povești de viață')
        self.assertNotIn('language',args)
        self.assertIn('не подтверждён',evidence)
        with self.assertRaises(ValueError): scout.plan('ro',[],21,100000,50000)

    def test_polish_topic_preserved(self):
        task=scout.plan('pl',['historie'],21,100000,50000,at=AT)[0]
        self.assertEqual(task[1]['titleQuery'],'historie')
        self.assertEqual(task[1]['videoTitleLanguage'],'pl')

    def test_outlier_window_expands_then_local_filter(self):
        self.assertEqual(scout.plan('es',[],100,0,10000,'outliers',AT)[0][1]['publishedWithin'],'sixMonths')

    def test_unique_channel_count_not_total_market(self):
        text=scout.summarize(scout.normalize({'videos':[row(),row(videoId='bcdefghijkl')]}))
        self.assertIn('1 известных каналов',text)
        self.assertIn('1 каналов с ≥2',text)

    def test_audit_uses_only_recent_known_views(self):
        report,_=scout.audit_text('UC_test',{}, {'videos':[row(),row(videoId='bcdefghijkl',viewCount=120000),
                row(videoId='cdefghijklm',videoPublishedAt='2025-01-01T00:00:00Z',viewCount=900000)]},21,100000,AT)
        self.assertIn('≥100,000: 2',report)
        self.assertIn('135,000',report)
        self.assertIn('Регистрация: неизвестна',report)
        self.assertIn('несколько успешных',report)

    def test_revenue_deducts_cost(self):
        self.assertEqual(scout.revenue(100000,3,120),180)
        self.assertEqual(scout.revenue(100000,0,120),-120)
        self.assertEqual(scout.signed_number(-100),-100)

    def test_export_csv_formula_escaping_and_unicode(self):
        rows=scout.normalize({'videos':[row(videoTitle='=1+1')]})
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'result.csv'
            scout.export_csv(rows,path)
            with path.open(encoding='utf-8-sig') as f:
                data=list(csv.reader(f))
            self.assertEqual(data[1][0],"'=1+1")
            self.assertEqual(data[1][1],'Канал')

class TransportTests(unittest.TestCase):
    def client(self,budget=5):
        c=scout.MCP('test-key',budget)
        c.tools={'vidiq_youtube_search':{'inputSchema':{'properties':{'query':{}},'required':['query']}}}
        return c

    def test_json_and_sse(self):
        result={'jsonrpc':'2.0','id':7,'result':{'ok':True}}
        self.assertEqual(scout.decode_rpc(json.dumps(result).encode(),7),result)
        raw=('event: message\ndata: '+json.dumps(result)+'\n\n').encode()
        self.assertEqual(scout.decode_rpc(raw,7),result)
        with self.assertRaises(RuntimeError): scout.decode_rpc(raw,8)

    def test_http_error_shows_stage_and_redacts_key(self):
        c=scout.MCP('test-key',0)
        err=urllib.error.HTTPError(scout.ENDPOINT,403,'Forbidden',{},io.BytesIO(b'bad key test-key'))
        with patch('scout.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=err
            with self.assertRaisesRegex(RuntimeError,'HTTP 403.*initialize.*bad key \\[ключ скрыт\\]') as raised:
                c.rpc('initialize',{})
        self.assertNotIn('test-key',str(raised.exception))

    def test_budget_stops_second_call(self):
        c=self.client()
        with patch.object(c,'rpc',return_value={'videos':[],'_credits':{'used':5}}) as rpc:
            c.call('vidiq_youtube_search',{'query':'test'})
            with self.assertRaises(RuntimeError): c.call('vidiq_youtube_search',{'query':'test2'})
            self.assertEqual(rpc.call_count,1)
            self.assertEqual(c.actual,5)

    def test_failed_call_not_retried_or_budget_released(self):
        c=self.client()
        with patch.object(c,'rpc',side_effect=RuntimeError('network')) as rpc:
            with self.assertRaises(RuntimeError): c.call('vidiq_youtube_search',{'query':'test'})
            self.assertEqual(rpc.call_count,1)
            self.assertEqual(c.reserved,5)
            self.assertEqual(c.unknown_charges,1)

    def test_missing_charge_not_counted_as_zero(self):
        c=self.client()
        with patch.object(c,'rpc',return_value={'videos':[]}): c.call('vidiq_youtube_search',{'query':'test'})
        self.assertEqual(c.unknown_charges,1)

    def test_schema_change_blocks_before_charge(self):
        c=self.client()
        with patch.object(c,'rpc') as rpc:
            with self.assertRaises(RuntimeError): c.call('vidiq_youtube_search',{'query':'test','fake':True})
            rpc.assert_not_called()
            self.assertEqual(c.reserved,0)

    def test_write_tools_blocked(self):
        c=self.client()
        with self.assertRaises(ValueError): c.call('vidiq_update_video',{})

if __name__=='__main__': unittest.main()
