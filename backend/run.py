"""后端启动入口：python run.py（等价于 uvicorn app.main:app --port 8000）"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
