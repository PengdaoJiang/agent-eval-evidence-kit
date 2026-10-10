import json,tempfile,unittest
from pathlib import Path
from review_intake.results import ingest_result

class IntakeTests(unittest.TestCase):
    def ingest(self,root,version,findings,previous=None):
        identity=dict(request_id='request-'+version,role='rtl',batch_id='demo-batch',snapshot_id=version,chat_id='synthetic-review')
        request=root/('request-'+version+'.json');response=root/('response-'+version+'.txt');annotations=root/('annotations-'+version+'.json')
        request.write_text(json.dumps(identity));response.write_text('Original synthetic review '+version)
        annotations.write_text(json.dumps({**identity,'findings':findings,'coverage':{'status':'synthetic fixture'}}))
        return ingest_result(request,response,annotations,root/'inbox',previous_result=previous)
    def finding(self,status='open',evidence='synthetic trace'):
        return dict(id='F1',severity='high',location='example.py:1',mechanism='Ownership survives reset',trigger='reset with pending work',reachability='module_only',minimum_verification='inject reset',status=status,evidence=evidence)
    def test_repeated_intake_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);first=self.ingest(root,'v1',[self.finding()]);second=self.ingest(root,'v1',[self.finding()])
            self.assertFalse(first['duplicate']);self.assertTrue(second['duplicate'])
    def test_omission_never_closes_a_finding(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);first=self.ingest(root,'v1',[self.finding()]);second=self.ingest(root,'v2',[],first)
            value=json.loads(Path(second['result_path']).read_text(encoding='utf-8'))
            self.assertEqual(value['counts']['carried'],1)
            self.assertEqual(value['findings'][0]['status'],'open')
            self.assertEqual(value['findings'][0]['observed_snapshot_id'],'v1')
            self.assertNotIn('Ownership survives reset',Path(second['index_path']).read_text(encoding='utf-8'))
    def test_closure_requires_explicit_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError,'requires explicit evidence'):self.ingest(Path(d),'v1',[self.finding('closed','')])
    def test_annotation_rewrite_cannot_rebind_response(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.ingest(root,'v1',[self.finding()])
            with self.assertRaisesRegex(ValueError,'already bound'):self.ingest(root,'v1',[])
if __name__=='__main__':unittest.main()
