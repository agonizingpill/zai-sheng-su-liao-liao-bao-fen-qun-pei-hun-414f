"""规划器单元测试：精确重量凑整、最低加权PVC、逐包贪选反例。"""
from decimal import Decimal
from src.planner import solve_group,q

def _c(bid,pvc,margin):
    b=type('B',(),{'id':bid,'pvc_ppm':Decimal(str(pvc)),'code':f'B{bid}','cluster_id':0,'cluster_version':1,'available_kg':Decimal(str(margin)),'melt_index':Decimal('9'),'moisture':Decimal('.1')})()
    return {'bale':b,'margin':Decimal(str(margin))}

def test_exact_fractional_target():
    # 余量为零散小数，目标 12.345kg：必须精确凑出而非近似
    cands=[_c(1,10,'5.123'),_c(2,20,'4.222'),_c(3,30,'3.000')]
    r=solve_group(cands,Decimal('12.345'),Decimal('40'))
    assert r is not None
    weighted,picks=r
    assert sum((k for _,k in picks),Decimal(0))==Decimal('12.345')
    for b,k in picks:
        assert k<=Decimal(next(c['margin'] for c in cands if c['bale'].id==b.id))

def test_low_pvc_filled_first_minimizes_weighted():
    cands=[_c(1,5,'500'),_c(2,20,'300'),_c(3,35,'300')]
    weighted,picks=solve_group(cands,Decimal('400'),Decimal('40'))
    # 最低PVC包先取满400（其余量500≥400），加权PVC即其PVC
    assert len(picks)==1 and picks[0][0].id==1 and picks[0][1]==Decimal('400')
    assert weighted==Decimal('5')

def test_greedy_single_bale_overshoot_then_split():
    # 若贪选“整包取”会拿500超目标；正确做法是低PVC包取300(满其拆分余量)再由次低补足
    cands=[_c(1,5,'300'),_c(2,20,'300'),_c(3,35,'300')]
    weighted,picks=solve_group(cands,Decimal('400'),Decimal('40'))
    assert sum((k for _,k in picks),Decimal(0))==Decimal('400')
    assert picks[0][0].id==1 and picks[0][1]==Decimal('300')  # 低PVC先取满其可用
    assert picks[1][0].id==2 and picks[1][1]==Decimal('100')  # 次低补足
    # 加权 PVC = (300*5+100*20)/400 = 8.75
    assert weighted==Decimal('8.75')

def test_insufficient_margin_infeasible():
    cands=[_c(1,5,'100'),_c(2,5,'150')]
    assert solve_group(cands,Decimal('400'),Decimal('40')) is None

def test_pvc_cap_violation_infeasible_even_with_enough_weight():
    cands=[_c(1,80,'500'),_c(2,90,'500')]
    assert solve_group(cands,Decimal('400'),Decimal('40')) is None

def test_quantize_helper():
    assert q(Decimal('1.2346'))==Decimal('1.235')  # 四舍五入到0.001
    assert q(Decimal('400'))==Decimal('400.000')

def test_tie_break_deterministic():
    # 同PVC不同包，结果按 bale.id 稳定排序
    cands=[_c(3,10,'500'),_c(1,10,'500'),_c(2,10,'500')]
    _,picks=solve_group(cands,Decimal('400'),Decimal('40'))
    assert picks[0][0].id==1
