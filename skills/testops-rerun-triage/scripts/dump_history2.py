import sys, datetime
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, '.')
from _paths import artifacts_root
from testops_client import init_state, load_config, resolve_token, get_json

root = artifacts_root(None)
cfg = load_config(root)
token = resolve_token(cfg)
init_state(cfg, token)
for num, tc in (('10.05.03.04', 282183), ('10.05.03.09', 282185)):
    h = get_json(f'/api/testcase/{tc}/history?size=8')
    print('#' * 15, num, 'tc', tc)
    for e in (h.get('content') or [])[:8]:
        dt = datetime.datetime.fromtimestamp(e.get('createdDate', 0) / 1000).strftime('%m-%d %H:%M')
        ln = (e.get('launch') or {}).get('name', '')[:34]
        dur = e.get('duration')
        print(f"  {e.get('status'):7s} {dt} {ln:34s} dur={dur}")
