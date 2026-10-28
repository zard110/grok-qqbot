import json, pathlib, sys, tempfile, unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from core.memory import MemoryStore, MemoryConflict

class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=pathlib.Path(self.tmp.name); self.store=MemoryStore(self.root)
    def test_documents_scoped_versioned_deleted_and_literal(self):
        s=self.store
        s.index_document('a','guide.txt','A.B\n训练功率计划')
        s.index_document('b','guide.txt','other AxB')
        self.assertEqual(s.search_documents('a','A.B')[0]['file'],'guide.txt')
        self.assertEqual(s.search_documents('a','['),[])
        self.assertEqual(s.search_documents('a','other'),[])
        s.index_document('a','guide.txt','new facts')
        self.assertEqual(s.search_documents('a','A.B'),[])
        s.rollback_document('a','guide.txt',1)
        self.assertTrue(s.search_documents('a','A.B'))
        s.delete_document('a','guide.txt')
        self.assertEqual(s.search_documents('a','A.B'),[])
    def test_fts_fallback_round_trip(self):
        self.store.index_document('a','one.txt','alpha')
        fallback=MemoryStore(self.root,use_fts=False)
        fallback.index_document('a','one.txt','beta')
        self.assertEqual(fallback.search_documents('a','alpha'),[])
        self.assertTrue(fallback.search_documents('a','beta'))
        restored=MemoryStore(self.root)
        self.assertEqual(restored.search_documents('a','alpha'),[])
        self.assertTrue(restored.search_documents('a','beta'))
    def test_fact_cas_and_scope(self):
        m=self.store.save_memory('a','群','时间','周六')
        with self.assertRaises(MemoryConflict):
            self.store.save_memory('a','群','时间','周日',memory_id=m['id'],expected_revision=0)
        with self.assertRaises(KeyError):
            self.store.save_memory('b','群','时间','周日',memory_id=m['id'],expected_revision=1)
        self.store.delete_memory('a',m['id'],1)
        self.assertEqual(self.store.search_memories('a'),[])
    def test_summaries_interval_and_context_budget(self):
        a=self.store.add_message('a','1','x'*3000)
        b=self.store.add_message('a','2','y'*3000,'out')
        c=self.store.add_message('a','3','z'*3000)
        summary=self.store.create_summary('a',a,b)
        self.assertEqual(summary['method'],'extractive')
        with self.assertRaises(MemoryConflict): self.store.create_summary('a',b,c)
        self.store.save_memory('a','subject','predicate','f'*3000)
        self.assertLessEqual(len(json.dumps(self.store.context('a',max_chars=1000),ensure_ascii=False)),1000)
    def test_legacy_repeatable_torn_line_and_output_direction(self):
        goid='A'*32; folder=self.root/'groups'/goid;folder.mkdir(parents=True)
        events=folder/'last-events.jsonl';sent=folder/'sent-log.jsonl';notes=folder/'notes.md'
        events.write_text(json.dumps({'d':{'id':'one','content':'hello','group_openid':goid}})+'\n'+ '{"d":',encoding='utf-8')
        sent.write_text(json.dumps({'id':'two','scope':'group','content':'reply'})+'\n',encoding='utf-8')
        notes.write_text('legacy fact',encoding='utf-8')
        first=self.store.context(goid)
        self.assertEqual(len(first['messages']),2)
        self.assertEqual({m['direction'] for m in first['messages']},{'in','out'})
        self.assertEqual(len(self.store.search_memories(goid)),1)
        with events.open('a',encoding='utf-8') as f:f.write('{"id":"three","content":"later"}}\n')
        self.assertEqual(len(self.store.messages(goid)),3)
        self.assertEqual(len(self.store.messages(goid)),3)
    def test_export_restore(self):
        self.store.save_memory('a','subject','fact','value')
        self.store.index_document('a','guide.txt','knowledge')
        snapshot=self.store.export_scope('a')
        other=MemoryStore(self.root/'other');other.restore_scope('a',snapshot)
        self.assertTrue(other.search_memories('a','value'))
        self.assertTrue(other.search_documents('a','knowledge'))
        with self.assertRaises(ValueError):other.restore_scope('b',snapshot)

    def test_command_receipt_replays_atomically(self):
        first=self.store.apply_command('a','delivery-1:0','memory.add',text='remember this',actor='admin')
        second=self.store.apply_command('a','delivery-1:0','memory.add',text='remember this',actor='admin')
        self.assertEqual(first,second)
        self.assertEqual(len(self.store.search_memories('a')),1)
        with self.assertRaises(MemoryConflict):
            self.store.apply_command('a','delivery-1:0','memory.add',text='different',actor='admin')
        deleted=self.store.apply_command('a','delivery-2:0','memory.delete',memory_id=first['id'],actor='admin')
        self.assertEqual(deleted,self.store.apply_command('a','delivery-2:0','memory.delete',memory_id=first['id'],actor='admin'))
        self.assertEqual(self.store.search_memories('a'),[])

    def test_snapshot_restores_source_intervals_and_is_repeatable(self):
        a=self.store.add_message('a','in:one','one')
        b=self.store.add_message('a','out:two','two','out')
        self.store.create_summary('a',a,b,'summary')
        self.store.save_memory('a','subject','fact','value')
        snapshot=self.store.export_scope('a')
        other=MemoryStore(self.root/'restored')
        other.add_message('b','unrelated','message')
        other.restore_scope('a',snapshot);other.restore_scope('a',snapshot)
        self.assertEqual(len(other.messages('a')),2)
        self.assertEqual(len(other.summaries('a')),1)
        self.assertEqual(len(other.search_memories('a')),1)
        summary=other.summaries('a')[0]
        self.assertEqual(summary['from_seq'],other.messages('a')[0]['seq'])

if __name__=='__main__':unittest.main()
