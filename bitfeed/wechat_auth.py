"""VPS-only public-platform QR login; no desktop-WeChat access or browser cookies.

Protocol verified against rachelos/we-mp-rss at commit
9fd4edb2a9311bffa571db050f53f415705db294, driver/wx_api.py.
Credentials stay in a private directory and are never logged or exported.
"""
import argparse
import json
import os
from pathlib import Path
import re
import time
import uuid

import requests

BASE="https://mp.weixin.qq.com"


def private_json(path,data):
    temporary=path.with_suffix(path.suffix+'.next')
    with open(temporary,'w',encoding='utf-8') as file:
        os.chmod(temporary,0o600)
        json.dump(data,file,ensure_ascii=False)
    os.replace(temporary,path)


def login(directory,wait_seconds=300,session=None):
    root=Path(directory)
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(root,0o700)
    def status(state,**details):
        private_json(root/'status.json',{'state':state,'updated_at':int(time.time()),**details})
        print(json.dumps({'state':state,**details}),flush=True)
    http=session or requests.Session()
    http.headers.update({'User-Agent':'Mozilla/5.0','Referer':BASE+'/'})
    fingerprint=uuid.uuid4().hex
    def request(method,path,**kwargs):
        response=getattr(http,method)(BASE+path,timeout=(10,15),allow_redirects=False,**kwargs)
        if response.status_code!=200:
            raise RuntimeError('HTTP '+str(response.status_code))
        return response
    status('starting')
    try:
        response=request('post','/cgi-bin/bizlogin',params={'action':'startlogin'},data={
            'fingerprint':fingerprint,'token':'','lang':'zh_CN','f':'json','ajax':'1',
            'redirect_url':'/cgi-bin/home?t=home/index&lang=zh_CN','login_type':'3'})
        identifier=http.cookies.get('uuid')
        if not identifier:
            match=re.search(r'uuid[\x27\x22]?\s*[:=]\s*[\x27\x22]([^\x27\x22]+)',response.text)
            identifier=match.group(1) if match else None
        if not identifier:
            status('unavailable',reason='startlogin returned no QR session')
            return False
        http.cookies.set('uuid',identifier)
        image=request('get','/cgi-bin/scanloginqrcode',params={'action':'getqrcode','uuid':identifier,'random':int(time.time()*1000)})
        if not image.headers.get('Content-Type','').startswith('image/') or len(image.content)>1000000:
            status('unavailable',reason='QR image not returned')
            return False
        (root/'qr.png').write_bytes(image.content)
        os.chmod(root/'qr.png',0o600)
        status('ready',expires_in=wait_seconds)
        deadline=time.monotonic()+wait_seconds
        last_state='ready'
        while time.monotonic()<deadline:
            time.sleep(3)
            response=request('get','/cgi-bin/scanloginqrcode',params={
                'action':'ask','fingerprint':fingerprint,'lang':'zh_CN','f':'json','ajax':'1'})
            try:data=response.json()
            except ValueError:
                status('blocked',reason='verification or non-JSON response');return False
            if 'invalid session' in str(data).lower():
                status('expired');return False
            code=data.get('status',0)
            if code in (2,4):
                if last_state!='scanned':status('scanned')
                last_state='scanned'
            if code not in (1,3):
                continue
            response=request('post','/cgi-bin/bizlogin',params={'action':'login'},data={
                'userlang':'zh_CN','redirect_url':'','cookie_forbidden':'0','cookie_cleaned':'0',
                'plugin_used':'0','login_type':'3','fingerprint':fingerprint,'token':'',
                'lang':'zh_CN','f':'json','ajax':'1'})
            match=re.search(r'token=([^&\s\x22\x27]+)',response.text)
            if not match:
                status('confirmation_required',reason='confirmed scan did not return a platform session');return False
            token=match.group(1)
            accounts=request('get','/cgi-bin/switchacct',params={'action':'get_acct_list',
                'fingerprint':fingerprint,'token':token,'lang':'zh_CN','f':'json','ajax':'1'}).json()
            available=(accounts.get('biz_list') or {}).get('list') or []
            if accounts.get('base_resp',{}).get('ret') != 0 or not available:
                status('no_platform_account',reason='scan completed but no accessible public-platform account');return False
            cookies=[{'name':c.name,'value':c.value,'domain':c.domain,'path':c.path,'expires':c.expires} for c in http.cookies]
            private_json(root/'session.json',{'token':token,'fingerprint':fingerprint,'cookies':cookies,
                'saved_at':int(time.time()),'user_agent':http.headers['User-Agent']})
            expiry=[int(c['expires']) for c in cookies if c['name'] in ('slave_sid','slave_user','data_ticket','bizuin') and c['expires']]
            private_json(root/'keepalive.json',{'state':'ok','checked_at':int(time.time()),'expires_at':min(expiry) if expiry else None})
            try:
                if json.loads((root/'cooldown.json').read_text(encoding='utf-8')).get('state')=='auth_expired':
                    (root/'cooldown.json').unlink()
            except (OSError,ValueError):
                pass
            status('authorized',account_count=len(available))
            return True
        status('expired');return False
    except (requests.RequestException,ValueError,RuntimeError) as exc:
        status('failed',error_type=type(exc).__name__)
        return False
    finally:
        if session is None:http.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',required=True)
    parser.add_argument('--wait-seconds',type=int,default=300)
    args=parser.parse_args()
    raise SystemExit(0 if login(args.directory,args.wait_seconds) else 1)
