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
from fastapi import Form

from .database import Base, engine, get_db
from . import models
from pydantic import BaseModel
from typing import List

class QualityRequest(BaseModel):
    qualities: List[str] = ["720p", "480p"]

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

@app.post("/overlay/image")
async def add_image_overlay(
    video_id: int,
    image_file: UploadFile = File(...),
    x: int = 10,
    y: int = 10,
    width: int = 100,
    height: int = 100,
    start_time: float = 0.0,
    end_time: float = 0.0,
    opacity: float = 1.0,
    db: Session = Depends(get_db)
):
    # Validate video
    video = db.query(models.Video).filter(models.Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    input_path = os.path.join(UPLOAD_DIR, video.filename)
    if not os.path.exists(input_path):
        raise HTTPException(status_code=404, detail="Source file not found")

    # Validate and save image
    if not image_file.filename.lower().endswith(('.png', '.jpg', '.jpeg')):
        raise HTTPException(status_code=400, detail="Only PNG/JPG images allowed")

    img_extension = image_file.filename.split('.')[-1]
    img_filename = f"overlay_img_{uuid.uuid4().hex}.{img_extension}"
    img_path = os.path.join(UPLOAD_DIR, img_filename)

    with open(img_path, "wb") as f:
        content = await image_file.read()
        f.write(content)

    # Generate output filename
    name, ext = os.path.splitext(video.filename)
    job_id = str(uuid.uuid4())
    output_filename = f"{name}_img_overlay_{job_id[:8]}{ext}"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    # Save overlay config
    overlay = models.Overlay(
        video_id=video_id,
        overlay_type="image",
        content=image_file.filename,
        position_x=x,
        position_y=y,
        start_time=start_time,
        end_time=end_time,
        opacity=opacity,
        scale_width=width,
        scale_height=height
    )
    db.add(overlay)
    db.commit()

    # Create job
    job = models.Job(
        id=job_id,
        original_video_id=video_id,
        status="pending",
        type="ImageOverlay"
    )
    db.add(job)
    db.commit()

    # Send to Celery
    celery_app.send_task(
        "app.tasks.add_image_overlay_task",
        args=[job_id, input_path, output_path, img_path, x, y, width, height, start_time, end_time, opacity],
        task_id=job_id
    )

    return {
        "job_id": job_id,
        "status": "pending",
        "message": "Image overlay processing started",
    }


@app.post("/overlay/video")
async def add_video_overlay(
    video_id: int,
    overlay_video: UploadFile = File(...),
    x: int = 10,
    y: int = 10,
    width: int = 320,
    height: int = 240,
    start_time: float = 0.0,
    end_time: float = 0.0,
    opacity: float = 1.0,
    db: Session = Depends(get_db)
):
    # Validate main video
    video = db.query(models.Video).filter(models.Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Main video not found")

    input_path = os.path.join(UPLOAD_DIR, video.filename)
    if not os.path.exists(input_path):
        raise HTTPException(status_code=404, detail="Source file not found")

    # Validate and save overlay video
    if not overlay_video.filename.lower().endswith(('.mp4', '.mov', '.avi', '.mkv')):
        raise HTTPException(status_code=400, detail="Only MP4/MOV/AVI/MKV videos allowed")

    overlay_extension = overlay_video.filename.split('.')[-1]
    overlay_filename = f"overlay_vid_{uuid.uuid4().hex}.{overlay_extension}"
    overlay_path = os.path.join(UPLOAD_DIR, overlay_filename)

    with open(overlay_path, "wb") as f:
        content = await overlay_video.read()
        f.write(content)

    # Generate output filename
    name, ext = os.path.splitext(video.filename)
    job_id = str(uuid.uuid4())
    output_filename = f"{name}_vid_overlay_{job_id[:8]}{ext}"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    # Save overlay config
    overlay = models.Overlay(
        video_id=video_id,
        overlay_type="video",
        content=overlay_video.filename,
        position_x=x,
        position_y=y,
        start_time=start_time,
        end_time=end_time,
        opacity=opacity,
        scale_width=width,
        scale_height=height
    )
    db.add(overlay)
    db.commit()

    # Create job
    job = models.Job(
        id=job_id,
        original_video_id=video_id,
        status="pending",
        type="VideoOverlay"
    )
    db.add(job)
    db.commit()

    # Send to Celery
    celery_app.send_task(
        "app.tasks.add_video_overlay_task",
        args=[job_id, input_path, output_path, overlay_path, x, y, width, height, start_time, end_time, opacity],
        task_id=job_id
    )

    return {
        "job_id": job_id,
        "status": "pending",
        "message": "Video overlay processing started",
    }

QUALITY_PRESETS = {
    "1080p": {"width": 1920, "height": 1080, "bitrate": "8000k"},
    "720p": {"width": 1280, "height": 720, "bitrate": "4000k"},
    "480p": {"width": 854, "height": 480, "bitrate": "2000k"},
    "360p": {"width": 640, "height": 360, "bitrate": "1000k"},
}

@app.post("/quality/{video_id}")
async def generate_quality_versions(
    video_id: int,
    request: QualityRequest,  # Default to these if not specified
    db: Session = Depends(get_db)
):
    # Validate video
    video = db.query(models.Video).filter(models.Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    input_path = os.path.join(UPLOAD_DIR, video.filename)
    if not os.path.exists(input_path):
        raise HTTPException(status_code=404, detail="Source file not found")
    
    qualities = request.qualities
    # Validate requested qualities
    invalid_qualities = [q for q in qualities if q not in QUALITY_PRESETS]
    if invalid_qualities:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid qualities: {invalid_qualities}. Available: {list(QUALITY_PRESETS.keys())}"
        )

    job_ids = []
    
    for quality in qualities:
        # Skip if already generated
        existing = db.query(models.VideoQuality)\
            .filter(models.VideoQuality.video_id == video_id)\
            .filter(models.VideoQuality.quality == quality).first()
        
        if existing:
            continue

        # Generate output filename
        name, ext = os.path.splitext(video.filename)
        output_filename = f"{name}_{quality}{ext}"
        output_path = os.path.join(UPLOAD_DIR, output_filename)

        # Create job
        job_id = str(uuid.uuid4())
        job = models.Job(
            id=job_id,
            original_video_id=video_id,
            status="pending"
        )
        db.add(job)
        db.commit()

        # Send to Celery
        celery_app.send_task(
            "app.tasks.convert_quality_task",
            args=[job_id, input_path, output_path, quality],
            task_id=job_id
        )

        job_ids.append(job_id)

    return {
        "video_id": video_id,
        "requested_qualities": qualities,
        "job_ids": job_ids,
        "message": f"Generating {len(job_ids)} quality versions"
    }

@app.get("/download/{filename}")
def download_file(filename: str):
    # Security check
    if ".." in filename or filename.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Look for file in uploads directory
    file_path = os.path.join(UPLOAD_DIR, filename)
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        file_path,
        media_type="video/mp4",
        filename=filename
    )

