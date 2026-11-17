import os,sys,types,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
_DB=Path(tempfile.gettempdir())/'poly_test.db'
if _DB.exists():_DB.unlink()
os.environ['DATABASE_URL']='sqlite:///'+str(_DB)
os.environ['SEED_DEMO']='false'
os.environ['JWT_SECRET']='test-secret'
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.sql import Select
# pysqlite 默认会隐式开启事务，导致应用内另开的 Session 读不到未提交的预留行；
# 驱动连接设为 AUTOCOMMIT 后，事务边界完全由 SQLAlchemy 的 Session.begin() 控制，
# 与 MySQL/InnoDB 下“连接内可读本事务未提交写入”的行为对齐
@event.listens_for(sa.Engine,'connect')
def _sqlite_autocommit(dbapi_conn,rec):
    dbapi_conn.isolation_level=None
# SQLite 不支持 MySQL 的 FOR UPDATE / populate_existing，测试中在类级别降级为无操作
Select.with_for_update=lambda self,*a,**k:self
_orig_execution_options=Select.execution_options
def _poly_opts(self,*a,**k):
    k.pop('populate_existing',None)
    return _orig_execution_options(self,*a,**k)
Select.execution_options=_poly_opts
fake_redis=types.SimpleNamespace(ping=lambda *a:True,get=lambda *a:None,set=lambda *a,**k:True,delete=lambda *a:1,rpush=lambda *a:1)
sys.modules.setdefault('redis',types.SimpleNamespace(Redis=types.SimpleNamespace(from_url=lambda *a,**k:fake_redis)))
import pytest
from fastapi.testclient import TestClient
from src.database import Base,engine,Session
import src.models, src.quality_models, src.production_models, src.reservation_models
import src.main as main
main.r=fake_redis
from src.models import User,Bale,Blend
from src.security import password_hash,token_for
from decimal import Decimal

@pytest.fixture
def client():
    Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
    with Session.begin() as s:
        for role in ('admin','blender','viewer'):
            s.add(User(email=f'{role}@huanli.local',password=password_hash('Blend2026!'),role=role))
    return TestClient(main.app)

@pytest.fixture
def hdr():
    def _h(role='blender'):
        with Session() as s:
            u=s.scalar(sa.select(User).where(User.email==f'{role}@huanli.local'))
            return {'Authorization':'Bearer '+token_for(u)}
    return _h

@pytest.fixture
def helpers():
    class H:
        @staticmethod
        def bale(code,avail='500',pvc='20',cluster=0,version=1,kg='500',mi='9.5',moist='0.12'):
            with Session.begin() as s:
                b=Bale(code=code,kg=Decimal(kg),available_kg=Decimal(avail),melt_index=Decimal(mi),moisture=Decimal(moist),
                       pvc_ppm=Decimal(pvc),cluster_id=cluster,cluster_version=version)
                s.add(b);s.flush();return b.id
        @staticmethod
        def blend(code='BL-T',target='400',pvc='40'):
            with Session.begin() as s:
                b=Blend(code=code,target_kg=Decimal(target),max_pvc=Decimal(pvc),status='draft',released_by='')
                s.add(b);s.flush();return b.id
    return H()
