from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
from pydantic import BaseModel
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
import os
import requests
import json
from celery import Celery
import redis

# --- НАСТРОЙКИ ---
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@db:5432/uptime_db")
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "amqp://guest:guest@rabbitmq:5672//")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# Для jwt авторизации. Идет
SECRET_KEY = "super_secret_key"
ALGORITHM = "HS256"

# Подключения
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")

celery_app = Celery("uptime_tasks", broker=CELERY_BROKER_URL)
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# --- БАЗА ДАННЫХ (Модели) ---
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

# --- СХЕМЫ (Pydantic) ---
class UserCreate(BaseModel): email: str; password: str
class WebsiteCreate(BaseModel): name: str; url: str

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

# --- ФОНОВАЯ ЗАДАЧА CELERY (HW 4) ---
@celery_app.task(name="ping_website")
def ping_website(website_id: int):
    db = SessionLocal()
    try:
        site = db.query(Website).filter(Website.id == website_id).first()
        if not site: return
        
        try:
            response = requests.get(site.url, timeout=5)
            site.status = "Up" if response.status_code == 200 else "Down"
        except:
            site.status = "Down"
            
        db.commit()
        redis_client.delete("websites_cache")
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
        if payload.get("sub") is None: raise credentials_exception
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.email == payload.get("sub")).first()
    if not user: raise HTTPException(status_code=401, detail="User not found")
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

# --- КЭШИРОВАНИЕ REDIS (HW 5) ---
@app.get("/api/websites")
def get_all_websites(db: Session = Depends(get_db)):

    cached = redis_client.get("websites_cache")
    if cached:
        return json.loads(cached)
    
    sites = db.query(Website).order_by(Website.id.desc()).all()
    result =[{"id": s.id, "name": s.name, "url": s.url, "status": s.status} for s in sites]
    
    redis_client.setex("websites_cache", 15, json.dumps(result))
    return result

@app.post("/api/websites")
def add_website(site: WebsiteCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    db_site = Website(name=site.name, url=site.url, owner_id=current_user.id)
    db.add(db_site)
    db.commit()
    db.refresh(db_site)
    

    ping_website.delay(db_site.id)

    redis_client.delete("websites_cache")
    return db_site


@app.get("/api/users/me")
def get_me(current_user: User = Depends(get_current_user)):
    """Возвращает данные текущего пользователя (email)"""
    return {"email": current_user.email}

@app.get("/api/websites/me")
def get_my_websites(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Возвращает только те сайты, которые добавил текущий пользователь"""
    sites = db.query(Website).filter(Website.owner_id == current_user.id).order_by(Website.id.desc()).all()
    return[{"id": s.id, "name": s.name, "url": s.url, "status": s.status} for s in sites]
