from decimal import Decimal
from datetime import datetime,timezone
from fastapi import HTTPException
from sqlalchemy import select,delete,func
from .models import Bale,Blend,BlendPortion,Audit
from .reservation_models import BlendReservation
from .quality_models import SupplierLot,LotBale

def obj(x):return {c.name:float(getattr(x,c.name)) if isinstance(getattr(x,c.name),Decimal) else getattr(x,c.name) for c in x.__table__.columns}
def get(s,model,id,lock=False):
    q=select(model).where(model.id==id)
    if lock:q=q.with_for_update().execution_options(populate_existing=True)
    x=s.scalar(q)
    if not x:raise HTTPException(404,'记录不存在')
    return x
def audit(s,u,action,detail):s.add(Audit(actor=u['email'],action=action,detail=str(detail),at=datetime.now(timezone.utc).isoformat()))
def portions(s,b):return s.scalars(select(BlendPortion).where(BlendPortion.blend_id==b.id).order_by(BlendPortion.bale_id)).all()
def reservations(s,b,lock=False):
    q=select(BlendReservation).where(BlendReservation.blend_id==b.id).order_by(BlendReservation.bale_id)
    if lock:q=q.with_for_update()
    return s.scalars(q).all()
def other_reserved(s,ids,exclude_blend_id):
    """料包被 *其他* 配方整单预留的重量：预留与手工称料共用同一余量。"""
    if not ids:return {}
    q=select(BlendReservation.bale_id,func.coalesce(func.sum(BlendReservation.kg),0)).where(BlendReservation.bale_id.in_(ids))
    if exclude_blend_id is not None:q=q.where(BlendReservation.blend_id!=exclude_blend_id)
    return {bid:total for bid,total in s.execute(q.group_by(BlendReservation.bale_id)).all()}
def other_portions(s,ids,exclude_blend_id):
    """料包被 *其他* 未放行配方手工称料占用的重量。

    已整单预留配方的称料行是预留镜像（跳过，避免与预留重复计算）；
    已放行配方的称料行已真实扣减 available_kg（跳过，避免二次占用）。
    """
    if not ids:return {}
    reserved_blends=set(s.scalars(select(BlendReservation.blend_id).distinct()))
    q=select(BlendPortion.bale_id,BlendPortion.blend_id,func.coalesce(func.sum(BlendPortion.kg),0)).join(Blend,Blend.id==BlendPortion.blend_id).where(BlendPortion.bale_id.in_(ids),Blend.status!='released')
    if exclude_blend_id is not None:q=q.where(BlendPortion.blend_id!=exclude_blend_id)
    held={}
    for bale_id,blend_id,kg in s.execute(q.group_by(BlendPortion.bale_id,BlendPortion.blend_id)).all():
        if blend_id in reserved_blends:continue
        held[bale_id]=held.get(bale_id,Decimal(0))+kg
    return held
def held_elsewhere(s,ids,exclude_blend_id):
    """其他配方对这些料包的全部占用：整单预留 + 未预留配方的手工称料，同一余量口径。"""
    held={}
    for bid,kg in other_reserved(s,ids,exclude_blend_id).items():held[bid]=held.get(bid,Decimal(0))+kg
    for bid,kg in other_portions(s,ids,exclude_blend_id).items():held[bid]=held.get(bid,Decimal(0))+kg
    return held
def view(s,b):
    d=obj(b);ps=portions(s,b);rs=reservations(s,b)
    d['portions']=[{**obj(p),'bale_code':s.get(Bale,p.bale_id).code} for p in ps];d['total_kg']=float(sum((p.kg for p in ps),Decimal(0)))
    d['reserved']=bool(rs);d['reservation']=[{**obj(r),'bale_code':s.get(Bale,r.bale_id).code} for r in rs]
    d['reserved_kg']=float(sum((r.kg for r in rs),Decimal(0)))
    return d
def set_portions(s,b,data,u):
    if b.status!='draft':raise HTTPException(409,'配方已审秤或已放行；先退回草稿才能调整')
    if reservations(s,b):raise HTTPException(409,'配方存在整单预留，手工改料前请先释放预留')
    ids=[x.bale_id for x in data.portions]
    if len(set(ids))!=len(ids):raise HTTPException(409,'同一料包只能称入一次')
    bales={x.id:x for x in s.scalars(select(Bale).where(Bale.id.in_(ids)).order_by(Bale.id).with_for_update())}
    held=held_elsewhere(s,ids,b.id)
    for p in data.portions:
        x=bales.get(p.bale_id)
        if not x:raise HTTPException(404,'所选料包不存在')
        if x.cluster_id is None:raise HTTPException(409,'料包尚未完成分群检验')
        if p.kg>x.available_kg-held.get(x.id,Decimal(0)):raise HTTPException(409,f'{x.code}可用余量不足（已扣除其他配方预留与称料占用），未保存配方')
    s.execute(delete(BlendPortion).where(BlendPortion.blend_id==b.id))
    for p in data.portions:
        x=bales[p.bale_id];s.add(BlendPortion(blend_id=b.id,bale_id=x.id,kg=p.kg,cluster_snapshot=x.cluster_id,version_snapshot=x.cluster_version,pvc_snapshot=x.pvc_ppm,melt_snapshot=x.melt_index,moisture_snapshot=x.moisture))
    b.weighted_pvc=None;s.flush();audit(s,u,'weighed',b.code);return view(s,b)
def _quarantine_blocked(s,ids):
    lot_ids=list(s.scalars(select(LotBale.lot_id).where(LotBale.bale_id.in_(ids)).distinct().order_by(LotBale.lot_id)))
    lots=list(s.scalars(select(SupplierLot).where(SupplierLot.id.in_(lot_ids)).order_by(SupplierLot.id).with_for_update().execution_options(populate_existing=True)))
    if any(x.state=='quarantine' for x in lots):raise HTTPException(409,'供应批次已隔离，不能审秤或最终投料放行')
def validate(s,b,locked_bales=None,use_reservation_margin=True):
    ps=portions(s,b)
    if not ps:raise HTTPException(409,'尚未选择料包，不能审秤')
    if len({(p.version_snapshot,p.cluster_snapshot) for p in ps})!=1:raise HTTPException(409,'跨群或跨分群版本选料，不能配混')
    total=sum((p.kg for p in ps),Decimal(0))
    if total!=b.target_kg:raise HTTPException(409,f'称入{total}kg与目标{b.target_kg}kg不一致')
    weighted=sum((p.kg*p.pvc_snapshot for p in ps),Decimal(0))/total
    if weighted>b.max_pvc:raise HTTPException(409,f'加权PVC {weighted:.3f}ppm超过配方上限{b.max_pvc}ppm')
    ids=sorted(p.bale_id for p in ps)
    if locked_bales is None:
        locked_bales={x.id:x for x in s.scalars(select(Bale).where(Bale.id.in_(ids)).order_by(Bale.id).with_for_update())}
    _quarantine_blocked(s,ids)
    held=held_elsewhere(s,ids,b.id) if use_reservation_margin else {}
    for p in ps:
        bale=locked_bales[p.bale_id] if locked_bales is not None else get(s,Bale,p.bale_id)
        margin=bale.available_kg-held.get(p.bale_id,Decimal(0))
        if p.kg>margin:raise HTTPException(409,f'{bale.code}可用余量不足（已扣除其他配方预留与称料占用），投料未扣减')
    return ps,weighted

def evaluate(s,b,u):
    if b.status!='draft':raise HTTPException(409,'只有草稿可审秤')
    _,weighted=validate(s,b);b.weighted_pvc=weighted;b.status='ready';audit(s,u,'checked',b.code);return view(s,b)
def _same_plan(rows,data):
    want=sorted((p.bale_id,p.kg) for p in data.portions)
    have=sorted((r.bale_id,r.kg) for r in rows)
    return want==have
def reserve(s,b,data,u):
    """确认采用推荐：整单预留。重复确认同方案幂等不重复占料；余量变化整单拒绝。"""
    if b.status!='draft':raise HTTPException(409,'仅草稿配方可确认整单预留')
    ids=[x.bale_id for x in data.portions]
    if not ids:raise HTTPException(409,'预留方案不能为空')
    if len(set(ids))!=len(ids):raise HTTPException(409,'同一料包只能称入一次')
    existing=reservations(s,b,lock=True)
    if existing:
        if _same_plan(existing,data):
            audit(s,u,'reserve_retry',b.code);return view(s,b)
        raise HTTPException(409,'配方已有不同预留方案，请先释放预留再重新确认')
    bales={x.id:x for x in s.scalars(select(Bale).where(Bale.id.in_(ids)).order_by(Bale.id).with_for_update())}
    held=held_elsewhere(s,ids,b.id)
    groups=set();total=Decimal(0);weighted_sum=Decimal(0)
    plan=[]
    for p in data.portions:
        x=bales.get(p.bale_id)
        if not x:raise HTTPException(404,'所选料包不存在')
        if x.cluster_id is None:raise HTTPException(409,'料包尚未完成分群检验')
        if p.kg<=0:raise HTTPException(409,'预留重量必须为正')
        if p.kg>x.available_kg-held.get(x.id,Decimal(0)):raise HTTPException(409,f'{x.code}可用余量不足（含并发预留变化），整单未预留，原方案保留')
        groups.add((x.cluster_version,x.cluster_id));plan.append((x,p.kg))
        total+=p.kg;weighted_sum+=p.kg*x.pvc_ppm
    if len(groups)!=1:raise HTTPException(409,'预留方案跨群或跨分群版本，整单未预留')
    if total!=b.target_kg:raise HTTPException(409,f'预留合计{total}kg与目标{b.target_kg}kg不一致，整单未预留')
    weighted=weighted_sum/total
    if weighted>b.max_pvc:raise HTTPException(409,f'加权PVC {weighted:.3f}ppm超过上限{b.max_pvc}ppm，整单未预留')
    _quarantine_blocked(s,ids)
    at=datetime.now(timezone.utc).isoformat()
    s.execute(delete(BlendPortion).where(BlendPortion.blend_id==b.id))
    for x,kg in plan:
        s.add(BlendPortion(blend_id=b.id,bale_id=x.id,kg=kg,cluster_snapshot=x.cluster_id,version_snapshot=x.cluster_version,pvc_snapshot=x.pvc_ppm,melt_snapshot=x.melt_index,moisture_snapshot=x.moisture))
        s.add(BlendReservation(blend_id=b.id,bale_id=x.id,kg=kg,cluster_snapshot=x.cluster_id,version_snapshot=x.cluster_version,pvc_snapshot=x.pvc_ppm,reserved_by=u['email'],reserved_at=at))
    b.weighted_pvc=None;s.flush();audit(s,u,'reserved',b.code);return view(s,b)
def unreserve(s,b,u):
    """退回草稿语义下的显式释放：删除整单预留，称料行保留可再编辑。"""
    if b.status!='draft':raise HTTPException(409,'仅草稿配方可释放预留；待放行单请先退回草稿')
    rows=reservations(s,b,lock=True)
    if not rows:raise HTTPException(409,'配方没有整单预留')
    s.execute(delete(BlendReservation).where(BlendReservation.blend_id==b.id))
    s.flush();audit(s,u,'reservation_released',b.code);return view(s,b)
def release(s,b,u):
    if b.status!='ready':raise HTTPException(409,'配方须审秤通过且不可重复放行')
    ps=portions(s,b);ids=sorted(p.bale_id for p in ps)
    locked={x.id:x for x in s.scalars(select(Bale).where(Bale.id.in_(ids)).order_by(Bale.id).with_for_update().execution_options(populate_existing=True))}
    # 放行是真实扣减：以堆场可用量为硬门限（不再扣除预留，预留本单随后转正）。
    ps,weighted=validate(s,b,locked,use_reservation_margin=False)
    for p in ps:
        if p.kg>locked[p.bale_id].available_kg:raise HTTPException(409,f'{locked[p.bale_id].code}可用重量不足，整单回滚，预留与余量均不变')
    s.execute(delete(BlendReservation).where(BlendReservation.blend_id==b.id))
    for p in ps:locked[p.bale_id].available_kg-=p.kg
    b.status='released';b.weighted_pvc=weighted;b.released_by=u['email'];audit(s,u,'released',b.code);s.flush();return view(s,b)
def reopen(s,b,u):
    if b.status!='ready':raise HTTPException(409,'仅待放行配方可退回，已放行配方不可改动')
    s.execute(delete(BlendReservation).where(BlendReservation.blend_id==b.id))
    b.status='draft';b.weighted_pvc=None;audit(s,u,'blend_reopened',b.code);return view(s,b)
