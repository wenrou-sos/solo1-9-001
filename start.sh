#!/usr/bin/env bash
# 一键启动：后端 (FastAPI :8000) + 前端 (Vite :5173)
# 首次运行会自动创建虚拟环境、安装依赖、初始化样例数据。
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT/backend"

if [ ! -d .venv ]; then
  echo ">> 创建 Python 虚拟环境并安装依赖..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

if [ ! -f ../data/hydro.db ]; then
  echo ">> 首次运行：生成并导入 3 个站点的样例数据..."
  .venv/bin/python -m app.seed
fi

echo ">> 启动后端 http://localhost:8000 ..."
.venv/bin/uvicorn app.main:app --port 8000 &
BACK_PID=$!

cd "$ROOT/frontend"
if [ ! -d node_modules ]; then
  echo ">> 安装前端依赖..."
  npm install
fi
echo ">> 启动前端 http://localhost:5173 ..."
npm run dev &
FRONT_PID=$!

trap "kill $BACK_PID $FRONT_PID 2>/dev/null" EXIT
wait
