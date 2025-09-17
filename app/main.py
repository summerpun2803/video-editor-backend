from fastapi import FastAPI

app = FastAPI(title="Video Editor Backend")

@app.get("/")
def read_root():
    return {"Hello": "Video Editor Backend is running!"}

@app.get("/health")
def health_check():
    return {"status": "OK"}