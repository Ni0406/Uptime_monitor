from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
from pydantic import BaseModel, Field, HttpUrl
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
import os
import requests
import json
from celery import Celery
from celery.result import AsyncResult
import redis
import celeryconfig

# --- НАСТРОЙКИ ОКРУЖЕНИЯ ---
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL", "redis://uptime-redis:6379/0")
SECRET_KEY = os.getenv("SECRET_KEY", "super_secret_jwt_key")
ALGORITHM = "HS256"

celery_app = Celery("uptime_tasks")
celery_app.config_from_object(celeryconfig)

# --- МЕНЕДЖЕР КЭША (Требование HW 4: CacheManager) ---
class CacheManager:
    def __init__(self, redis_url: str):
        self.client = redis.Redis.from_url(redis_url, decode_responses=True)

    def get(self, key: str):
        data = self.client.get(key)
        return json.loads(data) if data else None

    def set(self, key: str, value: any, ttl: int = 15):
        self.client.setex(key, ttl, json.dumps(value))

    def exists(self, key: str) -> bool:
        return bool(self.client.exists(key))

    def delete(self, key: str):
        self.client.delete(key)

cache_manager = CacheManager(REDIS_URL)

# --- БАЗА ДАННЫХ ---
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    websites = relationship("Website", back_populates="owner")

class Website(Base):
    __tablename__ = "websites"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    url = Column(String)
    status = Column(String, default="Unknown")
    owner_id = Column(Integer, ForeignKey("users.id"))
    owner = relationship("User", back_populates="websites")

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Uptime Monitor")

# --- СХЕМЫ PYDANTIC ---
class UserCreate(BaseModel):
    email: str
    password: str

class WebsiteCreate(BaseModel):
    name: str = Field(..., min_length=1)
    url: HttpUrl

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- ЗАДАЧИ CELERY ---
@celery_app.task(name="ping_all_websites")
def ping_all_websites():
    """Запускается раз в минуту (Beat) и отправляет сайты на проверку."""
    db = SessionLocal()
    try:
        sites = db.query(Website).all()
        for site in sites:
            ping_website.delay(site.id)
    finally:
        db.close()

@celery_app.task(name="ping_website")
def ping_website(website_id: int):
    """Проверяет доступность конкретного сайта (Worker)."""
    db = SessionLocal()
    try:
        site = db.query(Website).filter(Website.id == website_id).first()
        if not site:
            return {"error": "Site not found"}

        try:
            response = requests.get(site.url, timeout=5)
            site.status = "Up" if response.status_code == 200 else "Down"
        except Exception:
            site.status = "Down"

        db.commit()
        # Инвалидируем кэш после обновления статуса
        cache_manager.delete("websites_cache")
        return {"site_id": site.id, "status": site.status}
    finally:
        db.close()

# --- АВТОРИЗАЦИЯ ---
def create_access_token(data: dict):
    to_encode = data.copy()
    to_encode.update({"exp": datetime.utcnow() + timedelta(hours=24)})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("sub") is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    user = db.query(User).filter(User.email == payload.get("sub")).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

# --- ЭНДПОИНТЫ ---
@app.post("/api/register")
def register(user: UserCreate, db: Session = Depends(get_db)):
    hashed_pw = pwd_context.hash(user.password)
    db.add(User(email=user.email, hashed_password=hashed_pw))
    db.commit()
    return {"msg": "User created"}

@app.post("/api/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == form_data.username).first()
    if not user or not pwd_context.verify(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect email or password")
    return {"access_token": create_access_token({"sub": user.email}), "token_type": "bearer"}

@app.get("/api/users/me")
def get_me(current_user: User = Depends(get_current_user)):
    return {"email": current_user.email}

@app.get("/api/websites/me")
def get_my_websites(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    sites = db.query(Website).filter(Website.owner_id == current_user.id).order_by(Website.id.desc()).all()
    return [{"id": s.id, "name": s.name, "url": s.url, "status": s.status} for s in sites]

@app.get("/api/websites")
def get_all_websites(db: Session = Depends(get_db)):
    """Отдает дашборд с использованием CacheManager."""
    cached = cache_manager.get("websites_cache")
    if cached:
        return cached

    sites = db.query(Website).order_by(Website.id.desc()).all()
    result = [{"id": s.id, "name": s.name, "url": s.url, "status": s.status} for s in sites]

    cache_manager.set("websites_cache", result, ttl=15)
    return result

@app.post("/api/websites")
def add_website(site: WebsiteCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Добавляет сайт и запускает асинхронную задачу."""
    db_site = Website(name=site.name, url=str(site.url), owner_id=current_user.id)
    db.add(db_site)
    db.commit()
    db.refresh(db_site)

    task = ping_website.delay(db_site.id)

    return {
        "id": db_site.id,
        "name": db_site.name,
        "url": db_site.url,
        "status": db_site.status,
        "task_id": task.id  # возвращаем task_id для отслеживания
    }

# --- НОВЫЙ ЭНДПОИНТ (Требование HW 3.4: AsyncResult) ---
@app.get("/api/tasks/{task_id}")
def get_task_status(task_id: str):
    """Позволяет проверить статус асинхронной задачи по task_id."""
    res = AsyncResult(task_id, app=celery_app)
    return {
        "task_id": task_id,
        "status": res.status,  
        "ready": res.ready(),
        "result": res.result if res.ready() else None
    }