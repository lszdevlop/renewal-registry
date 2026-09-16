#!/usr/bin/env python3
"""Selection styling contract and actual JS state synchronization; no live data."""
from pathlib import Path
import re
import subprocess

html = (Path(__file__).resolve().parents[1] / 'index.html').read_text()
match = re.search(r'    function syncRenewalAlertSelection\(\) \{.*?\n    \}', html, re.S)
assert match, 'missing persistent reminder selection synchronization'
js = '''
let CURRENT_DOMAIN = 'a', focusedMemberKey = 'id:1';
const items = [['a','id:1'],['b','id:1'],['a','id:2']].map(([domain, memberKey]) => ({
 dataset:{domain,memberKey}, attrs:{}, selected:false,
 classList:{toggle(name,value){this.owner.selected=value}},
 setAttribute(name,value){this.attrs[name]=String(value)}
}));
items.forEach(x=>x.classList.owner=x);
const document = {querySelectorAll:()=>items};
function check(expected){
 syncRenewalAlertSelection();
 if(JSON.stringify(items.map(x=>x.selected))!==JSON.stringify(expected))throw Error('wrong selection');
 items.forEach(x=>{if(x.attrs['aria-pressed']!==String(x.selected))throw Error('aria state mismatch')});
}
'''+match.group(0)+'''
check([true,false,false]);
CURRENT_DOMAIN='b';check([false,true,false]);
focusedMemberKey=null;check([false,false,false]);
CURRENT_DOMAIN='a';focusedMemberKey='id:2';check([false,false,true]);
console.log('PASS domain-safe selection, switching, clearing, aria state');
'''
subprocess.run(['node', '-e', js], check=True)
assert html.count('syncRenewalAlertSelection();') >= 2, 'sync after both table and alert rerenders'
assert 'content: "已选中"' not in html, 'selection must use highlight only, without a text badge'
for token in ['.renewal-alert-item.is-selected', 'tr.member-focused > td', 'memberFocusKey(m) === focusedMemberKey']:
    assert token in html, f'missing selection visual contract: {token}'
print('PASS persistent reminder and member-row styling contract')
