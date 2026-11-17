"""端到端剧本：低PVC料包被别单先用后，推荐仍给出有解方案，并完整走预留→审秤→放行。"""
from decimal import Decimal
import sqlalchemy as sa
from src.database import Session
from src.models import Bale,Blend
from src.reservation_models import BlendReservation

def test_low_pvc_bale_taken_still_recommends_and_full_lifecycle(client,hdr,helpers):
    # 同版本同群：一包极低PVC料 + 若干普通料
    low=helpers.bale('PET-LOW',pvc='5',avail='500',cluster=0,version=1)
    a=helpers.bale('PET-A',pvc='15',avail='500',cluster=0,version=1)
    b=helpers.bale('PET-B',pvc='25',avail='300',cluster=0,version=1)
    c=helpers.bale('PET-C',pvc='18',avail='500',cluster=0,version=1)
    # 别单先把低PVC料整包预留（目标500）
    other=helpers.blend('BL-OTHER','500','40')
    r=client.post(f'/blends/{other}/reserve',headers=hdr(),json={'portions':[{'bale_id':low,'kg':'500'}]})
    assert r.status_code==200
    # 本单：目标400、PVC上限20。逐包贪选若先盯住已被占的低PVC包会误报无解；精确求解应有解
    bid=helpers.blend('BL-MAIN','400','20')
    d=client.get(f'/blends/{bid}/recommend',headers=hdr()).json()
    assert d['feasible'] is True,d
    sel=d['selected'];used={p['bale_id'] for p in sel['portions']}
    assert low not in used  # 已被别单预留的料不会进入方案
    assert sum(Decimal(str(p['kg'])) for p in sel['portions'])==Decimal('400')
    assert Decimal(str(sel['weighted_pvc']))<=Decimal('20')
    # 页面可核对逐组推荐依据
    assert all(set(g)>={'cluster_version','cluster_id','total_margin_kg','feasible','reason'} for g in d['groups'])
    # 确认采用 → 整单预留
    body={'portions':[{'bale_id':p['bale_id'],'kg':p['kg']} for p in sel['portions']]}
    v=client.post(f'/blends/{bid}/reserve',headers=hdr(),json=body).json()
    assert v['reserved'] is True and v['reserved_kg']==400.0
    # 已预留后再次推荐（排除本单自身占用）仍可行，且不产生重复预留
    again=client.post(f'/blends/{bid}/reserve',headers=hdr(),json=body)
    assert again.status_code==200 and again.json()['reserved_kg']==400.0
    with Session() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(BlendReservation).where(BlendReservation.blend_id==bid))==len(sel['portions'])
    # 堆场余量：低PVC包可用量不变但可称余量为0
    bales={x['id']:x for x in client.get('/bales',headers=hdr()).json()}
    assert Decimal(str(bales[low]['available_kg']))==Decimal('500')
    assert Decimal(str(bales[low]['margin_kg']))==Decimal('0')
    assert Decimal(str(bales[low]['reserved_kg']))==Decimal('500')
    # 审秤 → 放行（主管），预留转实扣
    assert client.post(f'/blends/{bid}/check',headers=hdr()).status_code==200
    rel=client.post(f'/blends/{bid}/release',headers=hdr('admin'))
    assert rel.status_code==200,rel.json()
    with Session() as s:
        assert s.get(Blend,bid).status=='released'
        assert s.scalar(sa.select(sa.func.count()).select_from(BlendReservation).where(BlendReservation.blend_id==bid))==0
    # 本单扣减只作用于本单料包；低PVC包仍保持别单预留状态（available=500）
    bales2={x['id']:x for x in client.get('/bales',headers=hdr()).json()}
    assert Decimal(str(bales2[low]['available_kg']))==Decimal('500')
    for p in sel['portions']:
        expect=Decimal('500')-Decimal(str(p['kg']))
        assert Decimal(str(bales2[p['bale_id']]['available_kg']))==expect
    # 别单放行后低PVC包才真实扣减
    client.post(f'/blends/{other}/check',headers=hdr())
    client.post(f'/blends/{other}/release',headers=hdr('admin'))
    bales3={x['id']:x for x in client.get('/bales',headers=hdr()).json()}
    assert Decimal(str(bales3[low]['available_kg']))==Decimal('0')

def test_reopen_returns_reservation_and_allows_replan(client,hdr,helpers):
    helpers.bale('PET-R1',pvc='10',avail='300',cluster=0,version=1)
    helpers.bale('PET-R2',pvc='12',avail='300',cluster=0,version=1)
    bid=helpers.blend('BL-RO','400','40')
    plan=client.get(f'/blends/{bid}/recommend',headers=hdr()).json()['selected']['portions']
    client.post(f'/blends/{bid}/reserve',headers=hdr(),json={'portions':[{'bale_id':p['bale_id'],'kg':p['kg']} for p in plan]})
    client.post(f'/blends/{bid}/check',headers=hdr())
    # 退回草稿 → 预留释放
    r=client.post(f'/blends/{bid}/reopen',headers=hdr())
    assert r.status_code==200 and r.json()['reserved'] is False
    with Session() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(BlendReservation).where(BlendReservation.blend_id==bid))==0
    # 释放后推荐可重新选料（余量回池）
    d=client.get(f'/blends/{bid}/recommend',headers=hdr()).json()
    assert d['feasible'] is True

def test_release_failure_leaves_reservation_and_margin_untouched(client,hdr,helpers):
    b1=helpers.bale('PET-F1',pvc='10',cluster=0,version=1)
    bid=helpers.blend('BL-F','400','40')
    plan=client.get(f'/blends/{bid}/recommend',headers=hdr()).json()['selected']['portions']
    client.post(f'/blends/{bid}/reserve',headers=hdr(),json={'portions':[{'bale_id':p['bale_id'],'kg':p['kg']} for p in plan]})
    client.post(f'/blends/{bid}/check',headers=hdr())
    # 非主管放行被拒：预留与余量都不变
    r=client.post(f'/blends/{bid}/release',headers=hdr('viewer'))
    assert r.status_code==403
    with Session() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(BlendReservation).where(BlendReservation.blend_id==bid))==len(plan)
        assert s.get(Bale,b1).available_kg==Decimal('500')
    # 主管放行成功后重复放行失败，余量同样不变
    client.post(f'/blends/{bid}/release',headers=hdr('admin'))
    after={x['id']:x for x in client.get('/bales',headers=hdr()).json()}
    client.post(f'/blends/{bid}/release',headers=hdr('admin'))
    again={x['id']:x for x in client.get('/bales',headers=hdr()).json()}
    assert again[b1]['available_kg']==after[b1]['available_kg']
    # 已放行称料行已真实扣减，不得再被当作占用而产生负可称余量
    for row in again.values():
        assert Decimal(str(row['margin_kg']))>=0

def test_released_portions_not_double_counted(client,hdr,helpers):
    b1=helpers.bale('PET-D1',pvc='10',avail='500',cluster=0,version=1)
    first=helpers.blend('BL-D1','400','40')
    plan=client.get(f'/blends/{first}/recommend',headers=hdr()).json()['selected']['portions']
    client.post(f'/blends/{first}/reserve',headers=hdr(),json={'portions':[{'bale_id':p['bale_id'],'kg':p['kg']} for p in plan]})
    client.post(f'/blends/{first}/check',headers=hdr())
    client.post(f'/blends/{first}/release',headers=hdr('admin'))
    row=next(x for x in client.get('/bales',headers=hdr()).json() if x['id']==b1)
    assert Decimal(str(row['available_kg']))==Decimal('100')
    assert Decimal(str(row['margin_kg']))==Decimal('100')  # 非负，放行部分未被二次占用
    # 剩余 100kg 仍可被新单推荐/预留
    second=helpers.blend('BL-D2','100','40')
    d=client.get(f'/blends/{second}/recommend',headers=hdr()).json()
    assert d['feasible'] is True
    assert sum(Decimal(str(p['kg'])) for p in d['selected']['portions'])==Decimal('100')
