"""Składnia skryptów z wyrenderowanych widoków i regresje generatora (Node.js)."""
import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from test_workflows import app, client_for


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__();self.scripts=[];self.current=None
    def handle_starttag(self,tag,attrs):
        if tag=='script' and dict(attrs).get('type')!='application/json' and not dict(attrs).get('src'):
            self.current=''
    def handle_data(self,data):
        if self.current is not None:self.current+=data
    def handle_endtag(self,tag):
        if tag=='script' and self.current is not None:
            self.scripts.append(self.current);self.current=None


def test_rendered_javascript(app,tmp_path):
    if not shutil.which('node'):pytest.skip('Node.js is needed for JavaScript checks')
    client=client_for(app,4)
    index=0
    for url in ['/leave/new','/rozliczenie-godzin?month=2026-09','/employees/1','/admin/settings','/requests','/kadry/rozliczenia-pracownikow']:
        parser=Scripts();parser.feed(client.get(url).get_data(as_text=True))
        for script in parser.scripts:
            path=tmp_path/f'script-{index}.js';path.write_text(script);index+=1
            subprocess.run(['node','--check',str(path)],check=True,capture_output=True,text=True)


def test_generator_regressions(app):
    if not shutil.which('node'):pytest.skip('Node.js is needed for JavaScript checks')
    html=client_for(app,5).get('/rozliczenie-godzin?month=2026-09').get_data(as_text=True)
    context=json.loads(html.split('<script id="timesheetContext" type="application/json">')[1].split('</script>')[0])
    parser=Scripts();parser.feed(html)
    script=next(s for s in parser.scripts if 'function generate()' in s)
    subprocess.run(['node',str(Path(__file__).with_name('generator_regressions.cjs'))],input=json.dumps({'context':context,'script':script}),text=True,check=True,capture_output=True)
