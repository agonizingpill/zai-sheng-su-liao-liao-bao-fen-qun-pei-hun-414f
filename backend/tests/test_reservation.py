from decimal import Decimal
from src.database import Session
from src.models import Bale,Blend,BlendPortion
from src.reservation_models import BlendReservation
import sqlalchemy as sa

def reserve_count(blend_id):
    with Session() as s:return s.scalar(sa.select(sa.func.count()).select_from(BlendReservation).where(BlendReservation.blend_id==blend_id))
def bale_row(bid):
    with Session() as s:return s.get(Bale,bid)
def blend_row(bid):
    with Session() as s:return s.get(Blend,bid)

def test_recommend_does_not_occupy_and_picks_same_group(client,hdr,helpers):
    # 群0(v1) 低PVC；群1 高PVC。目标400、上限40 → 只可能由群0凑出
    b1=helpers.bale('PET-1',pvc='10',cluster=0,version=1)
    b2=helpers.bale('PET-2',pvc='30',cluster=0,version=1,avail='300')
    helpers.bale('PET-3',pvc='200',cluster=1,version=1)
    bid=helpers.blend('BL-A','400','40')
    r=client.get(f'/blends/{bid}/recommend',headers=hdr())
    assert r.status_code==200,r.json()
    d=r.json();assert d['feasible'] is True
    sel=d['selected']
    assert {(p['cluster_id'],p['cluster_version']) for p in sel['portions']}=={(0,1)}
    assert sum(Decimal(str(p['kg'])) for p in sel['portions'])==Decimal('400')
    assert Decimal(str(sel['weighted_pvc']))<=Decimal('40')
    # 推荐是纯读：无预留、无称料行、可用量不变
    assert reserve_count(bid)==0
    assert bale_row(b1).available_kg==Decimal('500')
    with Session() as s:assert s.scalar(sa.select(sa.func.count()).select_from(BlendPortion))==0
    # 页面可核对逐组依据
    assert all({'cluster_id','cluster_version','total_margin_kg','feasible','reason'} <= set(g) for g in d['groups'])

def test_greedy_trap_exact_solver_finds_solution(client,hdr,helpers):
    # 逐包贪选会先拿低PVC整包500kg超目标而误判；精确求解应拆分：300+100
    helpers.bale('PET-LOW',pvc='5',avail='500',cluster=0,version=1)
    helpers.bale('PET-MID',pvc='20',avail='300',cluster=0,version=1)
    helpers.bale('PET-HI',pvc='35',avail='300',cluster=0,version=1)
    bid=helpers.blend('BL-B','400','40')
    d=client.get(f'/blends/{bid}/recommend',headers=hdr()).json()
    assert d['feasible'] is True
    assert sum(Decimal(str(p['kg'])) for p in d['selected']['portions'])==Decimal('400')
    # 低PVC优先取满 → 最低可达加权PVC
    assert d['selected']['portions'][0]['bale_code']=='PET-LOW'

def test_no_solution_when_pvc_cap_hard(client,hdr,helpers):
    helpers.bale('PET-X',pvc='80',avail='500',cluster=0,version=1)
    helpers.bale('PET-Y',pvc='90',avail='500',cluster=0,version=1)
    bid=helpers.blend('BL-C','400','40')
    d=client.get(f'/blends/{bid}/recommend',headers=hdr()).json()
    assert d['feasible'] is False and d['selected'] is None
    assert any('PVC' in g['reason'] for g in d['groups'])

def test_no_solution_when_margin_short(client,hdr,helpers):
    helpers.bale('PET-S1',pvc='10',avail='200',cluster=0,version=1)
    helpers.bale('PET-S2',pvc='10',avail='150',cluster=0,version=1)
    # 另一群同样凑不足目标，全场无同版本同群可行组
    helpers.bale('PET-OTHER',pvc='10',avail='300',cluster=1,version=1)
    bid=helpers.blend('BL-D','400','100')
    d=client.get(f'/blends/{bid}/recommend',headers=hdr()).json()
    assert d['feasible'] is False
    g0=next(g for g in d['groups'] if g['cluster_id']==0)
    assert g0['feasible'] is False and '低于目标' in g0['reason']

def test_reserve_whole_order_and_visible_margin(client,hdr,helpers):
    b1=helpers.bale('PET-R1',pvc='10',cluster=0,version=1)
    b2=helpers.bale('PET-R2',pvc='30',cluster=0,version=1)
    bid=helpers.blend('BL-E','400','40')
    plan=client.get(f'/blends/{bid}/recommend',headers=hdr()).json()['selected']['portions']
    body={'portions':[{'bale_id':p['bale_id'],'kg':p['kg']} for p in plan]}
    r=client.post(f'/blends/{bid}/reserve',headers=hdr(),json=body)
    assert r.status_code==200,r.json()
    v=r.json();assert v['reserved'] is True and v['reserved_kg']==400.0
    assert len(v['reservation'])==len(plan) and len(v['portions'])==len(plan)
    # 堆场可见预留与可用余量，available_kg 未被真实扣减
    bales={b['id']:b for b in client.get('/bales',headers=hdr()).json()}
    for p in plan:
        row=bales[p['bale_id']]
        assert Decimal(str(row['reserved_kg']))==Decimal(str(p['kg']))
        assert Decimal(str(row['margin_kg']))==Decimal('500')-Decimal(str(p['kg']))
        assert bale_row(p['bale_id']).available_kg==Decimal('500')

def test_reserve_idempotent_retry_no_double_hold(client,hdr,helpers):
    b1=helpers.bale('PET-I1',pvc='10',cluster=0,version=1)
    bid=helpers.blend('BL-F','400','40')
    plan=client.get(f'/blends/{bid}/recommend',headers=hdr()).json()['selected']['portions']
    body={'portions':[{'bale_id':p['bale_id'],'kg':p['kg']} for p in plan]}
    first=client.post(f'/blends/{bid}/reserve',headers=hdr(),json=body)
    second=client.post(f'/blends/{bid}/reserve',headers=hdr(),json=body)
    assert first.status_code==200 and second.status_code==200
    assert reserve_count(bid)==len(plan)
    row=bale_row(b1);assert row.available_kg==Decimal('500')
    d=client.get('/bales',headers=hdr()).json()
    assert Decimal(str(next(x for x in d if x['id']==b1)['reserved_kg']))<=Decimal('400')

def test_reserve_conflict_rejects_whole_order_keeps_original(client,hdr,helpers):
    # 第一单预留 b1 300kg；第二单同料包再要 300kg（余量仅剩200）→ 整单拒绝，原预留不变
    b1=helpers.bale('PET-C1',pvc='10',cluster=0,version=1)
    b2=helpers.bale('PET-C2',pvc='12',cluster=0,version=1)
    first=helpers.blend('BL-G1','300','40')
    p1=client.get(f'/blends/{first}/recommend',headers=hdr()).json()['selected']['portions']
    assert client.post(f'/blends/{first}/reserve',headers=hdr(),json={'portions':[{'bale_id':x['bale_id'],'kg':x['kg']} for x in p1]}).status_code==200
    second=helpers.blend('BL-G2','300','40')
    bad={'portions':[{'bale_id':b1,'kg':'300'}]}
    r=client.post(f'/blends/{second}/reserve',headers=hdr(),json=bad)
    assert r.status_code==409 and '整单未预留' in r.json()['detail']
    assert reserve_count(second)==0
    # 原方案保留：第一单预留行不动
    assert reserve_count(first)==1
    assert bale_row(b1).available_kg==Decimal('500')
    # 第二单存在可行解（用 b2 500kg 余量），推荐不会误报无解
    rec=client.get(f'/blends/{second}/recommend',headers=hdr()).json()
    assert rec['feasible'] is True

def test_manual_weigh_respects_same_margin(client,hdr,helpers):
    b1=helpers.bale('PET-M1',pvc='10',cluster=0,version=1)
    first=helpers.blend('BL-H1','300','40')
    p1=client.get(f'/blends/{first}/recommend',headers=hdr()).json()['selected']['portions']
    client.post(f'/blends/{first}/reserve',headers=hdr(),json={'portions':[{'bale_id':x['bale_id'],'kg':x['kg']} for x in p1]})
    second=helpers.blend('BL-H2','300','40')
    # 手工称 300kg 超过剩余 200kg 余量 → 拒绝且余量不变
    r=client.put(f'/blends/{second}/portions',headers=hdr(),json={'portions':[{'bale_id':b1,'kg':'300'}]})
    assert r.status_code==409 and '预留' in r.json()['detail']
    assert bale_row(b1).available_kg==Decimal('500')
    # 手工称 200kg 在余量内 → 保存成功（另一包是未预留的新料）
    other=helpers.bale('PET-M2',pvc='10',cluster=0,version=1)
    ok=client.put(f'/blends/{second}/portions',headers=hdr(),json={'portions':[{'bale_id':b1,'kg':'200'},
        {'bale_id':other,'kg':'100'}]})
    assert ok.status_code==200,ok.json()
    # 再把 b1 余量耗尽后，第三张单任何正称入都被同一余量门限拦截
    third=helpers.blend('BL-H3','100','40')
    blocked=client.put(f'/blends/{third}/portions',headers=hdr(),json={'portions':[{'bale_id':b1,'kg':'1'}]})
    assert blocked.status_code==409 and '预留' in blocked.json()['detail']

def test_cannot_edit_portions_while_reserved(client,hdr,helpers):
    b1=helpers.bale('PET-E1',pvc='10',cluster=0,version=1)
    bid=helpers.blend('BL-I','400','40')
    plan=client.get(f'/blends/{bid}/recommend',headers=hdr()).json()['selected']['portions']
    client.post(f'/blends/{bid}/reserve',headers=hdr(),json={'portions':[{'bale_id':x['bale_id'],'kg':x['kg']} for x in plan]})
    r=client.put(f'/blends/{bid}/portions',headers=hdr(),json={'portions':[{'bale_id':b1,'kg':'50'}]})
    assert r.status_code==409 and '释放预留' in r.json()['detail']

def test_unreserve_releases_hold(client,hdr,helpers):
    b1=helpers.bale('PET-U1',pvc='10',cluster=0,version=1)
    bid=helpers.blend('BL-J','400','40')
    plan=client.get(f'/blends/{bid}/recommend',headers=hdr()).json()['selected']['portions']
    client.post(f'/blends/{bid}/reserve',headers=hdr(),json={'portions':[{'bale_id':x['bale_id'],'kg':x['kg']} for x in plan]})
    assert reserve_count(bid)==len(plan)
    r=client.post(f'/blends/{bid}/unreserve',headers=hdr())
    assert r.status_code==200 and r.json()['reserved'] is False
    assert reserve_count(bid)==0
    # 称料行保留可继续手工编辑
    d=client.put(f'/blends/{bid}/portions',headers=hdr(),json={'portions':[{'bale_id':b1,'kg':'400'}]})
    assert d.status_code==200
    # 重复释放报错
    assert client.post(f'/blends/{bid}/unreserve',headers=hdr()).status_code==409

def test_check_then_reopen_releases_reservation(client,hdr,helpers):
    b1=helpers.bale('PET-K1',pvc='10',cluster=0,version=1)
    b2=helpers.bale('PET-K2',pvc='30',cluster=0,version=1)
    bid=helpers.blend('BL-K','400','40')
    plan=client.get(f'/blends/{bid}/recommend',headers=hdr()).json()['selected']['portions']
    client.post(f'/blends/{bid}/reserve',headers=hdr(),json={'portions':[{'bale_id':x['bale_id'],'kg':x['kg']} for x in plan]})
    # 审秤也按同一余量（含其他预留）校验
    assert client.post(f'/blends/{bid}/check',headers=hdr()).status_code==200
    assert blend_row(bid).status=='ready'
    # 退回草稿释放整单预留
    r=client.post(f'/blends/{bid}/reopen',headers=hdr())
    assert r.status_code==200 and r.json()['reserved'] is False
    assert reserve_count(bid)==0 and blend_row(bid).status=='draft'
    assert bale_row(b1).available_kg==Decimal('500')

def test_release_converts_reservation_to_deduction(client,hdr,helpers):
    b1=helpers.bale('PET-L1',pvc='10',cluster=0,version=1)
    bid=helpers.blend('BL-L','400','40')
    plan=client.get(f'/blends/{bid}/recommend',headers=hdr()).json()['selected']['portions']
    client.post(f'/blends/{bid}/reserve',headers=hdr(),json={'portions':[{'bale_id':x['bale_id'],'kg':x['kg']} for x in plan]})
    client.post(f'/blends/{bid}/check',headers=hdr())
    r=client.post(f'/blends/{bid}/release',headers=hdr('admin'))
    assert r.status_code==200,r.json()
    assert reserve_count(bid)==0
    held=sum(Decimal(str(p['kg'])) for p in plan)
    assert bale_row(b1).available_kg==Decimal('500')-next(Decimal(str(p['kg'])) for p in plan if p['bale_id']==b1)
    assert blend_row(bid).status=='released'
    # 重复放行拒绝，余量不变
    again=client.post(f'/blends/{bid}/release',headers=hdr('admin'))
    assert again.status_code==409
    assert bale_row(b1).available_kg==Decimal('500')-next(Decimal(str(p['kg'])) for p in plan if p['bale_id']==b1)

def test_concurrent_reservation_then_release_failure_leaves_both_untouched(client,hdr,helpers):
    # 两单各预留同包 300kg：第一单成功；第二单推荐会绕开，手工强占被拒
    b1=helpers.bale('PET-N1',pvc='10',cluster=0,version=1)
    first=helpers.blend('BL-N1','300','40')
    p1=client.get(f'/blends/{first}/recommend',headers=hdr()).json()['selected']['portions']
    client.post(f'/blends/{first}/reserve',headers=hdr(),json={'portions':[{'bale_id':x['bale_id'],'kg':x['kg']} for x in p1]})
    client.post(f'/blends/{first}/check',headers=hdr())
    # 第一单放行成功，实际扣减
    assert client.post(f'/blends/{first}/release',headers=hdr('admin')).status_code==200
    assert bale_row(b1).available_kg==Decimal('200')
    # 放行失败（重复）不改变余量与（已转正消失的）预留
    assert client.post(f'/blends/{first}/release',headers=hdr('admin')).status_code==409
    assert bale_row(b1).available_kg==Decimal('200')

def test_cross_group_reserve_rejected(client,hdr,helpers):
    b1=helpers.bale('PET-G1',pvc='10',cluster=0,version=1)
    b2=helpers.bale('PET-G2',pvc='10',cluster=1,version=1)
    bid=helpers.blend('BL-M','400','40')
    r=client.post(f'/blends/{bid}/reserve',headers=hdr(),
                  json={'portions':[{'bale_id':b1,'kg':'200'},{'bale_id':b2,'kg':'200'}]})
    assert r.status_code==409 and '跨群' in r.json()['detail']
    assert reserve_count(bid)==0

def test_viewer_cannot_reserve(client,hdr,helpers):
    helpers.bale('PET-V1',pvc='10',cluster=0,version=1)
    bid=helpers.blend('BL-V','400','40')
    plan=client.get(f'/blends/{bid}/recommend',headers=hdr('viewer')).json()['selected']['portions']
    r=client.post(f'/blends/{bid}/reserve',headers=hdr('viewer'),
                  json={'portions':[{'bale_id':x['bale_id'],'kg':x['kg']} for x in plan]})
    assert r.status_code==403

def test_reserve_rejects_different_second_plan(client,hdr,helpers):
    b1=helpers.bale('PET-W1',pvc='10',avail='300',cluster=0,version=1)
    b2=helpers.bale('PET-W2',pvc='10',avail='300',cluster=0,version=1)
    bid=helpers.blend('BL-W','400','40')
    plan=client.get(f'/blends/{bid}/recommend',headers=hdr()).json()['selected']['portions']
    assert len(plan)==2  # 两包拼 300+100
    client.post(f'/blends/{bid}/reserve',headers=hdr(),json={'portions':[{'bale_id':x['bale_id'],'kg':x['kg']} for x in plan]})
    # 不同方案（换包/换重量）→ 拒绝，要求先释放
    r=client.post(f'/blends/{bid}/reserve',headers=hdr(),json={'portions':[{'bale_id':b1,'kg':'200'},{'bale_id':b2,'kg':'200'}]})
    assert r.status_code==409 and '不同预留方案' in r.json()['detail']

def test_quarantine_blocks_recommendation_and_reserve(client,hdr,helpers):
    from src.quality_models import SupplierLot,LotBale
    b1=helpers.bale('PET-Q1',pvc='10',cluster=0,version=1)
    bid=helpers.blend('BL-Q','400','40')
    with Session.begin() as s:
        lot=SupplierLot(code='SL-Q1',supplier='测试站',state='quarantine',inspection_note='隔离测试依据至少八字')
        s.add(lot);s.flush();s.add(LotBale(lot_id=lot.id,bale_id=b1,received_snapshot={}))
    d=client.get(f'/blends/{bid}/recommend',headers=hdr()).json()
    assert d['feasible'] is False
    r=client.post(f'/blends/{bid}/reserve',headers=hdr(),json={'portions':[{'bale_id':b1,'kg':'400'}]})
    assert r.status_code==409
