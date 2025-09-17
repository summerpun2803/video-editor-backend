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
            original_filename=file.filename,
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

@app.post("/trim")
async def trim_video(video_id: int, start_time: float, end_time:float ,db: Session= Depends(get_db)):

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
        video_id=video_id,
        status="pending"
    )

    db.add(job)
    db.commit()

    try:
        duration = end_time - start_time
        
        # Build FFmpeg command
        cmd = [
            'ffmpeg',
            '-i', input_path,
            '-ss', str(start_time),
            '-t', str(duration),
            '-c:v', 'libx264',
            '-c:a', 'aac',
            '-strict', 'experimental',
            output_path
        ]
        
        # Run command
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"FFmpeg failed: {result.stderr}")

        # Verify output exists
        if not os.path.exists(output_path):
            raise Exception("Output file not created")

        # 6. Save new video to DB
        probe = ffmpeg.probe(output_path)
        format_info = probe.get('format', {})
        new_duration = float(format_info.get('duration', 0.0))
        new_size = int(format_info.get('size', 0))

        new_video = models.Video(
            filename=output_filename,
            original_filename=f"Trimmed from {video.original_filename}",
            duration=new_duration,
            size=new_size
        )
        db.add(new_video)
        db.commit()

        job.status = "completed"
        job.result_filename = output_filename
        db.commit()

        return {
            "job_id": job_id,
            "status": "completed",
            "video_id": new_video.id,
            "filename": output_filename,
            "download_url": f"/download/{output_filename}"
        }

    except Exception as e:
        job.status = "failed"
        db.commit()
        logger.error(f"Trim failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Trim failed: {str(e)}")