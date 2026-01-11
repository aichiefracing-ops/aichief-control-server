from fastapi import FastAPI

app = FastAPI(title="AI Chief Control Server")

@app.get("/")
def root():
    return {"status": "ok", "service": "ai-chief-control"}
