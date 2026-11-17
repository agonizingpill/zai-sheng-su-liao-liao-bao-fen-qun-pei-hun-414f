"""同版本同群称料方案推荐。

目标：在同一个 (分群版本, 群号) 内选取若干料包并指定每包称入量，使
  1) 称入合计恰好等于目标重量（3位小数精度）；
  2) 加权 PVC = Σ(kg×pvc)/总重 不超过上限；
  3) 每包称入量不超过其当前可用余量（扣除其他配方的预留）。

贪心逐包挑低 PVC 料会在“先挑的包余量不足、需要后包补量”等场景误报无解，
因此这里对每个同版本同群候选组做精确可行性判定：按 PVC 升序逐包尽量取满，
只要该组可用余量合计 ≥ 目标重量，就一定能构造出总重恰好达标的方案；
再在所有可行组中选加权 PVC 最低的一组。
"""
from decimal import Decimal
from sqlalchemy import select,func
from .models import Bale,Blend,BlendPortion
from .reservation_models import BlendReservation
from .quality_models import LotBale,SupplierLot

Q=Decimal('0.001')
def q(x):return x.quantize(Q)

def occupied_map(s,exclude_blend_id=None):
    """每个料包被 *其他* 配方占用的重量：整单预留 + 未预留配方的手工称料，统一余量口径。

    已整单预留配方的称料行是其预留的镜像，只计预留一次，避免重复占用。
    """
    held={}
    rq=select(BlendReservation.bale_id,func.coalesce(func.sum(BlendReservation.kg),0))
    if exclude_blend_id is not None:rq=rq.where(BlendReservation.blend_id!=exclude_blend_id)
    for bid,total in s.execute(rq.group_by(BlendReservation.bale_id)).all():
        held[bid]=held.get(bid,Decimal(0))+total
    reserved_blends=set(s.scalars(select(BlendReservation.blend_id).distinct()))
    pq=select(BlendPortion.bale_id,BlendPortion.blend_id,func.coalesce(func.sum(BlendPortion.kg),0)).join(Blend,Blend.id==BlendPortion.blend_id).where(Blend.status!='released')
    if exclude_blend_id is not None:pq=pq.where(BlendPortion.blend_id!=exclude_blend_id)
    for bid,blend_id,total in s.execute(pq.group_by(BlendPortion.bale_id,BlendPortion.blend_id)).all():
        if blend_id in reserved_blends:continue
        held[bid]=held.get(bid,Decimal(0))+total
    return held

def quarantined_bales(s,bale_ids):
    if not bale_ids:return set()
    rows=s.execute(select(LotBale.bale_id).join(SupplierLot,SupplierLot.id==LotBale.lot_id).where(LotBale.bale_id.in_(bale_ids),SupplierLot.state=='quarantine').distinct()).all()
    return {r[0] for r in rows}

def candidate_bales(s,blend):
    """按 (分群版本, 群号) 聚合候选料包，余量=可用量-其他配方预留与称料占用。"""
    occupied=occupied_map(s,blend.id)
    all_ids=list(s.scalars(select(Bale.id).where(Bale.cluster_id.isnot(None))))
    blocked=quarantined_bales(s,all_ids)
    groups={}
    for b in s.scalars(select(Bale).where(Bale.cluster_id.isnot(None)).order_by(Bale.id)):
        margin=q(b.available_kg-occupied.get(b.id,Decimal(0)))
        if margin<=0 or b.id in blocked:continue
        groups.setdefault((b.cluster_version,b.cluster_id),[]).append({'bale':b,'margin':margin})
    return groups

def solve_group(candidates,target,max_pvc):
    """组内精确求解。candidates 已带扣除预留后的余量 margin。

    余量合计不足直接不可行；否则按 PVC 升序逐包尽量取满构造方案，
    再校验加权 PVC。加权 PVC 随“把重量从高 PVC 包挪到低 PVC 包”单调下降，
    故低 PVC 优先取满给出该组可达到的最低加权 PVC，方案不可被逐包贪选误杀。
    """
    total_margin=sum((c['margin'] for c in candidates),Decimal(0))
    if total_margin<target:return None
    ordered=sorted(candidates,key=lambda c:(c['bale'].pvc_ppm,c['bale'].id))
    remaining=target;picks=[]
    for c in ordered:
        if remaining<=0:break
        take=min(c['margin'],remaining)
        take=q(take)
        if take>0:
            picks.append((c['bale'],take));remaining=q(remaining-take)
    if remaining!=0 or not picks:return None
    weighted=sum((kg*b.pvc_ppm for b,kg in picks),Decimal(0))/target
    if weighted>max_pvc:return None
    return weighted,picks

def recommend(s,blend):
    target=q(blend.target_kg);max_pvc=blend.max_pvc
    groups=candidate_bales(s,blend)
    evaluations=[];best=None
    for (version,cluster_id),cands in sorted(groups.items(),key=lambda kv:(kv[0][0],kv[0][1])):
        total_margin=sum((c['margin'] for c in cands),Decimal(0))
        result=solve_group(cands,target,max_pvc)
        item={'cluster_version':version,'cluster_id':cluster_id,'bale_count':len(cands),
              'total_margin_kg':float(q(total_margin)),'target_kg':float(target),'max_pvc':float(max_pvc),'feasible':False,
              'reason':None,'weighted_pvc':None,'portions':[]}
        if total_margin<target:
            item['reason']=f'同群可用余量合计{q(total_margin)}kg低于目标{target}kg'
        elif result is None:
            item['reason']=f'余量可凑足目标，但最低可达加权PVC仍超过上限{max_pvc}ppm'
        else:
            weighted,picks=result
            item['feasible']=True;item['weighted_pvc']=float(weighted)
            item['portions']=[{'bale_id':b.id,'bale_code':b.code,'kg':float(kg),'available_kg':float(b.available_kg),
                               'margin_kg':float(next(c['margin'] for c in cands if c['bale'].id==b.id)),
                               'pvc_ppm':float(b.pvc_ppm),'melt_index':float(b.melt_index),'moisture':float(b.moisture),
                               'cluster_id':b.cluster_id,'cluster_version':b.cluster_version} for b,kg in picks]
            if best is None or weighted<best[0] or (weighted==best[0] and (version,cluster_id)<(best[1],best[2])):
                best=(weighted,version,cluster_id,item)
        evaluations.append(item)
    if best is None:
        return {'blend_id':blend.id,'blend_code':blend.code,'feasible':False,
                'reason':'所有同版本同群候选均无法同时满足目标重量与PVC上限（或供应批隔离/无已分群料包）',
                'selected':None,'groups':evaluations}
    return {'blend_id':blend.id,'blend_code':blend.code,'feasible':True,'reason':None,
            'selected':best[3],'groups':evaluations}
