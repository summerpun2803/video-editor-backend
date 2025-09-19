from fastapi import FastAPI, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from fastapi.responses import FileResponse
import time
import logging
import os
import uuid
import ffmpeg
import subprocess

from .database import Base, engine, get_db
from . import models

from .celery_app import celery_app
from .tasks import trim_video_task

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
            logger.info(f"Attempt {attempt + 1}: Connecting to database...")
            with engine.connect() as conn:
                logger.info("Database connected!")
                
                # Create tables
                logger.info("Creating tables if not exist...")
                models.Base.metadata.create_all(bind=engine)
                logger.info("Tables created!")
                return
                
        except OperationalError as e:
            logger.warning(f"Database not ready: {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                logger.error("Failed to connect to database after all retries")
             
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
        "duration": db_video.duration,
        "size": db_video.size,
        "upload_time": db_video.upload_time
    }

@app.post("/trim")
async def trim_video(video_id: int, start_time: float, end_time:float ,db: Session=Depends(get_db)):

    video = db.query(models.Video).filter(models.Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    
    input_path = os.path.join(UPLOAD_DIR, video.filename)
    if not os.path.exists(input_path):
        raise HTTPException(status_code=404, detail="Source file not found")
    
    name, ext = os.path.splitext(video.filename)
    job_id = str(uuid.uuid4())
    output_filename = f"{name}_trimmed_{job_id[:8]}{ext}"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    job = models.Job(
        id=job_id,
        original_video_id=video_id,
        status="pending",
        type="trim"
    )

    db.add(job)
    db.commit()

    celery_app.send_task(
        "app.tasks.trim_video_task",
        args=[job_id, input_path, output_path, start_time, end_time - start_time],
        task_id=job_id
    )

    return {
        "job_id": job_id,
        "status": "pending",
        "message": "Video trimming started in background",
        "type": job.type
    }

@app.get("/status/{job_id}")
async def get_job_status(job_id: str, db: Session= Depends(get_db)):

    job = db.query(models.Job).filter(models.Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="JOB not found")
    

    response = {
        "job_id": job.id,
        "status": job.status,
        "original_video_id": job.original_video_id,
        "type": job.type
    }
    
    if job.status == "completed":
        response["updated_video_id"] = job.updated_video_id
        response["result_filename"] = job.result_filename
        
    return response

@app.post("/overlay/text")
async def add_text_overlay(
    video_id: int,
    text: str,
    x: int = 10,
    y: int = 10,
    font_size: int = 24,
    font_color: str = "white",
    start_time: float = 0.0,
    end_time: float = 0.0,
    db: Session = Depends(get_db)
):
    video = db.query(models.Video).filter(models.Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    
    input_path = os.path.join(UPLOAD_DIR, video.filename)
    if not os.path.exists(input_path):
        raise HTTPException(status_code=404, detail="Source file not found")
    
    name, ext = os.path.splitext(video.filename)
    job_id = str(uuid.uuid4())
    output_filename = f"{name}_text_{job_id[:8]}{ext}"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    overlay = models.Overlay(
        video_id=video_id,
        overlay_type="text",
        content=text,
        position_x=x,
        position_y=y,
        start_time=start_time,
        end_time=end_time,
        font_size=font_size,
        font_color=font_color,
    )
    db.add(overlay)
    db.commit()

    job = models.Job(
        id=job_id,
        original_video_id=video_id,
        status="pending",
        type="TextOverlay"
    )
    db.add(job)
    db.commit()

    celery_app.send_task(
        "app.tasks.add_text_overlay_task",
        args=[job_id, input_path, output_path, overlay.id],
        task_id=job_id
    )

    return {
        "job_id": job_id,
        "status": "pending",
        "message": "Text overlay processing started",
        "type": job.type
    }