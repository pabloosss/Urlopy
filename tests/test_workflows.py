"""Regresje na odizolowanej bazie SQLite; bez dostępu do danych produkcyjnych.

Uruchomienie: python3 -m pytest -q
"""
import calendar
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from unittest.mock import patch

import pytest
from werkzeug.security import generate_password_hash

import emerlog_leave
from emerlog_leave import database
from emerlog_leave.routes_timesheets_v4 import _ensure_submission_table
from emerlog_leave.services import polish_holidays


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DATABASE', str(tmp_path / 'isolated.sqlite'))
    monkeypatch.setattr(emerlog_leave, 'FORCE_HTTPS', False)
    # Testy nie uruchamiają schedulera backupów.
    with patch.object(emerlog_leave.threading.Thread, 'start'):
        app = emerlog_leave.create_app()
    app.config.update(TESTING=True, SECRET_KEY=secrets.token_hex(32), SESSION_COOKIE_SECURE=False)
    conn = database.get_db()
    password_hash = generate_password_hash(secrets.token_urlsafe(24))
    for uid, role, contract, fte in [(1,'pracownik','Umowa o pracę',100),(2,'menedzer','Umowa o pracę',75),(3,'kadry','Umowa o pracę',100),(4,'admin','Umowa o pracę',100),(5,'pracownik','Umowa zlecenie',100),(6,'pracownik','Umowa o pracę',100)]:
        conn.execute('INSERT INTO users (id,login,password_hash,full_name,role,department,contract_type,fte_percent,manager_id) VALUES (?,?,?,?,?,?,?,?,?)', (uid,f'fixture-{uid}',password_hash,f'Osoba Testowa{uid}',role,'IT',contract,fte,2 if uid==1 else None))
    conn.commit()
    conn.close()
    return app


def client_for(app, uid=1):
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = uid
        session['_csrf_token'] = 'fixture-csrf'
    return client


def send(client, path, payload):
    return client.post(path, json=payload, headers={'X-CSRF-Token':'fixture-csrf'})


def payload(year=2026, month=9, hours=8):
    rows=[]
    for day in range(1,calendar.monthrange(year,month)[1]+1):
        dt=date(year,month,day)
        off=dt.weekday()>=5 or dt in polish_holidays(year)
        rows.append(dict(day=day,iso=dt.isoformat(),weekday='ignored',start='-' if off else '08:00',end='-' if off else f'{8+hours:02}:00',hours=0 if off else hours,overtime=0,off=off,off_source='weekend' if off else '',leave='-',sign_employee='',sign_company=''))
    return dict(year=year,month=month,rows=rows,target_hours=None)


def test_versions_and_draft_are_independent(app):
    client=client_for(app)
    body=payload()
    first=send(client,'/rozliczenie-godzin/submit',body)
    assert first.status_code==200
    original=first.json
    body['rows'][0].update(overtime=2,end='18:00')
    assert send(client,'/rozliczenie-godzin/save',body).status_code==200
    conn=database.get_db()
    old=conn.execute('SELECT rows_json FROM hour_timesheet_submissions WHERE id=?',(original['submission_id'],)).fetchone()[0]
    assert json.loads(old)[0]['overtime']==0
    assert conn.execute('SELECT count(*) FROM hour_timesheet_submissions').fetchone()[0]==1
    conn.close()
    second=send(client,'/rozliczenie-godzin/submit',body)
    assert second.status_code==200 and second.json['version_no']==2
    conn=database.get_db()
    assert conn.execute('SELECT rows_json FROM hour_timesheet_submissions WHERE id=?',(original['submission_id'],)).fetchone()[0]==old
    assert conn.execute('SELECT count(*) FROM hour_timesheets').fetchone()[0]==1
    conn.close()
    hr=client_for(app,3)
    page=hr.get('/kadry/rozliczenia-pracownikow').get_data(as_text=True)
    assert page.index('>v2<')<page.index('>v1<')
    detail=hr.get(f"/kadry/rozliczenia-pracownikow/wersja/{second.json['submission_id']}")
    assert detail.status_code==200 and b'18:00' in detail.data
    assert client.get('/rozliczenie-godzin?month=2026-09').status_code==200


@pytest.mark.parametrize('bad', [[1], 'text', 7, {'year':'oops'}, {'year':float('inf')}])
def test_invalid_payload_returns_400(app,bad):
    assert send(client_for(app),'/rozliczenie-godzin/submit',bad).status_code==400


@pytest.mark.parametrize('field,value', [('hours',float('nan')),('hours',float('inf')),('overtime',float('nan')),('overtime',9),('end','16:99'),('end','15:00'),('start','garbage'),('off','false')])
def test_invalid_row_rejected(app,field,value):
    body=payload();body['rows'][0][field]=value
    assert send(client_for(app),'/rozliczenie-godzin/save',body).status_code==400


def test_off_day_cannot_hide_hours(app):
    body=payload();body['rows'][5]['hours']=4
    assert send(client_for(app),'/rozliczenie-godzin/submit',body).status_code==400


def test_absence_cannot_be_overridden(app):
    conn=database.get_db()
    conn.execute("INSERT INTO leave_requests(user_id,leave_type,date_from,date_to,days_count,status) VALUES (1,'Urlop wypoczynkowy','2026-09-01','2026-09-01',1,'zaakceptowany')")
    conn.commit();conn.close()
    body=payload()
    client=client_for(app)
    assert send(client,'/rozliczenie-godzin/submit',body).status_code==400
    body['rows'][0].update(off=True,start='-',end='-',hours=0)
    assert send(client,'/rozliczenie-godzin/submit',body).status_code==200
    conn=database.get_db()
    assert json.loads(conn.execute('SELECT rows_json FROM hour_timesheet_submissions').fetchone()[0])[0]['leave']=='Urlop'
    conn.close()


def test_zlecenie_target_and_overtime(app):
    client=client_for(app,5);body=payload()
    body['target_hours']=sum(r['hours'] for r in body['rows'])
    assert send(client,'/rozliczenie-godzin/submit',body).status_code==200
    body['target_hours']+=1
    assert send(client,'/rozliczenie-godzin/submit',body).status_code==400
    body['target_hours']-=1
    body['rows'][0].update(overtime=1,end='17:00')
    assert send(client,'/rozliczenie-godzin/submit',body).status_code==400


@pytest.mark.parametrize('uid',[1,2,5])
def test_employee_roles_cannot_read_hr_data(app,uid):
    client=client_for(app,uid)
    for url in ['/kadry/rozliczenia-pracownikow','/kadry/rozliczenia-pracownikow/wersja/1','/kadry/rozliczenia-pracownikow/pracownik/6.json','/employees/6','/limits']:
        response=client.get(url)
        assert response.status_code==302


def test_cannot_submit_for_another_employee_or_without_csrf(app):
    client=client_for(app);body=payload();body['user_id']=6
    assert client.post('/rozliczenie-godzin/submit',json=body).status_code==400
    assert send(client,'/rozliczenie-godzin/submit',body).status_code==200
    conn=database.get_db()
    assert conn.execute('SELECT user_id FROM hour_timesheet_submissions').fetchone()[0]==1
    conn.close()


def test_legacy_submission_migration_is_idempotent(app):
    conn=database.get_db()
    conn.execute("INSERT INTO hour_timesheets(user_id,year,month,contract_type,rows_json,last_sent_at) VALUES (1,2026,9,'Umowa o pracę',?,'2026-09-01 12:00:00')",(json.dumps(payload()['rows']),))
    _ensure_submission_table(conn);conn.commit()
    _ensure_submission_table(conn);conn.commit()
    assert conn.execute('SELECT count(*) FROM hour_timesheet_submissions').fetchone()[0]==1
    conn.close()


def test_parallel_submissions_preserve_both_versions(app):
    clients=[client_for(app),client_for(app)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses=list(executor.map(lambda c:send(c,'/rozliczenie-godzin/submit',payload()),clients))
    assert sorted(r.json['version_no'] for r in responses)==[1,2]


def test_parallel_leave_requests_cannot_overlap(app):
    clients=[client_for(app),client_for(app)]
    form=dict(_csrf_token='fixture-csrf',leave_type='Urlop wypoczynkowy',date_from='2026-09-14',date_to='2026-09-15')
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses=list(executor.map(lambda c:c.post('/leave/new',data=form),clients))
    assert sorted(r.status_code for r in responses)==[302,400]
    conn=database.get_db()
    assert conn.execute('SELECT count(*) FROM leave_requests').fetchone()[0]==1
    conn.close()


def test_leave_limit_cancel_and_reaccept(app):
    client=client_for(app)
    form=dict(_csrf_token='fixture-csrf',leave_type='Urlop wypoczynkowy',date_from='2026-09-14',date_to='2026-09-15')
    assert client.post('/leave/new',data=form).status_code==302
    other=client_for(app,6)
    other.post('/request/1/cancel',data={'_csrf_token':'fixture-csrf'})
    conn=database.get_db();assert conn.execute('SELECT status FROM leave_requests WHERE id=1').fetchone()[0]=='zaakceptowany';conn.close()
    client.post('/request/1/cancel',data={'_csrf_token':'fixture-csrf'})
    client_for(app,3).post('/request/1/accept',data={'_csrf_token':'fixture-csrf'})
    conn=database.get_db();assert conn.execute('SELECT status FROM leave_requests WHERE id=1').fetchone()[0]=='zaakceptowany';conn.close()


def test_pages_templates_and_endpoint_links(app):
    for name in app.jinja_env.list_templates():
        app.jinja_env.get_template(name)
    hr=client_for(app,4)
    for url in ['/dashboard','/my-leave','/leave/new','/requests','/requests/all','/kadry','/employees','/employees/1','/limits','/calendar','/presence','/reports','/audit','/settings','/rozliczenie-godzin','/kadry/rozliczenia-pracownikow']:
        response=hr.get(url,follow_redirects=True)
        assert response.status_code==200, url
