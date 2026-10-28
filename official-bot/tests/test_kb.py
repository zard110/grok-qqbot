import argparse, contextlib, io, json, pathlib, sys, tempfile, unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import groups as G
import kb
from core.memory import MemoryStore

class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=pathlib.Path(self.tmp.name);self.goid='A'*32
        self.patches=[patch.object(G,'DIR',self.root),patch.object(G,'GROUPS',self.root/'groups'),patch.object(G,'DEFAULT_DIR',self.root/'groups'/'_default'),patch.object(kb,'DATA_DIR',self.root)]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        self.cfg,_=G.ensure_group(self.goid)
    def test_prefix_escape_rejected(self):
        cfg={'knowledge':{'docs_dir':'../'+self.goid+'-outside'}}
        with self.assertRaises(ValueError):kb.kdir(self.goid,cfg)
    def test_deleted_source_cannot_recall_stale_pdf_cache(self):
        kd=kb.kdir(self.goid,self.cfg);(kd/'guide.txt').write_text('present',encoding='utf-8')
        cache=kd/'.pdftext';cache.mkdir();(cache/'removed.pdf.txt').write_text('deleted secret')
        store=MemoryStore(self.root)
        self.assertEqual(kb.refresh_index(self.goid,self.cfg,store),[])
        self.assertEqual(store.search_documents(self.goid,'deleted'),[])
        (kd/'guide.txt').unlink();kb.refresh_index(self.goid,self.cfg,store)
        self.assertEqual(store.search_documents(self.goid,'present'),[])
    def test_pdf_failure_revokes_old_index(self):
        kd=kb.kdir(self.goid,self.cfg);(kd/'file.pdf').write_bytes(b'%PDF-fake')
        store=MemoryStore(self.root);store.index_document(self.goid,'file.pdf','stale')
        with patch.object(kb,'_pdf_text',side_effect=ValueError('parse failed')):
            self.assertTrue(kb.refresh_index(self.goid,self.cfg,store))
        self.assertEqual(store.search_documents(self.goid,'stale'),[])
    def test_reject_unsupported_import(self):
        source=self.root/'bad.exe';source.write_bytes(b'bad')
        with self.assertRaises(ValueError):kb.cmd_add_doc(self.goid,self.cfg,argparse.Namespace(path=str(source)))

if __name__=='__main__':unittest.main()
