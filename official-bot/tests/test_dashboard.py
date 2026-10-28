import http.client, json, pathlib, sys, tempfile, threading, unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import dashboard, groups as G, kb

class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=pathlib.Path(self.tmp.name);self.goid='A'*32
        for p in [patch.object(G,'DIR',self.root),patch.object(G,'GROUPS',self.root/'groups'),patch.object(G,'DEFAULT_DIR',self.root/'groups'/'_default'),patch.object(kb,'DATA_DIR',self.root)]:
            p.start();self.addCleanup(p.stop)
        G.ensure_group(self.goid)
        self.server=dashboard.DashboardServer(0,self.root,bootstrap='test-one-use-code')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close_server)
        self.cookie='';self.csrf=''
    def close_server(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)
    def request(self,path,body=None,extra=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        headers={'Cookie':self.cookie}
        if body is not None:headers.update({'Content-Type':'application/json','Origin':self.server.origin,'X-CSRF-Token':self.csrf})
        headers.update(extra or {})
        conn.request('GET' if body is None else 'POST',path,body=None if body is None else json.dumps(body),headers=headers)
        response=conn.getresponse();data=response.read();status=response.status
        cookie=response.getheader('Set-Cookie')
        conn.close()
        return status,json.loads(data) if response.getheader('Content-Type','').startswith('application/json') else data,cookie
    def login(self):
        status,data,cookie=self.request('/api/login',{'code':'test-one-use-code'})
        self.assertEqual(status,200);self.csrf=data['csrf'];self.cookie=cookie.split(';')[0]
        self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)
    def test_auth_host_origin_csrf_and_replay(self):
        self.assertEqual(self.request('/api/groups')[0],403)
        self.assertEqual(self.request('/',extra={'Host':'evil.example'})[0],403)
        self.login()
        self.assertEqual(self.request('/api/login',{'code':'test-one-use-code'})[0],403)
        self.assertEqual(self.request('/api/groups')[0],200)
        url='/api/groups/'+self.goid+'/config'
        revision=self.request(url)[1]['revision']
        body={'revision':revision,'patch':{'enabled':False}}
        self.assertEqual(self.request(url,body,{'Origin':'http://example.com'})[0],403)
        self.assertEqual(self.request(url,body,{'X-CSRF-Token':'bad'})[0],403)
        self.assertEqual(self.request(url,body)[0],200)
        self.assertEqual(self.request(url,body)[0],409)
    def test_memory_and_prompt_crud(self):
        self.login();prefix='/api/groups/'+self.goid
        status,m,_=self.request(prefix+'/memories',{'subject':'group','predicate':'time','value':'Saturday'})
        self.assertEqual(status,200)
        self.assertEqual(self.request(prefix+'/memories?q=Saturday')[1][0]['id'],m['id'])
        self.assertEqual(self.request(prefix+'/memories/delete',{'id':m['id'],'revision':m['revision']})[0],200)
        self.assertNotIn(m['id'],[row['id'] for row in self.request(prefix+'/memories')[1]])
        prompt=self.request(prefix+'/prompt')[1]
        self.assertEqual(self.request(prefix+'/prompt',{'revision':prompt['revision'],'text':'new persona'})[0],200)
        self.assertEqual(self.request(prefix+'/prompt',{'revision':prompt['revision'],'text':'stale'})[0],409)
    def test_path_escape_and_scoped_assets(self):
        self.login();prefix='/api/groups/'+self.goid
        self.assertEqual(self.request(prefix+'/knowledge/source?name=../../secret.txt')[0],400)
        self.assertEqual(self.request('/api/groups/_system/assets')[0],400)
        folder=G.group_dir(self.goid)/'stickers';folder.mkdir();(folder/'safe.png').write_bytes(b'PNG')
        (folder/'unsafe.svg').write_text('<svg/>')
        assets=self.request(prefix+'/assets')[1]
        self.assertEqual([x['name'] for x in assets],['safe.png'])
        self.assertEqual(self.request(prefix+'/asset?id='+assets[0]['id'])[0],200)
    def test_document_editor_cas(self):
        self.login();prefix='/api/groups/'+self.goid
        source=self.request(prefix+'/knowledge/source?name=test.md')[1]
        body={'name':'test.md','text':'knowledge text','revision':source['revision']}
        self.assertEqual(self.request(prefix+'/knowledge/save',body)[0],200)
        self.assertEqual(self.request(prefix+'/knowledge/save',body)[0],409)
        self.assertTrue(self.request(prefix+'/knowledge?q=knowledge')[1])

    def test_missing_document_get_hides_content_without_mutating_index(self):
        self.login();prefix='/api/groups/'+self.goid
        self.server.store.index_document(self.goid,'missing.md','removed content')
        self.assertEqual(self.request(prefix+'/knowledge?q=removed')[1],[])
        self.assertEqual(self.request(prefix+'/knowledge')[1],[])
        self.assertEqual(self.server.store.documents(self.goid)[0]['active'],1)
        kb.refresh_index(self.goid,G.load_config(self.goid),self.server.store)
        self.assertEqual(self.server.store.documents(self.goid)[0]['active'],0)

    def test_explicit_import_rejects_notes_symlink_outside_group(self):
        self.login();prefix='/api/groups/'+self.goid
        outside=self.root/'outside.txt';outside.write_text('outside private text')
        notes=G.group_dir(self.goid)/'notes.md';notes.unlink()
        try:
            notes.symlink_to(outside)
        except OSError:
            self.skipTest('Creating symlinks is unavailable for this Windows account')
        self.assertEqual(self.request(prefix+'/import',{})[0],400)
        with self.server.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM memories WHERE scope=?',(self.goid,)).fetchone()[0],0)

if __name__=='__main__':unittest.main()
