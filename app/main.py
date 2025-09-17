from fastapi import FastAPI, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
import time
import logging
import os
import uuid
import ffmpeg

from .database import Base, engine, get_db
from . import models

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
UPLOAD_DIR = "./uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="Video Editor Backend")

# Health check endpoint — NO DB connection on startup
@app.get("/health")
def health_check():
    return {"status": "OK", "message": "Backend is running"}

# Only create tables after app is running
@app.on_event("startup")
async def startup_event():
    logger.info("⏳ Starting up... Waiting 10 seconds for DB to be fully ready...")
    time.sleep(10)  # Give PostgreSQL time to fully initialize
    
    max_retries = 5
    retry_delay = 5
    
    for attempt in range(max_retries):
        try:
            logger.info(f"🔁 Attempt {attempt + 1}: Connecting to database...")
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                logger.info("✅ Database connected!")
                
                # Create tables
                logger.info("🔨 Creating tables if not exist...")
                models.Base.metadata.create_all(bind=engine)
                logger.info("✅ Tables created!")
                return
                
        except OperationalError as e:
            logger.warning(f"⚠️ Database not ready (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                logger.error("❌ Failed to connect to database after all retries")
                # Don't crash the app — let it start, but DB operations will fail
                # This way, /health still works, and you can debug

@app.get("/")
def read_root():
    return {"message": "Video Editor Backend is running!"}

@app.post("/upload")
async def upload_video(file: UploadFile = File(...), db: Session = Depends(get_db)):
    # Generate safe unique filename
    file_extension = file.filename.split('.')[-1] if '.' in file.filename else 'mp4'
    safe_filename = f"{uuid.uuid4().hex}.{file_extension}"
    file_path = os.path.join(UPLOAD_DIR, safe_filename)
    
    logger.info(f"File name: {file_path}")  # ← FIXED LOGGING
    
   
    try:
        with open(file_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):  # 1MB chunks
                f.write(chunk)
    except Exception as e:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")
    
    # Extract metadata using ffmpeg
    try:
        probe = ffmpeg.probe(file_path)
        format_info = probe.get('format', {})
        video_stream = next((s for s in probe.get('streams', []) if s.get('codec_type') == 'video'), None)
        
        duration = float(format_info.get('duration', 0.0))
        size = int(format_info.get('size', 0))
        
    except Exception as e:
        # Clean up file if metadata fails
        if os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(status_code=400, detail=f"Invalid video file: {str(e)}")
    
    # Save to database
    try:
        db_video = models.Video(
            filename=safe_filename,
            original_filename=file.filename,  # ← Now safe!
            duration=duration,
            size=size
        )
        db.add(db_video)
        db.commit()
        db.refresh(db_video)
    except Exception as e:
        db.rollback()
        logger.error(f"Database error: {e}")
        if os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    
    return {
        "id": db_video.id,
        "filename": db_video.filename,
        "original_filename": db_video.original_filename,
        "duration": db_video.duration,
        "size": db_video.size,
        "upload_time": db_video.upload_time
    }