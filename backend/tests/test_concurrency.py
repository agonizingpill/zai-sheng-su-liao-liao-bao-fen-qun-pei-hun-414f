"""并发抢料的串行等价验证（SQLite 无行锁，无法并行复现 InnoDB FOR UPDATE 阻塞）。

MySQL 下两个事务同时对同一料包 SELECT ... FOR UPDATE 时，后到事务阻塞并在
前者提交后读到最新余量；本测试以串行方式精确复现该提交顺序下的判定结果：
  - 第一单整单预留 300kg 成功；
  - 第二单再要 300kg（余量仅剩 200kg）整单被拒、不留任何预留行；
  - 重试确认同方案不重复占料（幂等）；
  - 堆场 available_kg 在预留阶段始终不变。
"""
import os,sys,types,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
_DB=Path(tempfile.gettempdir())/'poly_conc.db'
if _DB.exists():_DB.unlink()
os.environ['DATABASE_URL']='sqlite:///'+str(_DB);os.environ['SEED_DEMO']='false';os.environ['JWT_SECRET']='test-secret'
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.sql import Select
@event.listens_for(sa.Engine,'connect')
def _ac(c,r):c.isolation_level=None
Select.with_for_update=lambda self,*a,**k:self
_oe=Select.execution_options
Select.execution_options=lambda self,*a,**k:(k.pop('populate_existing',None),_oe(self,*a,**k))[1]
fr=types.SimpleNamespace(ping=lambda *a:True,get=lambda *a:None,set=lambda *a,**k:True,delete=lambda *a:1,rpush=lambda *a:1)
sys.modules.setdefault('redis',types.SimpleNamespace(Redis=types.SimpleNamespace(from_url=lambda *a,**k:fr)))
from decimal import Decimal
from fastapi import HTTPException
from src.database import Base,engine,Session
import src.models,src.quality_models,src.production_models,src.reservation_models
from src.models import User,Bale,Blend
from src.reservation_models import BlendReservation
from src.schemas import PortionsIn,PortionIn
from src import blending
from src.security import password_hash

Base.metadata.create_all(engine)
with Session.begin() as s:
    s.add(User(email='blender@huanli.local',password=password_hash('x'),role='blender'))
    s.add(Bale(code='PET-CONC',kg=Decimal('500'),available_kg=Decimal('500'),melt_index=Decimal('9'),moisture=Decimal('.1'),pvc_ppm=Decimal('10'),cluster_id=0,cluster_version=1))
    s.add(Blend(code='BL-C1',target_kg=Decimal('300'),max_pvc=Decimal('40'),status='draft',released_by=''))
    s.add(Blend(code='BL-C2',target_kg=Decimal('300'),max_pvc=Decimal('40'),status='draft',released_by=''))
u={'email':'blender@huanli.local','role':'blender'}
plan=PortionsIn(portions=[PortionIn(bale_id=1,kg=Decimal('300'))])
with Session.begin() as s:
    blending.reserve(s,blending.get(s,Blend,1),plan,u)
# 第二独立事务：模拟并发中后拿到行锁、读到第一单提交后余量的事务
try:
    with Session.begin() as s:
        blending.reserve(s,blending.get(s,Blend,2),plan,u)
    raise SystemExit('FAIL: second order should be rejected')
except HTTPException as e:
    assert '整单未预留' in e.detail
with Session() as s:
    rows=list(s.execute(sa.select(BlendReservation.blend_id,BlendReservation.kg)))
    assert rows==[(1,Decimal('300.000'))],rows  # 失败单不留行，原方案（空）保留
    assert s.get(Bale,1).available_kg==Decimal('500')
# 第一单重试确认同方案 → 幂等，不重复占料
with Session.begin() as s:
    blending.reserve(s,blending.get(s,Blend,1),plan,u)
with Session() as s:
    assert s.scalar(sa.select(sa.func.count()).select_from(BlendReservation))==1
# 第二单改要 200kg 需先把目标调整为 200kg（同群同版本、在上限内）→ 可以成功，两单合计恰好 500kg
with Session.begin() as s:
    s.get(Blend,2).target_kg=Decimal('200')
with Session.begin() as s:
    blending.reserve(s,blending.get(s,Blend,2),PortionsIn(portions=[PortionIn(bale_id=1,kg=Decimal('200'))]),u)
with Session() as s:
    total=s.scalar(sa.select(sa.func.coalesce(sa.func.sum(BlendReservation.kg),0)).where(BlendReservation.bale_id==1))
    assert total==Decimal('500.000'),total
# 再来任何正量都被余量门限拦截
try:
    with Session.begin() as s:
        s.add(Blend(code='BL-C3',target_kg=Decimal('1'),max_pvc=Decimal('40'),status='draft',released_by=''));s.flush()
        bid=s.scalar(sa.select(Blend.id).where(Blend.code=='BL-C3'))
    with Session.begin() as s:
        blending.reserve(s,blending.get(s,Blend,bid),PortionsIn(portions=[PortionIn(bale_id=1,kg=Decimal('1'))]),u)
    raise SystemExit('FAIL: over-margin reserve should be rejected')
except HTTPException as e:
    assert '余量不足' in e.detail
print('SERIALIZED CONCURRENCY INVARIANTS OK')
